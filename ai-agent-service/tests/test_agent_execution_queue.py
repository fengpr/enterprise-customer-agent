"""覆盖 Redis Stream 可靠队列的 ACK、重试、DLQ 与幂等行为。"""

import json

from schemas.intent_schema import AgentExecutionJob
from services.agent_execution_queue import AgentExecutionQueue


class FakeRedis:
    """最小 Redis Stream 内存替身，记录 Consumer Group 消息和 ACK。"""

    def __init__(self):
        self.values, self.streams, self.pending, self.acks = {}, {}, {}, []
        self.counter = 0
        self.last_block = None

    def ping(self): return True
    def xgroup_create(self, *args, **kwargs): return True
    def get(self, key): return self.values.get(key)
    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values: return False
        self.values[key] = value; return True
    def setex(self, key, ttl, value): self.values[key] = value
    def scan_iter(self, match=None):
        prefix = (match or "").rstrip("*")
        return iter([key for key in self.values if key.startswith(prefix)])
    def xadd(self, stream, fields):
        self.counter += 1; stream_id = f"{self.counter}-0"
        self.streams.setdefault(stream, []).append((stream_id, fields)); return stream_id
    def xreadgroup(self, group, consumer, streams, count=1, block=0):
        self.last_block = block
        stream = next(iter(streams)); messages = self.streams.get(stream, [])[:count]
        if not messages: return []
        self.streams[stream] = self.streams[stream][count:]
        self.pending[stream] = messages
        return [(stream, messages)]
    def xautoclaim(self, stream, group, consumer, min_idle, start, count=1):
        return ("0-0", self.pending.get(stream, [])[:count], [])
    def xack(self, stream, group, stream_id):
        self.acks.append(stream_id)
        self.pending[stream] = [item for item in self.pending.get(stream, []) if item[0] != stream_id]
        return 1


def _queue():
    """构造显式启用的内存队列。"""
    queue = AgentExecutionQueue(redis_client=FakeRedis(), consumer_name="test-worker")
    return queue


def _job(key="key-1"):
    """构造不含客户 Token 的安全任务。"""
    return AgentExecutionJob(request_id="request-1", customer_id=8, message="查询订单", idempotency_key=key, execution_credential="v1.short.signature")


def test_job_restores_short_lived_execution_identity_without_customer_token():
    """Worker 还原任务时应使用短期内部身份，不能依赖或恢复客户原始 Token。"""
    request = _job().to_request()

    assert request.auth_token == "agent-execution:8:request-1:v1.short.signature"
    assert "Bearer " not in request.auth_token


def test_enqueue_claim_and_ack_success(monkeypatch):
    """正常入队、Consumer Group 消费并成功 ACK。"""
    monkeypatch.setenv("AGENT_EXECUTION_QUEUE_ENABLED", "true")
    queue = _queue(); request_id = queue.enqueue(_job(), "owner")
    stream_id, claimed_id, job, attempt = queue.claim(0)
    queue.ack_success(stream_id, claimed_id, {"answer": "完成"})
    assert request_id == claimed_id == "request-1"
    assert job.customer_id == 8 and attempt == 0
    assert stream_id in queue._redis.acks
    assert queue.get(request_id)["status"] == "SUCCESS"


def test_pending_recovery_and_retry(monkeypatch):
    """Worker 重启后可认领 Pending，失败后应重新写入 Stream。"""
    monkeypatch.setenv("AGENT_EXECUTION_QUEUE_ENABLED", "true")
    queue = _queue(); queue.enqueue(_job(), "owner")
    stream_id, request_id, job, _ = queue.claim(0)
    recovered = queue.recover_pending()
    assert recovered[1] == request_id
    queue.retry_or_dead_letter(stream_id, request_id, job, 0, "UPSTREAM")
    assert queue.get(request_id)["status"] == "PENDING"
    assert queue.get(request_id)["attempt"] == 1
    assert queue._redis.streams[queue.stream_key]


def test_max_attempt_goes_to_safe_dead_letter(monkeypatch):
    """超过最大尝试次数时 DLQ 只能保留失败摘要，不能保存任务敏感载荷。"""
    monkeypatch.setenv("AGENT_EXECUTION_QUEUE_ENABLED", "true")
    queue = _queue(); queue.max_attempts = 2; queue.enqueue(_job(), "owner")
    stream_id, request_id, job, _ = queue.claim(0)
    queue.retry_or_dead_letter(stream_id, request_id, job, 1, "UPSTREAM")
    _, fields = queue._redis.streams[queue.dead_letter_key][0]
    assert queue.get(request_id)["status"] == "DEAD_LETTER"
    assert set(fields) == {"request_id", "attempt", "error_code"}


def test_idempotency_does_not_enqueue_twice_or_store_authorization(monkeypatch):
    """相同客户幂等键复用 request_id，Stream 载荷不含 Authorization。"""
    monkeypatch.setenv("AGENT_EXECUTION_QUEUE_ENABLED", "true")
    safe_job = _job("same").model_copy(update={"login_user_context": {"display_name": "张三", "role": "customer", "verified": True, "source": "java_auth"}})
    queue = _queue(); first = queue.enqueue(safe_job, "owner")
    second = queue.enqueue(AgentExecutionJob(request_id="request-2", customer_id=8, message="重复", idempotency_key="same"), "owner")
    _, fields = queue._redis.streams[queue.stream_key][0]
    job_payload = json.loads(fields["job"])
    assert first == second == "request-1"
    assert len(queue._redis.streams[queue.stream_key]) == 1
    assert "Authorization" not in json.dumps(fields)
    assert "auth_token" not in fields["job"]
    assert job_payload["login_user_context"]["display_name"] == "张三"
    assert "customer_id" not in job_payload["login_user_context"]


def test_worker_heartbeat_replaces_unreliable_process_detection(monkeypatch):
    """启动脚本应根据 Redis 心跳判断 Worker 是否真正存活。"""
    monkeypatch.setenv("AGENT_EXECUTION_QUEUE_ENABLED", "true")
    queue = _queue()

    assert queue.has_active_worker() is False
    queue.heartbeat()
    assert queue.has_active_worker() is True


def test_queue_socket_timeout_exceeds_stream_block(monkeypatch):
    """Redis 读超时必须大于 Stream 阻塞周期，避免空队列等待导致 Worker 退出。"""
    import redis

    captured = {}
    fake = FakeRedis()

    def from_url(url, **kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("AGENT_EXECUTION_QUEUE_ENABLED", "true")
    monkeypatch.setenv("AGENT_QUEUE_BLOCK_MS", "3000")
    monkeypatch.setenv("AGENT_REDIS_SOCKET_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(redis.Redis, "from_url", from_url)

    queue = AgentExecutionQueue(consumer_name="timeout-test-worker")

    assert queue.redis_socket_timeout_seconds >= 4
    assert captured["socket_timeout"] == queue.redis_socket_timeout_seconds
    queue.claim()
    assert fake.last_block == 3000
