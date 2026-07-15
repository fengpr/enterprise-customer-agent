from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from langchain_core.runnables import RunnableLambda

from agents.action_request import enrich_action_analysis
from agents.customer_service_agent import CustomerServiceAgent
from graphs.ticket_process_graph import build_ticket_process_graph
from schemas.intent_schema import IntentResult


ORDER_NO = "EC202606220001"


def _draft(
    message: str,
    *,
    action_type: str | None = "return_goods",
    user_goal: str = "action_request",
    action_slots: dict | None = None,
) -> IntentResult:
    """构造不依赖真实模型的意图草稿，验证后端确定性槽位归并。"""
    return IntentResult(
        intent="refund" if action_type == "return_goods" else "other",
        user_goal=user_goal,
        emotion="normal",
        order_related=bool(action_type),
        order_no=[],
        need_order_query=False,
        need_ticket=False,
        need_human=False,
        priority="medium",
        confidence=0.95,
        summary=message,
        action_type=action_type,
        action_slots=action_slots or {},
    )


@pytest.mark.parametrize(
    ("message", "expected_reason", "expected_method", "expected_time", "expected_missing"),
    [
        (
            "我要退货，商品有问题，上门取件，明天上午九点来。",
            "商品有问题",
            "pickup",
            "明天上午九点",
            [],
        ),
        (
            "商品质量问题，我要退货，明天下午上门取件。",
            "商品质量问题",
            "pickup",
            "明天下午",
            [],
        ),
        (
            "我要退货，因为尺码不合适，我自己寄回。",
            "尺码不合适",
            "self_ship",
            None,
            [],
        ),
        (
            "我要退货，商品质量问题，上门取件。",
            "商品质量问题",
            "pickup",
            None,
            ["pickup_time_window"],
        ),
    ],
)
def test_one_turn_extracts_all_available_return_slots(
    message: str,
    expected_reason: str,
    expected_method: str,
    expected_time: str | None,
    expected_missing: list[str],
):
    """槽位出现顺序不影响结果，只追问本轮确实没有提供的字段。"""
    analysis, pending = enrich_action_analysis(
        _draft(message),
        message=message,
        selected_order_no=ORDER_NO,
        pending_action_request=None,
    )

    assert analysis.action_slots["order_no"] == ORDER_NO
    assert analysis.action_slots["after_sale_reason"] == expected_reason
    assert analysis.action_slots["return_method"] == expected_method
    assert analysis.action_slots.get("pickup_time_window") == expected_time
    assert analysis.missing_slots == expected_missing
    assert analysis.next_action == ("collect_slots" if expected_missing else "create_ticket")
    assert pending and pending["flow_version"] == 2
    assert pending["flow_state"] == ("COLLECTING" if expected_missing else "READY")


def test_followup_can_fill_reason_method_and_time_together():
    """上一轮缺多个槽位时，下一条消息可以一次补齐，而不是只消费第一个槽位。"""
    first, pending = enrich_action_analysis(
        _draft("我要退货"),
        message="我要退货",
        selected_order_no=ORDER_NO,
        pending_action_request=None,
    )
    assert first.missing_slots == ["after_sale_reason", "return_method"]

    message = "商品有问题，上门取件，明天上午九点来。"
    completed, continued = enrich_action_analysis(
        _draft(message, action_type=None, user_goal="other"),
        message=message,
        selected_order_no=ORDER_NO,
        pending_action_request=pending,
    )

    assert completed.action_slots["after_sale_reason"] == "商品有问题"
    assert completed.action_slots["return_method"] == "pickup"
    assert completed.action_slots["pickup_time_window"] == "明天上午九点"
    assert completed.missing_slots == []
    assert completed.next_action == "create_ticket"
    assert continued and continued["flow_state"] == "READY"


def test_time_can_arrive_before_return_method_and_is_reused():
    """用户先提供时间、后选择上门取件时，时间槽位应保留并参与最终归并。"""
    now = datetime.utcnow()
    pending = {
        "pending_id": "PA-OUT-OF-ORDER",
        "status": "waiting_for_user_input",
        "action_type": "return_goods",
        "action_slots": {"order_no": ORDER_NO, "after_sale_reason": "商品有问题"},
        "missing_slots": ["return_method"],
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "completed": False,
    }

    with_time, pending_with_time = enrich_action_analysis(
        _draft("明天上午九点", action_type=None, user_goal="other"),
        message="明天上午九点",
        selected_order_no=ORDER_NO,
        pending_action_request=pending,
    )
    assert with_time.action_slots["pickup_time_window"] == "明天上午九点"
    assert with_time.missing_slots == ["return_method"]

    completed, _ = enrich_action_analysis(
        _draft("上门取件", action_type=None, user_goal="other"),
        message="上门取件",
        selected_order_no=ORDER_NO,
        pending_action_request=pending_with_time,
    )
    assert completed.action_slots["return_method"] == "pickup"
    assert completed.action_slots["pickup_time_window"] == "明天上午九点"
    assert completed.missing_slots == []


def test_self_ship_clears_previous_pickup_time():
    """客户改成自行寄回后，旧取件时间不能继续写入工单。"""
    pending = {
        "pending_id": "PA-CHANGE-METHOD",
        "status": "waiting_for_user_input",
        "action_type": "return_goods",
        "action_slots": {
            "order_no": ORDER_NO,
            "after_sale_reason": "商品有问题",
            "return_method": "pickup",
            "pickup_time_window": "明天上午九点",
        },
        "missing_slots": [],
        "updated_at": datetime.utcnow().isoformat(),
        "expires_at": (datetime.utcnow() + timedelta(minutes=30)).isoformat(),
        "completed": False,
    }

    analysis, _ = enrich_action_analysis(
        _draft("改成我自己寄回", action_type=None, user_goal="other"),
        message="改成我自己寄回",
        selected_order_no=ORDER_NO,
        pending_action_request=pending,
    )

    assert analysis.action_slots["return_method"] == "self_ship"
    assert "pickup_time_window" not in analysis.action_slots
    assert analysis.missing_slots == []


def test_standalone_unwanted_item_requires_action_confirmation():
    """单独说“不想要了”只表达原因，不能在没有明确动作授权时直接建单。"""
    message = "我不想要了"
    analysis, pending = enrich_action_analysis(
        _draft(message, action_type=None, user_goal="other"),
        message=message,
        selected_order_no=ORDER_NO,
        pending_action_request=None,
    )

    assert analysis.missing_slots == ["action_confirmation"]
    assert analysis.next_action == "collect_slots"
    assert pending and pending["confirmation_reason"] == "ambiguous_unwanted_item"
    assert pending["deferred_slots"]["after_sale_reason"] == "不想要"
    agent = CustomerServiceAgent.__new__(CustomerServiceAgent)
    question = agent._format_action_slot_question(
        {"analysis": analysis, "pending_action_request": pending, "tool_results": []}
    )
    assert ORDER_NO in question
    assert "发起退货申请" in question


def test_standalone_unwanted_item_without_order_confirms_then_collects_order():
    """没有选中订单时先确认退货诉求，确认后仍必须收集订单，不能继承长期历史。"""
    message = "我不想要了"
    analysis, pending = enrich_action_analysis(
        _draft(message, action_type=None, user_goal="other"),
        message=message,
        selected_order_no=None,
        pending_action_request=None,
    )
    assert analysis.missing_slots == ["action_confirmation"]
    assert pending and not pending["action_slots"].get("order_no")

    confirmed, _ = enrich_action_analysis(
        _draft("是", action_type=None, user_goal="other"),
        message="是",
        selected_order_no=None,
        pending_action_request=pending,
    )
    assert confirmed.action_slots["after_sale_reason"] == "不想要"
    assert "order_no" in confirmed.missing_slots
    assert confirmed.next_action == "collect_slots"


def test_low_confidence_llm_slots_cannot_make_action_ready():
    """低置信度模型槽位只能触发澄清，不能绕过确定性校验直接建单。"""
    message = "帮我处理一下"
    draft = _draft(
        message,
        action_slots={
            "order_no": ORDER_NO,
            "after_sale_reason": "模型猜测原因",
            "return_method": "pickup",
            "pickup_time_window": "明天上午九点",
        },
    ).model_copy(update={"confidence": 0.5, "order_no": [ORDER_NO]})

    analysis, pending = enrich_action_analysis(
        draft,
        message=message,
        selected_order_no=None,
        pending_action_request=None,
    )

    assert analysis.next_action == "collect_slots"
    assert set(analysis.missing_slots) >= {"order_no", "after_sale_reason", "return_method"}
    assert pending and set(pending["ambiguous_fields"]) >= {
        "order_no",
        "after_sale_reason",
        "return_method",
        "pickup_time_window",
    }


def test_unwanted_item_fills_reason_when_return_is_already_pending():
    """当前退货流程正在等原因时，“不想要了”应补原因而不是取消流程。"""
    now = datetime.utcnow()
    pending = {
        "pending_id": "PA-WAIT-REASON",
        "status": "waiting_for_user_input",
        "action_type": "return_goods",
        "action_slots": {"order_no": ORDER_NO},
        "missing_slots": ["return_reason", "return_method"],
        "updated_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "completed": False,
    }

    analysis, _ = enrich_action_analysis(
        _draft("我不想要了", action_type=None, user_goal="other"),
        message="我不想要了",
        selected_order_no=ORDER_NO,
        pending_action_request=pending,
    )

    assert analysis.action_slots["after_sale_reason"] == "我不想要了"
    assert analysis.missing_slots == ["return_method"]
    assert analysis.next_action == "collect_slots"


def test_explicit_cancel_is_not_saved_as_reason():
    """“不退了”是流程取消指令，不能被开放原因抽取覆盖。"""
    pending = {
        "pending_id": "PA-CANCEL",
        "status": "waiting_for_user_input",
        "action_type": "return_goods",
        "action_slots": {"order_no": ORDER_NO},
        "missing_slots": ["return_reason"],
        "updated_at": datetime.utcnow().isoformat(),
        "expires_at": (datetime.utcnow() + timedelta(minutes=30)).isoformat(),
        "completed": False,
    }

    analysis, cancelled = enrich_action_analysis(
        _draft("不退了", action_type=None, user_goal="other"),
        message="不退了",
        selected_order_no=ORDER_NO,
        pending_action_request=pending,
    )

    assert analysis.next_action == "cancel_pending"
    assert cancelled and cancelled["status"] == "cancelled"
    assert "after_sale_reason" not in analysis.action_slots


def test_complete_one_turn_request_validates_order_and_creates_one_ticket():
    """信息齐全的明确诉求应校验订单后直接幂等建单，并透传全部履约字段。"""
    message = "我要退货，商品有问题，上门取件，明天上午九点来。"
    draft = _draft(message)
    queried_orders: list[str] = []
    created_payloads: list[dict] = []

    def prepare_action(state):
        analysis, pending = enrich_action_analysis(
            state["analysis"],
            message=state["message"],
            selected_order_no=state.get("selected_order_no"),
            pending_action_request=state.get("pending_action_request"),
            conversation_context=state.get("conversation_context"),
        )
        return {"analysis": analysis, "pending_action_request": pending}

    def query_order(order_no, auth_token):
        queried_orders.append(order_no)
        return {
            "status": "success",
            "query_type": "order_detail",
            "order_no": order_no,
            "data": {
                "orderNo": order_no,
                "productName": "Smart Router AX3000",
                "orderStatus": "SIGNED",
                "afterSaleStatus": "NONE",
            },
        }

    def create_ticket(payload, auth_token):
        created_payloads.append(payload)
        return {
            "status": "success",
            "data": {
                "ticketNo": "T-DYNAMIC-RETURN",
                "status": "PENDING_ASSIGN",
                "ticketType": payload["ticketType"],
                "orderNo": payload["orderNo"],
            },
        }

    graph = build_ticket_process_graph(
        analyzer_chain=RunnableLambda(lambda _: draft),
        retrieve_knowledge=lambda _: [],
        query_order=query_order,
        query_customer_orders=lambda customer_id, auth_token: {"status": "success", "data": []},
        query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
        create_ticket=create_ticket,
        auto_assign_ticket=lambda ticket_no: {"status": "failed"},
        list_customer_tickets=lambda auth_token: {"status": "success", "data": []},
        query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
        urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
        prepare_action=prepare_action,
        compose_answer=lambda state: "退货工单已创建",
        log_tool_call=lambda tool_name, input_data, output_data: None,
    )

    result = graph.invoke(
        {
            "message": message,
            "customer_id": 1,
            "session_id": "S-DYNAMIC-RETURN",
            "selected_order_no": ORDER_NO,
            "tool_results": [],
            "citations": [],
        }
    )

    assert queried_orders == [ORDER_NO]
    assert len(created_payloads) == 1
    payload = created_payloads[0]
    assert payload["ticketType"] == "refund"
    assert payload["orderNo"] == ORDER_NO
    assert payload["returnMethod"] == "pickup"
    assert payload["pickupTimeWindow"] == "明天上午九点"
    assert payload["pickupStatus"] == "PREFERENCE_RECORDED"
    assert payload["idempotency_key"].startswith("agent-ticket:PA-")
    assert "after_sale_reason: 商品有问题" in payload["content"]
    assert result["ticket_result"]["status"] == "success"
    assert result["pending_action_request"]["status"] == "completed"


def test_duplicate_return_ticket_appends_new_information_instead_of_fake_registration():
    """命中在途退货工单时必须调用追加接口，不能只复用工单号后声称新信息已经登记。"""
    message = "我要退货，商品有问题，上门取件，后天下午三点来。"
    draft = _draft(message)
    appended: list[tuple[str, dict]] = []

    def prepare_action(state):
        analysis, pending = enrich_action_analysis(
            state["analysis"],
            message=state["message"],
            selected_order_no=state.get("selected_order_no"),
            pending_action_request=state.get("pending_action_request"),
            conversation_context=state.get("conversation_context"),
        )
        return {"analysis": analysis, "pending_action_request": pending}

    def append_information(ticket_no, payload, auth_token):
        appended.append((ticket_no, payload))
        return {
            "status": "success",
            "data": {
                "ticket": {
                    "ticketNo": ticket_no,
                    "ticketType": "refund",
                    "orderNo": ORDER_NO,
                    "status": "PENDING_ASSIGN",
                    "content": "业务动作：return_goods",
                },
                "updateMode": "APPLIED",
                "fulfillmentUpdated": True,
                "deduplicated": False,
            },
        }

    graph = build_ticket_process_graph(
        analyzer_chain=RunnableLambda(lambda _: draft),
        retrieve_knowledge=lambda _: [],
        query_order=lambda order_no, auth_token: {
            "status": "success",
            "query_type": "order_detail",
            "data": {"orderNo": order_no},
        },
        query_customer_orders=lambda customer_id, auth_token: {"status": "success", "data": []},
        query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
        create_ticket=lambda payload, auth_token: pytest.fail("已有工单时不应重复创建"),
        auto_assign_ticket=lambda ticket_no: {"status": "failed"},
        list_customer_tickets=lambda auth_token: {
            "status": "success",
            "data": [{
                "ticketNo": "T-EXISTING",
                "ticketType": "refund",
                "orderNo": ORDER_NO,
                "status": "PENDING_ASSIGN",
                "content": "业务动作：return_goods",
            }],
        },
        query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
        urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
        prepare_action=prepare_action,
        compose_answer=lambda state: "已处理",
        log_tool_call=lambda tool_name, input_data, output_data: None,
        append_ticket_information=append_information,
    )

    result = graph.invoke({
        "message": message,
        "customer_id": 1,
        "session_id": "S-SUPPLEMENT",
        "selected_order_no": ORDER_NO,
        "tool_results": [],
        "citations": [],
    })

    assert len(appended) == 1
    ticket_no, payload = appended[0]
    assert ticket_no == "T-EXISTING"
    assert payload["afterSaleReason"] == "商品有问题"
    assert payload["pickupTimeWindow"] == "后天下午三点"
    assert payload["idempotency_key"].startswith("agent-ticket-supplement:PA-")
    assert result["ticket_result"]["deduplicated"] is True
    assert result["ticket_result"]["supplement_result"]["data"]["updateMode"] == "APPLIED"


def test_processing_ticket_reply_says_pickup_change_needs_review():
    """处理中工单只能登记取件变更申请，客户回复不得声称原取件安排已被直接修改。"""
    agent = CustomerServiceAgent.__new__(CustomerServiceAgent)
    analysis = _draft(
        "改成十分钟后取件",
        action_slots={
            "order_no": ORDER_NO,
            "after_sale_reason": "商品有问题",
            "return_method": "pickup",
            "pickup_time_window": "十分钟后",
        },
    ).model_copy(update={"need_ticket": True, "need_human": True, "next_action": "create_ticket"})
    state = {
        "analysis": analysis,
        "ticket_result": {
            "status": "success",
            "deduplicated": True,
            "data": {"ticketNo": "T-EXISTING", "status": "PROCESSING", "orderNo": ORDER_NO},
            "supplement_result": {
                "status": "success",
                "data": {"updateMode": "REVIEW_REQUIRED", "fulfillmentUpdated": False},
            },
        },
        "tool_results": [],
        "citations": [],
        "risk_reasons": [],
    }

    answer = agent._build_customer_message(state)

    assert "仅登记为变更申请" in answer
    assert "不会直接覆盖原安排" in answer
    assert "取件时间偏好已更新" not in answer
