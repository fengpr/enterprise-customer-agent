"""可观测性闭环测试：指标输出、关联 Header 与敏感字段过滤。"""

from fastapi.testclient import TestClient

from app import app
from services.observability import current_context, set_request_context
from services.stream_event_service import StreamEventService


def test_metrics_endpoint_exposes_prometheus_format():
    """Prometheus 端点必须可抓取标准指标。"""
    response = TestClient(app).get("/metrics")
    assert response.status_code == 200
    assert "agent_http_requests_total" in response.text


def test_trace_headers_are_generated_and_returned():
    """请求缺少关联 Header 时服务生成安全随机 ID 并回传。"""
    response = TestClient(app).get("/health")
    assert response.headers["X-Request-ID"].startswith("req-")
    assert len(response.headers["X-Trace-ID"]) >= 16


def test_context_and_event_payload_do_not_keep_authorization():
    """上下文仅保存 ID，事件缓冲必须剔除认证信息。"""
    set_request_context("req-safe", "trace-safe")
    assert current_context() == {"request_id": "req-safe", "trace_id": "trace-safe"}
    service = StreamEventService(redis_client=None)
    service._memory_events.clear()
    event = service.publish("req-safe", "delta", {"text": "ok", "Authorization": "Bearer secret"})
    assert "Authorization" not in event["payload"]
    assert "secret" not in service.to_sse(event)
