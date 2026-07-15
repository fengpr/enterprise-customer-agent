"""基于 Redis Stream Consumer Group 的可靠在线 Agent 队列。"""

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from schemas.intent_schema import AgentExecutionJob
from services.observability import DLQ_DEPTH, QUEUE_DEPTH, QUEUE_PENDING, QUEUE_RETRIES, QUEUE_RUNNING


class AgentExecutionQueue:
    """提供 Stream 入队、ACK、Pending 恢复、重试、死信与幂等状态管理。"""

    stream_key = "agent:execution:stream"
    dead_letter_key = "agent:execution:dead-letter"
    group_name = "agent-execution-workers"
    worker_heartbeat_prefix = "agent:execution:worker:heartbeat:"

    def __init__(self, redis_client: Any | None = None, consumer_name: str | None = None) -> None:
        """初始化 Redis 客户端和 Consumer Group；注入客户端便于集成测试。"""
        self._redis = redis_client
        self.consumer_name = consumer_name or os.getenv("AGENT_WORKER_NAME", f"worker-{uuid.uuid4().hex[:8]}")
        self.max_attempts = int(os.getenv("AGENT_JOB_MAX_ATTEMPTS", "3"))
        self.visibility_timeout_ms = int(os.getenv("AGENT_JOB_VISIBILITY_TIMEOUT_MS", "30000"))
        self.ttl_seconds = int(os.getenv("AGENT_JOB_TTL_SECONDS", "600"))
        self.idempotency_ttl = int(os.getenv("AGENT_IDEMPOTENCY_TTL_SECONDS", "600"))
        self.claim_block_ms = max(100, int(os.getenv("AGENT_QUEUE_BLOCK_MS", "1000")))
        configured_socket_timeout = float(os.getenv("AGENT_REDIS_SOCKET_TIMEOUT_SECONDS", "5"))
        # Redis 读超时必须明显大于 XREADGROUP 阻塞周期，否则空队列也会被误判为连接超时。
        self.redis_socket_timeout_seconds = max(configured_socket_timeout, self.claim_block_ms / 1000 + 1)
        # 心跳 TTL 要略大于空闲轮询周期，同时能尽快识别卡在下游调用中的失联 Worker。
        self.worker_heartbeat_ttl = int(os.getenv("AGENT_WORKER_HEARTBEAT_TTL_SECONDS", "20"))
        if self._redis is None:
            redis_url = os.getenv("REDIS_URL")
            if not redis_url:
                return
            try:
                import redis
                self._redis = redis.Redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=1,
                    socket_timeout=self.redis_socket_timeout_seconds,
                    health_check_interval=15,
                )
                self._redis.ping()
            except Exception:
                self._redis = None
        if self._redis:
            self._ensure_group()

    @property
    def enabled(self) -> bool:
        """仅在 Redis 连通且显式开关开启时允许使用可靠队列。"""
        return bool(self._redis and os.getenv("AGENT_EXECUTION_QUEUE_ENABLED", "true").lower() in {"1", "true", "yes", "on"})

    def enqueue(self, job: AgentExecutionJob, owner: str) -> str:
        """用 SET NX 去重后 XADD；任务载荷只保留脱敏业务字段与短期执行凭证。"""
        self._require_enabled()
        key = self._idempotency_key(job.customer_id, job.idempotency_key)
        existing = self._redis.get(key)
        if existing:
            return existing
        if not self._redis.set(key, job.request_id, nx=True, ex=self.idempotency_ttl):
            return self._redis.get(key)
        payload = job.model_dump(mode="json")
        # 防御性删除，确保调用方无法通过扩展字段把客户 Token 写入 Redis。
        payload.pop("auth_token", None)
        self._save_status(job.request_id, {"status": "PENDING", "owner": owner, "attempt": 0, "customer_status": "已进入处理队列"})
        self._redis.xadd(self.stream_key, {"request_id": job.request_id, "job": json.dumps(payload, ensure_ascii=False), "attempt": "0"})
        self._refresh_metrics()
        return job.request_id

    def heartbeat(self, ttl_seconds: int | None = None) -> None:
        """写入消费者心跳，供启动脚本识别真正可工作的 Agent Worker。

        心跳不携带客户信息。Worker 卡在模型调用时无法刷新心跳，TTL 到期后再次
        执行启动脚本会启动新的消费者，由 Redis Stream 恢复超时 Pending 任务。
        """
        if not self.enabled:
            return
        try:
            ttl = int(ttl_seconds or self.worker_heartbeat_ttl)
            self._redis.setex(self._worker_heartbeat_key(self.consumer_name), ttl, str(int(time.time())))
        except Exception:
            # 心跳仅用于运维发现，Redis 短暂异常不能中断正常的队列执行路径。
            pass

    def has_active_worker(self) -> bool:
        """检查是否存在未过期的 Worker 心跳，而不是依赖系统进程列表。"""
        if not self.enabled:
            return False
        try:
            return any(True for _ in self._redis.scan_iter(match=f"{self.worker_heartbeat_prefix}*"))
        except Exception:
            return False

    def snapshot(self) -> dict[str, Any]:
        """返回内部监控页需要的队列实时快照；Redis 异常时只标记不可用，不影响业务接口。"""
        if not self.enabled:
            return {
                "enabled": False,
                "available": False,
                "active_worker": False,
                "stream_depth": None,
                "pending": None,
                "dead_letter": None,
                "running": None,
                "retrying": None,
                "error": "redis_queue_disabled",
            }
        try:
            # 先刷新 Prometheus Gauge，再直接读取 Redis 当前状态，避免页面看到进程内旧值。
            self._refresh_metrics()
            pending = self._redis.xpending(self.stream_key, self.group_name)
            running, retrying = self._status_counts()
            return {
                "enabled": True,
                "available": True,
                "active_worker": self.has_active_worker(),
                "stream_depth": int(self._redis.xlen(self.stream_key)),
                "pending": int(pending.get("pending", 0) if isinstance(pending, dict) else pending[0]),
                "dead_letter": int(self._redis.xlen(self.dead_letter_key)),
                "running": running,
                "retrying": retrying,
                "error": None,
            }
        except Exception:
            return {
                "enabled": True,
                "available": False,
                "active_worker": False,
                "stream_depth": None,
                "pending": None,
                "dead_letter": None,
                "running": None,
                "retrying": None,
                "error": "redis_queue_unavailable",
            }

    def claim(self, block_ms: int | None = None) -> tuple[str, str, AgentExecutionJob, int] | None:
        """通过 XREADGROUP 领取新消息并将状态切换为 RUNNING。"""
        self._require_enabled()
        actual_block_ms = self.claim_block_ms if block_ms is None else max(0, int(block_ms))
        data = self._redis.xreadgroup(
            self.group_name,
            self.consumer_name,
            {self.stream_key: ">"},
            count=1,
            block=actual_block_ms,
        )
        return self._parse_claim(data)

    def recover_pending(self) -> tuple[str, str, AgentExecutionJob, int] | None:
        """通过 XAUTOCLAIM 领取超过可见性超时的 Pending 消息，支持 Worker 重启恢复。"""
        self._require_enabled()
        try:
            response = self._redis.xautoclaim(self.stream_key, self.group_name, self.consumer_name, self.visibility_timeout_ms, "0-0", count=1)
            messages = response[1] if isinstance(response, (tuple, list)) and len(response) > 1 else []
        except Exception as exc:
            # Redis 5 不支持 XAUTOCLAIM，使用 XPENDING + XCLAIM 实现等价的超时恢复。
            if "unknown command" not in str(exc).lower() and "xautoclaim" not in str(exc).lower():
                raise
            pending = self._redis.xpending_range(self.stream_key, self.group_name, "-", "+", 1)
            expired_ids = [item["message_id"] for item in pending if int(item.get("time_since_delivered", 0)) >= self.visibility_timeout_ms]
            messages = self._redis.xclaim(
                self.stream_key,
                self.group_name,
                self.consumer_name,
                self.visibility_timeout_ms,
                expired_ids,
            ) if expired_ids else []
        return self._parse_claim([(self.stream_key, messages)])

    def ack_success(self, stream_id: str, request_id: str, result: dict[str, Any]) -> None:
        """持久化安全结果后 XACK，确保已完成消息不会再次消费。"""
        degraded = bool(result.get("degraded"))
        self._save_status(request_id, {"status": "DEGRADED" if degraded else "SUCCESS", "result": result, "error_code": result.get("error_code") if degraded else None, "customer_status": "已转人工处理" if degraded else "处理完成"}, preserve_owner=True)
        self._redis.xack(self.stream_key, self.group_name, stream_id)
        self._refresh_metrics()

    def retry_or_dead_letter(self, stream_id: str, request_id: str, job: AgentExecutionJob, attempt: int, error_code: str) -> None:
        """失败任务重新投递；达到最大次数后写入无敏感信息的 DLQ 并 ACK 原消息。"""
        next_attempt = attempt + 1
        if next_attempt >= self.max_attempts:
            self._redis.xadd(self.dead_letter_key, {"request_id": request_id, "attempt": str(next_attempt), "error_code": error_code})
            self._save_status(request_id, {"status": "DEAD_LETTER", "error_code": error_code, "attempt": next_attempt, "customer_status": "已转人工处理"}, preserve_owner=True)
            self._redis.xack(self.stream_key, self.group_name, stream_id)
            self._refresh_metrics()
            return
        # 可重试失败仍处于 PENDING，避免 API/SSE 把一次临时失败误判为终态。
        self._save_status(request_id, {"status": "PENDING", "error_code": error_code, "attempt": next_attempt, "customer_status": "正在重试"}, preserve_owner=True)
        # ACK 原消息并写入新 stream message，避免同一 Pending 无限占用消费者。
        self._redis.xack(self.stream_key, self.group_name, stream_id)
        self._redis.xadd(self.stream_key, {"request_id": request_id, "job": json.dumps(job.model_dump(mode="json"), ensure_ascii=False), "attempt": str(next_attempt)})
        self._refresh_metrics()

    def get(self, request_id: str) -> dict[str, Any] | None:
        """返回请求状态；不回传队列原始任务和任何执行凭证。"""
        if not self.enabled:
            return None
        raw = self._redis.get(self._status_key(request_id))
        return json.loads(raw) if raw else None

    def _parse_claim(self, data: Any) -> tuple[str, str, AgentExecutionJob, int] | None:
        """解析 Redis Stream 响应并更新任务为 RUNNING。"""
        if not data:
            return None
        try:
            _, messages = data[0]
            stream_id, fields = messages[0]
            request_id = fields["request_id"]
            job = AgentExecutionJob.model_validate_json(fields["job"])
            attempt = int(fields.get("attempt", "0"))
            self._save_status(request_id, {"status": "RUNNING", "attempt": attempt, "customer_status": "正在处理"}, preserve_owner=True)
            self._refresh_metrics()
            return stream_id, request_id, job, attempt
        except (IndexError, KeyError, ValueError):
            return None

    def _ensure_group(self) -> None:
        """幂等创建 Consumer Group，已存在时不影响其他 Worker。"""
        try:
            self._redis.xgroup_create(self.stream_key, self.group_name, id="0-0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def _refresh_metrics(self) -> None:
        """从 Redis 实际 Stream 和 Pending 状态采样指标，失败时不影响队列流程。"""
        try:
            QUEUE_DEPTH.set(int(self._redis.xlen(self.stream_key)))
            DLQ_DEPTH.set(int(self._redis.xlen(self.dead_letter_key)))
            pending = self._redis.xpending(self.stream_key, self.group_name)
            QUEUE_PENDING.set(int(pending.get("pending", 0) if isinstance(pending, dict) else pending[0]))
            running, retries = self._status_counts()
            QUEUE_RUNNING.set(running)
            QUEUE_RETRIES.set(retries)
        except Exception:
            pass

    def _status_counts(self) -> tuple[int, int]:
        """扫描短 TTL 状态 Key，统计执行中与重试中的任务数量。"""
        running = retrying = 0
        for key in self._redis.scan_iter(match="agent:execution:status:*"):
            raw = self._redis.get(key)
            state = json.loads(raw) if raw else {}
            running += int(state.get("status") == "RUNNING")
            retrying += int(state.get("status") == "PENDING" and int(state.get("attempt", 0)) > 0)
        return running, retrying

    def _save_status(self, request_id: str, value: dict[str, Any], preserve_owner: bool = False) -> None:
        """设置带 TTL 的状态记录，并在状态迁移时保留结果归属摘要。"""
        if preserve_owner:
            current = self.get(request_id) or {}
            if current.get("owner"):
                value["owner"] = current["owner"]
        self._redis.setex(self._status_key(request_id), self.ttl_seconds, json.dumps(value, ensure_ascii=False))

    def _require_enabled(self) -> None:
        """明确拒绝未配置 Redis 的生产队列调用。"""
        if not self.enabled:
            raise RuntimeError("Agent execution Redis Stream queue is not available")

    @staticmethod
    def _idempotency_key(customer_id: int, idempotency_key: str) -> str:
        """生成按客户隔离的幂等 Key，防止不同客户相互命中。"""
        return f"agent:execution:idempotency:{customer_id}:{idempotency_key}"

    @staticmethod
    def _status_key(request_id: str) -> str:
        """生成任务状态 Redis Key。"""
        return f"agent:execution:status:{request_id}"

    @classmethod
    def _worker_heartbeat_key(cls, consumer_name: str) -> str:
        """生成仅含消费者名称的心跳键，不写入任何会话或身份信息。"""
        return f"{cls.worker_heartbeat_prefix}{consumer_name}"
