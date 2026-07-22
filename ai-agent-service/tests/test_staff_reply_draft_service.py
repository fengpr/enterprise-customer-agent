"""验证坐席客户话术草稿只使用真实、脱敏的业务事实。"""

from types import SimpleNamespace

import app
from services.staff_reply_draft_service import StaffReplyDraftService


def _ticket() -> dict:
    """构造包含退货原因与取件偏好的已核验工单事实。"""
    return {
        "ticketNo": "T202607220888",
        "ticketType": "refund",
        "status": "PROCESSING",
        "orderNo": "EC202607160010",
        "title": "退货申请",
        "content": "商品存在质量问题，联系电话 13812345678，地址广东省深圳市南山区科技园15号",
        "aiSummary": "客户申请退货，等待售后核实",
        "returnMethod": "pickup",
        "pickupTimeWindow": "明天上午九点",
        "pickupStatus": "PENDING",
        "customerId": 999,
    }


def test_llm_draft_uses_desensitized_ticket_facts_and_preserves_processing_result(monkeypatch) -> None:
    """模型草稿应获得退货原因等真实事实，但不能获得客户 ID、手机号和地址。"""
    service = StaffReplyDraftService(model=object(), invoker=SimpleNamespace())
    captured: dict = {}

    def fake_generate(facts: dict) -> str:
        captured.update(facts)
        return "很抱歉这次商品问题给您带来困扰。我们已记录您选择上门取件及明天上午九点的偏好，售后同事会继续核实并同步进展。"

    monkeypatch.setattr(service, "_generate_with_llm", fake_generate)
    draft, mode = service.generate(
        ticket=_ticket(),
        processing_result="已登记退货原因，等待售后审核。",
        messages=[{"sender_type": "customer", "content": "商品有问题，请尽快处理"}],
    )

    assert mode == "llm"
    assert "售后同事" in draft
    assert captured["ticket"]["order_no"] == "EC202607160010"
    assert captured["ticket"]["return_method"] == "pickup"
    assert captured["ticket"]["pickup_time_window"] == "明天上午九点"
    assert "customerId" not in captured["ticket"]
    assert "13812345678" not in captured["ticket"]["customer_request"]
    assert "科技园15号" not in captured["ticket"]["customer_request"]
    assert captured["processing_result"] == "已登记退货原因，等待售后审核。"


def test_invalid_llm_draft_falls_back_without_false_commitment_or_handoff_wording(monkeypatch) -> None:
    """模型输出未核验承诺或重复转交话术时必须回退，避免坐席接手后仍让客户等待。"""
    service = StaffReplyDraftService(model=object(), invoker=SimpleNamespace())
    monkeypatch.setattr(service, "_generate_with_llm", lambda _: "请您等待工作人员处理，我们保证退款，并且已安排取件。")

    draft, mode = service.generate(ticket=_ticket(), processing_result="正在核实处理。", messages=[])

    assert mode == "fallback"
    assert "正在核实处理。" in draft
    assert "保证退款" not in draft
    assert "已安排取件" not in draft
    assert "等待工作人员" not in draft


def test_staff_draft_api_returns_llm_generation_mode(monkeypatch) -> None:
    """草稿接口应把模型生成结果返回给坐席，但不会自动写入客户会话。"""
    monkeypatch.setattr(app, "_current_login_user", lambda _: {"user_id": 101, "role": "staff", "display_name": "客服小王"})
    ticket = _ticket() | {"handlerId": 101}
    monkeypatch.setattr(app, "_get_staff_ticket", lambda *_: ticket)
    monkeypatch.setattr(app, "_find_ticket_chat_context", lambda *_: {"session": {"session_id": "S202607220888"}})
    monkeypatch.setattr(app.chat_messages, "list_by_session", lambda _: [{"sender_type": "customer", "content": "商品有问题"}])
    monkeypatch.setattr(
        app,
        "staff_reply_draft_service",
        SimpleNamespace(generate=lambda **_: ("很抱歉给您带来不便，我们已记录商品质量问题并继续为您核实。", "llm")),
    )

    from fastapi.testclient import TestClient

    response = TestClient(app.app).post(
        "/api/staff/tickets/T202607220888/reply/draft",
        json={"close_reason": "已登记退货原因，等待审核。"},
        headers={"Authorization": "Bearer staff-token"},
    )

    assert response.status_code == 200
    assert response.json()["generation_mode"] == "llm"
    assert "商品质量问题" in response.json()["draft_message"]
