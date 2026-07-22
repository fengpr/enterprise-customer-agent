from __future__ import annotations

import json

from fastapi.testclient import TestClient

import app
from repositories.chat_repository import ChatMessageRepository, ChatSessionRepository
from repositories.staff_handoff_audit_repository import StaffHandoffAuditRepository


def test_handoff_recent_window_and_explicit_history_page(tmp_path) -> None:
    """座席默认只获取最近窗口，更早消息必须通过独立分页能力读取。"""
    sessions = ChatSessionRepository(db_path=str(tmp_path / "handoff-context.db"))
    messages = ChatMessageRepository(sessions)
    session = sessions.create(1001, "人工交接测试")
    session_id = session["session_id"]

    for index in range(16):
        messages.save(session_id, "customer" if index % 2 == 0 else "ai", f"可见消息-{index + 1}")

    recent = messages.list_recent_by_session(session_id, limit=12)
    assert [message["content"] for message in recent] == [f"可见消息-{index}" for index in range(5, 17)]
    assert messages.has_message_before(session_id, recent[0]["id"])

    earlier = messages.list_before_message_id(session_id, recent[0]["id"], limit=30)
    assert [message["content"] for message in earlier] == [f"可见消息-{index}" for index in range(1, 5)]
    assert not messages.has_message_before(session_id, earlier[0]["id"])


def test_handoff_history_audit_does_not_store_conversation_content(tmp_path) -> None:
    """历史展开审计只保留会话、座席、游标和数量，不能把聊天正文重复写入审计表。"""
    sessions = ChatSessionRepository(db_path=str(tmp_path / "handoff-audit.db"))
    session = sessions.create(1002, "审计测试")
    audit = StaffHandoffAuditRepository(sessions)

    audit.record_history_access(
        session_no=session["session_id"],
        staff_id="staff-7",
        before_message_id=42,
        returned_count=12,
    )

    with sessions.database.connection() as conn:
        row = conn.execute(
            "SELECT session_no, staff_id, action, metadata FROM staff_handoff_audit_log"
        ).fetchone()

    assert row["session_no"] == session["session_id"]
    assert row["staff_id"] == "staff-7"
    assert row["action"] == "expand_history"
    assert json.loads(row["metadata"]) == {"before_message_id": 42, "returned_count": 12}


class _FakeHandoffMessages:
    """模拟会话消息仓储，验证接口不把增量参数变成历史绕过通道。"""

    def __init__(self) -> None:
        self.messages = [
            {
                "id": index,
                "session_id": "S-HANDOFF",
                "sender_type": "customer" if index % 2 else "ai",
                "sender_id": None,
                "content": f"消息-{index}",
                "message_type": "text",
                "extra_data": {"customer_visible": True},
                "created_at": "2026-07-22T10:00:00Z",
            }
            for index in range(1, 21)
        ]
        self.incremental_called = False

    def list_recent_by_session(self, _session_no: str, limit: int) -> list[dict]:
        return self.messages[-limit:]

    def list_by_session(self, _session_no: str, after_message_id: int = 0) -> list[dict]:
        self.incremental_called = True
        return [item for item in self.messages if item["id"] > after_message_id]

    def list_before_message_id(self, _session_no: str, before_message_id: int, limit: int) -> list[dict]:
        return [item for item in self.messages if item["id"] < before_message_id][-limit:]

    def has_message_before(self, _session_no: str, message_id: int) -> bool:
        return any(item["id"] < message_id for item in self.messages)


def test_staff_handoff_defaults_to_window_and_audits_expansion(monkeypatch) -> None:
    """待接入只看摘要；已接入默认最近窗口，显式展开才读取更早记录并审计。"""
    fake_messages = _FakeHandoffMessages()
    audit_events: list[dict] = []
    session = {
        "session_id": "S-HANDOFF",
        "handoff_status": "ACTIVE",
        "human_assigned_staff_id": "9",
        "intent": "status_query",
        "ai_summary": "客户希望了解订单状态。",
    }

    monkeypatch.setattr(app, "_require_staff_user", lambda _: {"user_id": 9, "role": "staff"})
    monkeypatch.setattr(app, "_get_staff_visible_handoff_session", lambda *_: session)
    monkeypatch.setattr(app, "_get_staff_owned_handoff_session", lambda *_: session)
    monkeypatch.setattr(app, "_handoff_session_payload", lambda value: value)
    monkeypatch.setattr(app, "_staff_handoff_summary", lambda _: {"title": "交接", "ai_summary": "客户希望了解订单状态。"})
    monkeypatch.setattr(app, "chat_messages", fake_messages)
    monkeypatch.setattr(
        app.staff_handoff_audit,
        "record_history_access",
        lambda **kwargs: audit_events.append(kwargs),
    )
    client = TestClient(app.app)

    # 恶意使用很小游标时仍只得到默认 12 条，而不是完整历史。
    detail = client.get("/api/staff/handoff/sessions/S-HANDOFF", params={"after_message_id": 1})
    assert detail.status_code == 200
    assert [item["id"] for item in detail.json()["messages"]] == list(range(9, 21))
    assert not fake_messages.incremental_called

    history = client.get("/api/staff/handoff/sessions/S-HANDOFF/history", params={"before_message_id": 9})
    assert history.status_code == 200
    assert [item["id"] for item in history.json()["messages"]] == list(range(1, 9))
    assert audit_events == [
        {"session_no": "S-HANDOFF", "staff_id": "9", "before_message_id": 9, "returned_count": 8}
    ]
