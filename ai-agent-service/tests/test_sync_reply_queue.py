"""验证同步回复接口只编排队列任务，不直接执行 Agent。"""

from types import SimpleNamespace

import app
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from schemas.intent_schema import AgentReplyRequest
from services.stream_event_service import StreamEventService


class FakeQueue:
    """模拟队列状态，验证同步入口的短等待与复用语义。"""

    enabled = True

    def __init__(self, state, active_worker: bool = True):
        self.state = state
        self.jobs = []
        self.active_worker = active_worker

    def enqueue(self, job, owner):
        self.jobs.append((job, owner))
        return self.state.get("request_id", job.request_id)

    def get(self, request_id):
        return self.state

    def has_active_worker(self):
        return self.active_worker


def _request():
    """构造仅供限流主体读取的最小 Request 替身。"""
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))


def test_sync_reply_returns_completed_queue_result(monkeypatch):
    """任务短等待完成时，接口应返回兼容的 AgentReply 结果。"""
    queue = FakeQueue({"status": "SUCCESS", "result": {"answer": "已处理"}})
    monkeypatch.setattr(app, "agent_execution_queue", queue)
    monkeypatch.setattr(app, "_current_login_user", lambda _: {"customer_id": 8})

    result = app.reply(AgentReplyRequest(message="查询订单"), _request(), "Bearer token", "same-key")

    assert result["answer"] == "已处理"
    assert queue.jobs[0][0].customer_id == 8
    assert queue.jobs[0][0].idempotency_key == "same-key"


def test_sync_reply_timeout_returns_202_queue_status(monkeypatch):
    """短等待未完成时，应返回 202 和客户可见排队状态。"""
    queue = FakeQueue({"status": "PENDING"})
    monkeypatch.setattr(app, "agent_execution_queue", queue)
    monkeypatch.setattr(app, "_current_login_user", lambda _: {"customer_id": 8})
    monkeypatch.setenv("AGENT_SYNC_WAIT_SECONDS", "0")

    result = app.reply(AgentReplyRequest(message="查询订单"), _request(), "Bearer token", "same-key")

    assert isinstance(result, JSONResponse)
    assert result.status_code == 202
    assert b'"queued":true' in result.body


def test_sync_reply_returns_terminal_degraded_when_worker_missing(monkeypatch):
    """没有活跃 Worker 时不能继续返回排队中，避免客户请求长期悬挂。"""
    queue = FakeQueue({"status": "PENDING"}, active_worker=False)
    monkeypatch.setattr(app, "agent_execution_queue", queue)
    monkeypatch.setattr(app, "_current_login_user", lambda _: {"customer_id": 8})

    result = app.reply(AgentReplyRequest(message="查询订单"), _request(), "Bearer token", "same-key")

    assert isinstance(result, JSONResponse)
    assert result.status_code == 503
    assert b"AGENT_WORKER_UNAVAILABLE" in result.body
    assert queue.jobs == []


def test_result_query_rejects_other_owner(monkeypatch):
    """结果查询必须校验 Token 摘要归属，不能跨客户读取请求状态。"""
    queue = FakeQueue({"status": "PENDING", "owner": "other-owner"})
    monkeypatch.setattr(app, "agent_execution_queue", queue)
    monkeypatch.setattr(app, "_queue_owner", lambda _: "current-owner")

    with pytest.raises(HTTPException) as exc_info:
        app.get_queued_reply("request-1", "Bearer token")
    assert exc_info.value.status_code == 403


def test_handoff_message_is_persisted_without_creating_agent_queue_job(monkeypatch):
    """人工页签消息应走专用落库接口，不能创建 SSE/Agent 队列任务。"""
    called = {}

    class FakeExecutionService:
        def execute(self, payload):
            called["payload"] = payload
            return {"session_id": "session-human", "answer": "已记录", "customer_message": "已记录"}

    monkeypatch.setattr(app, "agent_execution_service", FakeExecutionService())
    monkeypatch.setattr(
        app,
        "_current_login_user",
        lambda _: {"customer_id": 8, "display_name": "测试客户", "role": "customer"},
    )

    result = app.send_handoff_message(
        AgentReplyRequest(message="请优先跟进", session_id="session-human", route_target="ai"),
        "Bearer token",
    )

    assert result["session_id"] == "session-human"
    assert called["payload"].route_target == "human"
    assert called["payload"].customer_id == 8


def test_sse_wait_window_returns_reconnectable_queued_event(monkeypatch):
    """单次 SSE 等待到期时应返回非终态 queued，前端可用同一幂等键继续订阅。"""
    queue = FakeQueue({"status": "PENDING"})
    events = StreamEventService(redis_client=False)
    events._memory_events.clear()
    monkeypatch.setattr(app, "agent_execution_queue", queue)
    monkeypatch.setattr(app, "stream_event_service", events)
    monkeypatch.setattr(app, "_current_login_user", lambda _: {"customer_id": 8})
    monkeypatch.setenv("AGENT_QUEUE_MAX_WAIT_SECONDS", "0")

    with TestClient(app.app) as client:
        response = client.post(
            "/api/agent/reply/stream",
            json={"message": "查询退货规则"},
            headers={"Authorization": "Bearer token", "Idempotency-Key": "same-stream-key"},
        )

    assert response.status_code == 200
    assert 'event: queued' in response.text
    assert '请求仍在后台处理中' in response.text
    assert queue.jobs[0][0].idempotency_key == "same-stream-key"


def test_sse_returns_degraded_when_worker_missing(monkeypatch):
    """SSE 在无 Worker 时应立即返回 degraded 终态，而不是生成不可消费的队列任务。"""
    queue = FakeQueue({"status": "PENDING"}, active_worker=False)
    events = StreamEventService(redis_client=False)
    events._memory_events.clear()
    monkeypatch.setattr(app, "agent_execution_queue", queue)
    monkeypatch.setattr(app, "stream_event_service", events)
    monkeypatch.setattr(app, "_current_login_user", lambda _: {"customer_id": 8})

    with TestClient(app.app) as client:
        response = client.post(
            "/api/agent/reply/stream",
            json={"message": "查询退货规则"},
            headers={"Authorization": "Bearer token", "Idempotency-Key": "same-stream-key"},
        )

    assert response.status_code == 200
    assert "event: degraded" in response.text
    assert "AGENT_WORKER_UNAVAILABLE" in response.text
    assert queue.jobs == []
