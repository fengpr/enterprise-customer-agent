from __future__ import annotations

from repositories.chat_repository import ChatMessageRepository, ChatSessionRepository
from services.staff_presence_service import StaffPresenceService


class FakeRedis:
    """用于验证心跳双 TTL 语义的最小 Redis 替身。"""

    def __init__(self) -> None:
        self.values: dict[str, tuple[int, str]] = {}

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = (ttl, value)

    def exists(self, key: str) -> bool:
        return key in self.values

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.values.pop(key, None)


def test_handoff_lifecycle_is_idempotent_and_independent_from_session_status(tmp_path) -> None:
    """重复转人工和 AI 回写不能破坏人工排队或当前坐席归属。"""
    repository = ChatSessionRepository(db_path=str(tmp_path / "handoff.db"))
    session = repository.create(7, "需要人工")
    first = repository.request_handoff(session["session_id"], "customer_requested")
    assert first and first["handoff_status"] == "PENDING"

    accepted = repository.accept_handoff(session["session_id"], "staff-1", "客服小美")
    assert accepted and accepted["handoff_status"] == "ACTIVE"
    assert repository.accept_handoff(session["session_id"], "staff-2", "客服小李") is None

    repeated = repository.request_handoff(session["session_id"], "duplicate")
    assert repeated and repeated["handoff_status"] == "ACTIVE"
    assert repeated["human_assigned_staff_id"] == "staff-1"

    repository.update_after_agent_reply(session["session_id"], {"summary": "继续使用智能助手"}, "AI_REPLIED")
    current = repository.get_by_session_no(session["session_id"])
    assert current and current["status"] == "AI_REPLIED"
    assert current["handoff_status"] == "ACTIVE"

    assert repository.close_handoff(session["session_id"], "staff-2") is None
    closed = repository.close_handoff(session["session_id"], "staff-1")
    assert closed and closed["handoff_status"] == "CLOSED"

    reopened = repository.request_handoff(session["session_id"], "again")
    assert reopened and reopened["handoff_status"] == "PENDING"
    assert reopened["human_assigned_staff_id"] is None


def test_customer_cancel_delete_and_safe_incremental_messages(tmp_path) -> None:
    """待接入会话必须先取消再删除，客户增量消息不得泄露内部扩展字段。"""
    sessions = ChatSessionRepository(db_path=str(tmp_path / "safe.db"))
    messages = ChatMessageRepository(sessions)
    session = sessions.create(9, "安全消息")
    session_id = session["session_id"]
    sessions.request_handoff(session_id, "customer_requested")
    assert not sessions.soft_delete_for_customer(session_id, 9)

    messages.save(session_id, "system", "不可见", extra_data={"internal_suggestion": "secret"})
    visible = messages.save(
        session_id,
        "staff",
        "客户可见回复",
        sender_id="staff-secret",
        extra_data={"customer_visible": True, "internal_suggestion": "secret", "message_source": "manual_handoff_reply"},
    )
    safe = messages.list_by_session_for_customer(session_id, 9, after_message_id=visible["id"] - 1)
    assert len(safe) == 1
    assert safe[0]["sender_id"] is None
    assert "internal_suggestion" not in safe[0]["extra_data"]

    cancelled = sessions.cancel_pending_handoff(session_id, 9)
    assert cancelled and cancelled["handoff_status"] == "CLOSED"
    assert sessions.soft_delete_for_customer(session_id, 9)


def test_presence_uses_online_and_recovery_grace_keys() -> None:
    """接单在线 TTL 与失联回收宽限 TTL 必须分离，并可在退出时一并清理。"""
    redis = FakeRedis()
    presence = StaffPresenceService(client=redis, ttl_seconds=30, grace_seconds=60)
    assert presence.heartbeat("staff-1")
    assert redis.values["staff-presence:staff-1"][0] == 30
    assert redis.values["staff-presence-grace:staff-1"][0] == 60
    assert presence.is_online("staff-1")
    assert presence.is_within_grace("staff-1")
    presence.remove("staff-1")
    assert not presence.is_online("staff-1")
    assert not presence.is_within_grace("staff-1")
