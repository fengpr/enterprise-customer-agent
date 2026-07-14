"""内部系统监控接口测试，覆盖鉴权、队列快照容错和关键指标聚合。"""

from fastapi.testclient import TestClient

import app
from services.observability import CACHE, DOWNSTREAM_REQUESTS


class FakeMonitorQueue:
    """用最小队列替身验证监控接口，不依赖真实 Redis 服务。"""

    def __init__(self, snapshot: dict):
        self._snapshot = snapshot

    def snapshot(self) -> dict:
        """返回预置队列快照。"""
        return self._snapshot


def _staff_user(_: str | None) -> dict:
    """构造 staff 身份，避免测试访问真实 Java 鉴权服务。"""
    return {"user_id": 1, "customer_id": 1, "display_name": "staff", "role": "staff"}


def test_staff_can_read_system_monitor(monkeypatch):
    """staff 用户可以读取 Worker、队列、DLQ 与指标聚合快照。"""
    monkeypatch.setattr(app, "_current_login_user", _staff_user)
    monkeypatch.setattr(
        app,
        "agent_execution_queue",
        FakeMonitorQueue({
            "enabled": True,
            "available": True,
            "active_worker": True,
            "stream_depth": 3,
            "pending": 1,
            "dead_letter": 0,
            "running": 2,
            "retrying": 1,
            "error": None,
        }),
    )

    response = TestClient(app.app).get("/api/staff/system/monitor", headers={"Authorization": "Bearer staff-token"})

    assert response.status_code == 200
    data = response.json()
    assert data["worker"]["active"] is True
    assert data["queue"]["stream_depth"] == 3
    assert data["queue"]["pending"] == 1
    assert data["dlq"]["count"] == 0
    assert data["updated_at"]


def test_customer_cannot_read_system_monitor(monkeypatch):
    """非 staff 角色不能访问内部运维数据。"""
    monkeypatch.setattr(app, "_current_login_user", lambda _: {"user_id": 2, "customer_id": 2, "role": "customer"})

    response = TestClient(app.app).get("/api/staff/system/monitor", headers={"Authorization": "Bearer customer-token"})

    assert response.status_code == 403


def test_monitor_returns_partial_snapshot_when_redis_unavailable(monkeypatch):
    """Redis 队列不可用时接口仍返回 200，并在 queue 中标记 unavailable。"""
    monkeypatch.setattr(app, "_current_login_user", _staff_user)
    monkeypatch.setattr(
        app,
        "agent_execution_queue",
        FakeMonitorQueue({
            "enabled": True,
            "available": False,
            "active_worker": False,
            "stream_depth": None,
            "pending": None,
            "dead_letter": None,
            "running": None,
            "retrying": None,
            "error": "redis_queue_unavailable",
        }),
    )

    response = TestClient(app.app).get("/api/staff/system/monitor", headers={"Authorization": "Bearer staff-token"})

    assert response.status_code == 200
    assert response.json()["queue"]["available"] is False
    assert response.json()["queue"]["error"] == "redis_queue_unavailable"


def test_monitor_aggregates_llm_and_cache_metrics(monkeypatch):
    """LLM timeout/429/circuit_open 与缓存命中率应从 Prometheus Registry 聚合到 JSON 响应。"""
    monkeypatch.setattr(app, "_current_login_user", _staff_user)
    monkeypatch.setattr(
        app,
        "agent_execution_queue",
        FakeMonitorQueue({
            "enabled": True,
            "available": True,
            "active_worker": True,
            "stream_depth": 0,
            "pending": 0,
            "dead_letter": 0,
            "running": 0,
            "retrying": 0,
            "error": None,
        }),
    )
    DOWNSTREAM_REQUESTS.labels("online_llm", "timeout").inc()
    DOWNSTREAM_REQUESTS.labels("online_llm", "rate_limit_429").inc()
    DOWNSTREAM_REQUESTS.labels("online_llm", "circuit_open").inc()
    CACHE.labels("rag_cache_hit", "hit").inc()
    CACHE.labels("rag_cache_hit", "miss").inc()

    response = TestClient(app.app).get("/api/staff/system/monitor", headers={"Authorization": "Bearer staff-token"})

    data = response.json()
    assert data["llm"]["timeout"] >= 1
    assert data["llm"]["rate_limit_429"] >= 1
    assert data["llm"]["circuit_open"] >= 1
    assert data["cache"]["rag"]["hit_rate"] is not None
    assert app._cache_metric("empty_cache_metric_for_test")["hit_rate"] is None
