"""验证工单客户沟通接口必须由当前处理坐席操作。"""

from fastapi.testclient import TestClient

import app


def _staff_user(_: str | None) -> dict:
    """构造已认证坐席身份，避免测试依赖 Java 登录服务。"""
    return {"user_id": 101, "customer_id": 1, "display_name": "客服小王", "role": "staff"}


def _ticket(handler_id: int | None) -> dict:
    """构造 Java 工单详情响应中的必要字段。"""
    return {
        "ticketNo": "T202607220001",
        "handlerId": handler_id,
        "status": "PROCESSING",
    }


def test_unassigned_ticket_cannot_generate_or_send_customer_reply(monkeypatch) -> None:
    """未领取工单即使对坐席可见，也不能生成或发送客户可见话术。"""
    monkeypatch.setattr(app, "_current_login_user", _staff_user)
    monkeypatch.setattr(app, "_get_staff_ticket", lambda *_: _ticket(None))
    # 归属校验必须在查找客户会话之前执行，避免无权坐席探测工单关联会话。
    monkeypatch.setattr(app, "_find_ticket_chat_context", lambda *_: (_ for _ in ()).throw(AssertionError("不应查询会话")))
    client = TestClient(app.app)

    draft = client.post(
        "/api/staff/tickets/T202607220001/reply/draft",
        json={"close_reason": "已处理"},
        headers={"Authorization": "Bearer staff-token"},
    )
    send = client.post(
        "/api/staff/tickets/T202607220001/reply/send",
        json={"message": "您好，问题已处理。"},
        headers={"Authorization": "Bearer staff-token"},
    )

    assert draft.status_code == 403
    assert send.status_code == 403
    assert "先领取工单" in draft.json()["detail"]


def test_other_staff_ticket_cannot_generate_or_send_customer_reply(monkeypatch) -> None:
    """其他坐席名下的工单不能被当前坐席用于草稿或客户回复。"""
    monkeypatch.setattr(app, "_current_login_user", _staff_user)
    monkeypatch.setattr(app, "_get_staff_ticket", lambda *_: _ticket(202))
    monkeypatch.setattr(app, "_find_ticket_chat_context", lambda *_: (_ for _ in ()).throw(AssertionError("不应查询会话")))
    client = TestClient(app.app)

    response = client.post(
        "/api/staff/tickets/T202607220001/reply/draft",
        json={"close_reason": "已处理"},
        headers={"Authorization": "Bearer staff-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "只能处理自己名下的工单"


def test_ticket_owner_sends_reply_to_ticket_linked_session(monkeypatch) -> None:
    """座席确认发送后必须写入工单关联会话，不能投递到当前页面的任意会话。"""
    from types import SimpleNamespace

    saved: dict = {}
    statuses: list[tuple[str, str]] = []

    def save_message(**kwargs):
        """记录持久化参数，验证客户可见消息的会话归属和来源。"""
        saved.update(kwargs)
        return {"id": 88, **kwargs}

    monkeypatch.setattr(app, "_current_login_user", _staff_user)
    monkeypatch.setattr(app, "_get_staff_ticket", lambda *_: _ticket(101))
    monkeypatch.setattr(
        app,
        "_find_ticket_chat_context",
        lambda *_: {"session": {"session_id": "S-linked-ticket-session"}},
    )
    monkeypatch.setattr(app, "chat_messages", SimpleNamespace(save=save_message))
    monkeypatch.setattr(
        app,
        "chat_sessions",
        SimpleNamespace(update_status=lambda session_id, status: statuses.append((session_id, status))),
    )
    client = TestClient(app.app)

    response = client.post(
        "/api/staff/tickets/T202607220001/reply/send",
        json={"message": "您好，已为您更新处理进度。"},
        headers={"Authorization": "Bearer staff-token"},
    )

    assert response.status_code == 200
    assert response.json()["session_id"] == "S-linked-ticket-session"
    assert saved["session_no"] == "S-linked-ticket-session"
    assert saved["sender_type"] == "staff"
    assert saved["extra_data"]["ticket_no"] == "T202607220001"
    assert saved["extra_data"]["route_target"] == "human"
    assert saved["extra_data"]["customer_visible"] is True
    assert saved["extra_data"]["message_source"] == "staff_confirmed_reply"
    assert statuses == [("S-linked-ticket-session", "STAFF_REPLIED")]


def test_ticket_owner_can_generate_customer_reply_draft(monkeypatch) -> None:
    """当前处理人仍可正常生成草稿，避免归属校验误伤正常工作流。"""
    monkeypatch.setattr(app, "_current_login_user", _staff_user)
    monkeypatch.setattr(app, "_get_staff_ticket", lambda *_: _ticket(101))
    monkeypatch.setattr(
        app,
        "_find_ticket_chat_context",
        lambda *_: {"session": {"session_id": "S202607220001"}},
    )
    # 权限测试不依赖真实模型或网络；模型事实约束由独立草稿服务测试覆盖。
    from types import SimpleNamespace

    monkeypatch.setattr(
        app,
        "staff_reply_draft_service",
        SimpleNamespace(
            generate_with_metadata=lambda **_: SimpleNamespace(
                draft_message="您好，问题已处理完成，后续进展会在当前会话同步。",
                generation_mode="fallback",
                fallback_reason="not_configured",
            )
        ),
    )
    client = TestClient(app.app)

    response = client.post(
        "/api/staff/tickets/T202607220001/reply/draft",
        json={"close_reason": "问题已处理完成"},
        headers={"Authorization": "Bearer staff-token"},
    )

    assert response.status_code == 200
    assert response.json()["session_id"] == "S202607220001"
    assert "问题已处理完成" in response.json()["draft_message"]


def test_ticket_customer_mismatch_cannot_write_to_linked_session(monkeypatch) -> None:
    """工单 externalSessionNo 指向其他客户会话时必须阻断，不能把座席消息写错会话。"""
    from fastapi import HTTPException
    from types import SimpleNamespace

    monkeypatch.setattr(
        app,
        "chat_sessions",
        SimpleNamespace(get_by_session_no=lambda _: {"session_id": "S-other", "customer_id": 9}),
    )

    try:
        app._find_ticket_chat_context(
            "T202607220099",
            {"ticketNo": "T202607220099", "customerId": 7, "externalSessionNo": "S-other"},
        )
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("客户归属不一致时不应返回会话上下文")


def test_ticket_status_sync_writes_customer_visible_event_to_linked_human_session(monkeypatch) -> None:
    """Java 状态回调必须写回 externalSessionNo 对应的人工会话，且可被安全重放。"""
    from types import SimpleNamespace

    saved: list[dict] = []
    monkeypatch.setattr(app, "chat_sessions", SimpleNamespace(
        get_by_session_no=lambda _: {"session_id": "S-linked", "customer_id": 7},
        update_status=lambda *_: None,
    ))
    monkeypatch.setattr(app, "chat_messages", SimpleNamespace(
        list_by_session=lambda _: [],
        save=lambda *args, **kwargs: saved.append({"args": args, "kwargs": kwargs}),
    ))

    result = app.sync_ticket_status_to_conversation(
        {"ticketNo": "T202607220099", "customerId": 7, "externalSessionNo": "S-linked", "status": "PROCESSING"},
        app.AGENT_INTERNAL_SECRET,
    )

    assert result == {"status": "success", "session_id": "S-linked"}
    assert saved[0]["args"][:3] == ("S-linked", "system", "工单 T202607220099 状态已更新为“处理中”。")
    assert saved[0]["kwargs"]["extra_data"]["route_target"] == "human"
