import re
from datetime import datetime
from typing import Any

from agents.order_context import build_order_context_record, extract_order_no


ORDER_NO_PATTERN = re.compile(r"(?<![A-Za-z])(?:EC)?\d{10,18}", flags=re.IGNORECASE)
TICKET_NO_PATTERN = re.compile(r"T\d{12,24}", flags=re.IGNORECASE)


def build_conversation_context(
    *,
    messages: list[dict[str, Any]],
    pending_action_request: dict[str, Any] | None,
    selected_order_no: str | None,
    selected_ticket_no: str | None,
    login_user_context: dict[str, Any] | None = None,
    max_messages: int = 12,
) -> dict[str, Any]:
    """从同一会话最近消息中提取安全上下文，供 Agent 解析多轮指代。"""
    recent_messages = messages[-max_messages:]
    context: dict[str, Any] = {
        "last_order": None,
        "last_product": None,
        "last_ticket": None,
        "last_action": None,
        "order_context": None,
        "safe_context_summary": "",
        "debug_context": {
            "message_window": [message.get("id") for message in recent_messages],
            "evidence": [],
        },
    }

    _apply_history_context(context, recent_messages)
    safe_login_user = _safe_login_user_context(login_user_context)
    session_memory = _build_session_memory(recent_messages, safe_login_user)
    session_memory["pending_action"] = _build_pending_action_memory(pending_action_request)
    order_context = _build_order_context(recent_messages, allow_historical_selected=bool(selected_order_no))
    context["login_user_context"] = safe_login_user
    context["session_memory"] = session_memory
    context["order_context"] = order_context
    if selected_order_no:
        # 本轮前端仍明确选中该订单，属于当前显式上下文；后续仍须由 Java 接口校验订单归属。
        selected_at = datetime.utcnow().isoformat()
        context["order_context"] = build_order_context_record(
            order_no=selected_order_no,
            source="selected_by_user",
            confirmed_at=selected_at,
            last_used_at=selected_at,
            confidence=0.98,
        )
        _set_context_value(
            context,
            "last_order",
            selected_order_no,
            source="selected_by_user",
            confidence=0.98,
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


def _build_pending_action_memory(pending: dict[str, Any] | None) -> dict[str, Any] | None:
    """把当前会话待完成动作投影为安全短期记忆，不复制原始消息或工具结果。"""
    if not pending or pending.get("completed") or pending.get("status") in {"completed", "cancelled"}:
        return None
    slots = pending.get("collected_slots") or pending.get("action_slots") or {}
    return {
        "action_type": pending.get("action_type"),
        "issue_type": pending.get("issue_type"),
        "order_no": pending.get("order_no") or slots.get("order_no"),
        "missing_slots": list(pending.get("missing_slots") or []),
        "collected_slots": dict(slots),
        "status": pending.get("status"),
        "created_at": pending.get("created_at"),
        "updated_at": pending.get("updated_at"),
        "expires_at": pending.get("expires_at") or pending.get("expire_at"),
        "source": pending.get("source"),
        "confidence": pending.get("confidence"),
    }


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


def _build_order_context(messages: list[dict[str, Any]], *, allow_historical_selected: bool) -> dict[str, Any] | None:
    """从当前会话中提取最近订单上下文，明确选择/提及优先于工具推断。"""
    inferred: dict[str, Any] | None = None
    for message in reversed(messages):
        extra_data = message.get("extra_data") or {}
        created_at = message.get("created_at")
        if message.get("sender_type") == "customer":
            selected_order_no = extra_data.get("selected_order_no")
            if selected_order_no and allow_historical_selected:
                # 当前前端仍保持选中态时，历史选中才可延续；取消选中后不能继续继承旧订单。
                return build_order_context_record(
                    order_no=str(selected_order_no),
                    source="selected_by_user",
                    confirmed_at=created_at,
                    last_used_at=created_at,
                    confidence=0.98,
                )
            mentioned_order_no = extract_order_no(str(message.get("content") or ""))
            if mentioned_order_no:
                return build_order_context_record(
                    order_no=mentioned_order_no,
                    source="mentioned_by_user",
                    confirmed_at=created_at,
                    last_used_at=created_at,
                    confidence=0.92,
                )
            continue

        for tool_result in extra_data.get("tool_results") or []:
            candidate = _order_no_from_tool_result(tool_result)
            if candidate and inferred is None:
                inferred = build_order_context_record(
                    order_no=candidate,
                    source="inferred_from_context",
                    confirmed_at=None,
                    last_used_at=created_at,
                    confidence=0.6,
                )
    return inferred


def _order_no_from_tool_result(result: dict[str, Any]) -> str | None:
    """从工具结果中提取订单候选；工具推断来源使用前必须结合时间窗口确认。"""
    if result.get("status") != "success":
        return None
    data = result.get("data")
    if isinstance(data, dict):
        return data.get("orderNo") or result.get("order_no")
    if isinstance(data, list) and data:
        first = data[0] or {}
        return first.get("orderNo")
    return None


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


def _build_session_memory(messages: list[dict[str, Any]], login_user_context: dict[str, Any]) -> dict[str, Any]:
    """从当前会话客户可见消息中生成短期记忆，只用于多轮连续对话和称呼。"""
    user_messages: list[dict[str, Any]] = []
    ai_messages: list[dict[str, Any]] = []
    preferred_name: str | None = None
    self_claimed_name: str | None = None

    for message in messages:
        sender_type = message.get("sender_type")
        content = _safe_message_text(message.get("content"))
        if not content:
            continue
        item = {
            "message_id": message.get("id"),
            "content": content,
            "created_at": message.get("created_at"),
        }
        if sender_type == "customer":
            user_messages.append(item)
            extracted = _extract_self_claimed_name(content)
            if extracted:
                self_claimed_name = extracted
                preferred_name = extracted
        elif sender_type == "ai":
            # AI 消息只保留客户侧实际可见内容，不把内部建议、工具结果或风控字段送入会话记忆。
            extra_data = message.get("extra_data") or {}
            # 客户停止生成后的片段只用于历史展示，不能成为下一轮模型的正常回答记忆。
            if extra_data.get("generation_cancelled"):
                continue
            visible = _safe_message_text(extra_data.get("customer_message") or content)
            if visible:
                item["content"] = visible
                ai_messages.append(item)

    display_name = _normalize_name(login_user_context.get("display_name"))
    claimed = _normalize_name(self_claimed_name or preferred_name)
    return {
        "recent_user_messages": user_messages[-5:],
        "recent_ai_messages": ai_messages[-5:],
        "last_user_question": (user_messages[-1]["content"] if user_messages else None),
        "last_ai_answer": (ai_messages[-1]["content"] if ai_messages else None),
        "preferred_name": preferred_name,
        "self_claimed_name": self_claimed_name,
        # 冲突标记仅供后端确定性逻辑使用，传给 LLM 或前端时会被过滤。
        "identity_conflict": bool(display_name and claimed and display_name != claimed),
    }


def _safe_login_user_context(login_user_context: dict[str, Any] | None) -> dict[str, Any]:
    """保留登录态中的安全身份字段，避免 Authorization 或 customer_id 进入模型上下文。"""
    raw = login_user_context or {}
    display_name = _safe_message_text(raw.get("display_name"), max_length=40)
    role = _safe_message_text(raw.get("role"), max_length=30) or "customer"
    source = _safe_message_text(raw.get("source"), max_length=30) or "java_auth"
    return {
        "display_name": display_name,
        "role": role,
        "verified": bool(raw.get("verified", True)),
        "source": source,
    }


def _extract_self_claimed_name(content: str) -> str | None:
    """识别“我叫/我是/以后叫我”等会话称呼表达，不把它当作认证身份。"""
    patterns = [
        r"(?:我叫|我是|本人叫|我的名字叫)\s*([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z·]{1,11})",
        r"(?:以后|之后)?(?:请)?(?:叫我|称呼我为|喊我)\s*([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z·]{1,11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, content)
        if not match:
            continue
        name = match.group(1).strip(" ，,。！？!；;：:")
        # 排除“我是客户/会员/人工”等角色词，避免把普通身份描述误当姓名。
        if name and name not in {"客户", "用户", "会员", "客服", "人工", "本人"}:
            return name[:12]
    return None


def _safe_message_text(value: Any, max_length: int = 200) -> str:
    """清洗客户可见文本，避免凭证和过长内容进入短期记忆。"""
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "[AUTH_REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text[:max_length]


def _normalize_name(value: Any) -> str:
    """归一化姓名用于冲突判断，不改变原始称呼展示。"""
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


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
