import re
from typing import Any


ORDER_NO_PATTERN = re.compile(r"(?<![A-Za-z])(?:EC)?\d{10,18}", flags=re.IGNORECASE)
TICKET_NO_PATTERN = re.compile(r"T\d{12,24}", flags=re.IGNORECASE)


def build_conversation_context(
    *,
    messages: list[dict[str, Any]],
    pending_action_request: dict[str, Any] | None,
    selected_order_no: str | None,
    selected_ticket_no: str | None,
    max_messages: int = 12,
) -> dict[str, Any]:
    """从同一会话最近消息中提取安全上下文，供 Agent 解析多轮指代。"""
    recent_messages = messages[-max_messages:]
    context: dict[str, Any] = {
        "last_order": None,
        "last_product": None,
        "last_ticket": None,
        "last_action": None,
        "safe_context_summary": "",
        "debug_context": {
            "message_window": [message.get("id") for message in recent_messages],
            "evidence": [],
        },
    }

    _apply_history_context(context, recent_messages)
    if selected_order_no:
        _set_context_value(
            context,
            "last_order",
            selected_order_no,
            source="selected_order",
            confidence=0.85,
            evidence={"kind": "selected_order"},
        )
    if selected_ticket_no:
        _set_context_value(
            context,
            "last_ticket",
            selected_ticket_no,
            source="selected_ticket",
            confidence=0.85,
            evidence={"kind": "selected_ticket"},
        )
    _apply_pending_context(context, pending_action_request)
    context["safe_context_summary"] = _build_safe_summary(context)
    return context


def _apply_history_context(context: dict[str, Any], messages: list[dict[str, Any]]) -> None:
    """按时间倒序读取历史事实，优先保留高可信工具结果。"""
    for message in reversed(messages):
        extra_data = message.get("extra_data") or {}
        message_id = message.get("id")
        if message.get("sender_type") == "customer":
            _extract_user_entities(context, str(message.get("content") or ""), message_id)
            continue

        analysis = extra_data.get("analysis") or {}
        action_type = analysis.get("action_type")
        action_slots = analysis.get("action_slots") or {}
        if action_type:
            _set_last_action(
                context,
                {
                    "action_type": action_type,
                    "order_no": action_slots.get("order_no") or _first_item(analysis.get("order_no")),
                    "source": "analysis_history",
                    "confidence": 0.65,
                },
                evidence={"kind": "analysis_history", "message_id": message_id},
            )

        for tool_result in extra_data.get("tool_results") or []:
            _extract_tool_result(context, tool_result, message_id)
        ticket_result = extra_data.get("ticket_result")
        if ticket_result:
            _extract_ticket_result(context, ticket_result, message_id)


def _apply_pending_context(context: dict[str, Any], pending: dict[str, Any] | None) -> None:
    """把未完成动作作为最高优先上下文，但不覆盖当前显式输入。"""
    if not pending or pending.get("completed") or pending.get("status") in {"completed", "cancelled"}:
        return
    slots = pending.get("action_slots") or {}
    order_no = slots.get("order_no")
    if order_no:
        _set_context_value(
            context,
            "last_order",
            order_no,
            source="pending",
            confidence=0.9,
            evidence={"kind": "pending_order", "pending_id": pending.get("pending_id")},
        )
    _set_last_action(
        context,
        {
            "action_type": pending.get("action_type"),
            "order_no": order_no,
            "source": "pending",
            "confidence": 0.95,
        },
        evidence={"kind": "pending_action", "pending_id": pending.get("pending_id")},
    )


def _extract_user_entities(context: dict[str, Any], content: str, message_id: Any) -> None:
    """从用户历史原文抽取低可信实体，原文不进入 LLM 上下文。"""
    order_match = ORDER_NO_PATTERN.search(content)
    if order_match:
        _set_context_value(
            context,
            "last_order",
            order_match.group(0),
            source="user_message",
            confidence=0.55,
            evidence={"kind": "user_order", "message_id": message_id},
        )
    ticket_match = TICKET_NO_PATTERN.search(content)
    if ticket_match:
        _set_context_value(
            context,
            "last_ticket",
            ticket_match.group(0).upper(),
            source="user_message",
            confidence=0.55,
            evidence={"kind": "user_ticket", "message_id": message_id},
        )


def _extract_tool_result(context: dict[str, Any], result: dict[str, Any], message_id: Any) -> None:
    """从可信工具结果提取订单、商品和工单上下文。"""
    if result.get("status") != "success":
        return
    query_type = result.get("query_type")
    data = result.get("data")
    if query_type == "order_detail" and isinstance(data, dict):
        order_no = data.get("orderNo") or result.get("order_no")
        if order_no:
            _set_context_value(
                context,
                "last_order",
                order_no,
                source="tool_order_detail",
                confidence=0.95,
                evidence={"kind": "tool_order_detail", "message_id": message_id},
            )
        if data.get("productName"):
            _set_context_value(
                context,
                "last_product",
                data.get("productName"),
                source="tool_order_detail",
                confidence=0.95,
                evidence={"kind": "tool_order_detail_product", "message_id": message_id},
            )
    if query_type == "customer_orders" and isinstance(data, list) and data:
        order = data[0] or {}
        if order.get("orderNo"):
            _set_context_value(
                context,
                "last_order",
                order.get("orderNo"),
                source="tool_customer_orders",
                confidence=0.9,
                evidence={"kind": "tool_customer_orders", "message_id": message_id},
            )
        if order.get("productName"):
            _set_context_value(
                context,
                "last_product",
                order.get("productName"),
                source="tool_order_detail",
                confidence=0.85,
                evidence={"kind": "tool_customer_orders_product", "message_id": message_id},
            )
    if query_type in {"ticket_status", "ticket_urge"} and isinstance(data, dict) and data.get("ticketNo"):
        _set_context_value(
            context,
            "last_ticket",
            data.get("ticketNo"),
            source="tool_ticket_status",
            confidence=0.95,
            evidence={"kind": query_type, "message_id": message_id},
        )


def _extract_ticket_result(context: dict[str, Any], result: dict[str, Any], message_id: Any) -> None:
    """从建单结果提取高可信工单号。"""
    if result.get("status") != "success":
        return
    data = result.get("data") or {}
    ticket_no = data.get("ticketNo")
    if ticket_no:
        _set_context_value(
            context,
            "last_ticket",
            ticket_no,
            source="tool_create_ticket",
            confidence=0.95,
            evidence={"kind": "tool_create_ticket", "message_id": message_id},
        )


def _set_context_value(
    context: dict[str, Any],
    key: str,
    value: Any,
    *,
    source: str,
    confidence: float,
    evidence: dict[str, Any],
) -> None:
    """按置信度写入上下文实体，保留来源用于排查。"""
    if value is None or str(value).strip() == "":
        return
    current = context.get(key)
    if current and float(current.get("confidence") or 0) > confidence:
        return
    context[key] = {
        "value": str(value),
        "source": source,
        "confidence": confidence,
    }
    _append_evidence(context, {**evidence, "field": key, "value": str(value), "source": source, "confidence": confidence})


def _set_last_action(context: dict[str, Any], action: dict[str, Any], *, evidence: dict[str, Any]) -> None:
    """写入最近动作上下文，pending 动作天然高于历史分析。"""
    if not action.get("action_type"):
        return
    current = context.get("last_action")
    confidence = float(action.get("confidence") or 0)
    if current and float(current.get("confidence") or 0) > confidence:
        return
    context["last_action"] = {
        "action_type": action.get("action_type"),
        "order_no": action.get("order_no"),
        "source": action.get("source"),
        "confidence": confidence,
    }
    _append_evidence(context, {**evidence, "field": "last_action", "source": action.get("source"), "confidence": confidence})


def _append_evidence(context: dict[str, Any], evidence: dict[str, Any]) -> None:
    """记录调试依据，避免把原始消息内容暴露给模型。"""
    debug_context = context.setdefault("debug_context", {})
    debug_context.setdefault("evidence", []).append(evidence)


def _build_safe_summary(context: dict[str, Any]) -> str:
    """生成只包含事实指代的短摘要，作为唯一传给 LLM 的历史上下文。"""
    parts: list[str] = []
    order = context.get("last_order")
    if order:
        parts.append(f"最近关联订单号为 {order['value']}，来源 {order['source']}，置信度 {order['confidence']:.2f}")
    product = context.get("last_product")
    if product:
        parts.append(f"最近关联商品为 {product['value']}，来源 {product['source']}，置信度 {product['confidence']:.2f}")
    ticket = context.get("last_ticket")
    if ticket:
        parts.append(f"最近关联工单号为 {ticket['value']}，来源 {ticket['source']}，置信度 {ticket['confidence']:.2f}")
    action = context.get("last_action")
    if action:
        order_text = f"，关联订单 {action.get('order_no')}" if action.get("order_no") else ""
        parts.append(f"最近未完成或刚发生的业务动作是 {action.get('action_type')}{order_text}，来源 {action.get('source')}，置信度 {action.get('confidence'):.2f}")
    return "；".join(parts)


def _first_item(value: Any) -> Any:
    """安全读取列表首项。"""
    if isinstance(value, list) and value:
        return value[0]
    return None
