import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from agents.order_context import resolve_order_context
from agents.intent_normalizer import is_logistics_message as normalize_is_logistics_message
from schemas.intent_schema import ActionTurnExtraction, IntentResult, ReturnGoodsSlots, SlotMetadata


ACTION_SLOT_RULES: dict[str, dict[str, Any]] = {
    "return_goods": {
        "intent": "refund",
        "ticket_type": "refund",
        "required_slots": ["order_no", "after_sale_reason", "return_method"],
        "optional_slots": ["product_name", "description", "evidence_hint", "pickup_status"],
        "conditional_required_slots": {"return_method=pickup": ["pickup_time_window"]},
        "need_order_validation": True,
        "default_next_action": "create_ticket",
    },
    "refund_request": {
        "intent": "refund",
        "ticket_type": "refund",
        "required_slots": ["order_no", "after_sale_reason"],
        "optional_slots": ["product_name", "description", "evidence_hint"],
        "conditional_required_slots": {},
        "need_order_validation": True,
        "default_next_action": "create_ticket",
    },
    "exchange_goods": {
        "intent": "exchange",
        "ticket_type": "exchange",
        "required_slots": ["order_no", "after_sale_reason"],
        "optional_slots": ["product_name", "description", "evidence_hint"],
        "conditional_required_slots": {},
        "need_order_validation": True,
        "default_next_action": "create_ticket",
    },
    "repair_request": {
        "intent": "repair",
        "ticket_type": "repair",
        "required_slots": ["order_no", "fault_description"],
        "optional_slots": ["contact_preference", "evidence_hint", "description"],
        "conditional_required_slots": {},
        "need_order_validation": True,
        "default_next_action": "create_ticket",
    },
    "invoice_issue": {
        "intent": "invoice",
        "ticket_type": "invoice",
        "required_slots": ["order_no", "invoice_title", "invoice_type"],
        "optional_slots": ["tax_no", "description"],
        "conditional_required_slots": {"company_invoice": ["tax_no"]},
        "need_order_validation": True,
        "default_next_action": "create_ticket",
    },
    "cancel_order": {
        "intent": "refund",
        "ticket_type": "refund",
        "required_slots": ["order_no", "description"],
        "optional_slots": ["product_name"],
        "conditional_required_slots": {},
        "need_order_validation": True,
        "default_next_action": "create_ticket",
    },
    "complaint_submit": {
        "intent": "complaint",
        "ticket_type": "complaint",
        "required_slots": ["description"],
        "optional_slots": ["order_no", "product_name", "evidence_hint"],
        "conditional_required_slots": {},
        "need_order_validation": False,
        "default_next_action": "create_ticket",
    },
}

PENDING_DIRECT_CONTINUE_SECONDS = 5 * 60
PENDING_EXPIRE_SECONDS = 30 * 60


def extract_action_turn(
    analysis: IntentResult,
    *,
    message: str,
    selected_order_no: str | None,
    pending_action_request: dict[str, Any] | None,
) -> ActionTurnExtraction:
    """一次性抽取本轮动作、操作语义和全部槽位，供统一状态归并使用。"""
    pending = _usable_pending(pending_action_request)
    inferred_action_type = _selected_order_short_action_type(message, selected_order_no) or infer_action_type(message, analysis)
    pending_action_type = str((pending or {}).get("action_type") or "") or None
    action_type = inferred_action_type or pending_action_type
    explicit_action = _has_explicit_action_phrase(message, selected_order_no)

    if pending and _is_cancel_message(message):
        operation = "cancel"
    elif pending and _is_confirm_message(message):
        operation = "confirm"
    elif pending and _is_explicit_topic_switch(message, pending):
        operation = "switch"
    elif explicit_action:
        operation = "start"
    elif pending:
        operation = "update"
    else:
        operation = "unknown"

    slots: dict[str, Any] = {}
    metadata: dict[str, SlotMetadata] = {}
    ambiguous_fields: list[str] = []

    # 模型只负责提供候选槽位；低置信度结果不能直接进入可执行状态。
    for key, value in _clean_slots(analysis.action_slots or {}).items():
        if analysis.confidence < 0.7:
            ambiguous_fields.append(key)
            continue
        slots[key] = value
        metadata[key] = SlotMetadata(source="llm", confidence=analysis.confidence)

    explicit_order_no = _extract_order_no(message)
    analysis_order_no = analysis.order_no[0] if analysis.order_no else None
    if analysis_order_no and analysis.confidence >= 0.7 and not slots.get("order_no"):
        slots["order_no"] = analysis_order_no
        metadata["order_no"] = SlotMetadata(source="llm", confidence=analysis.confidence)
    elif analysis_order_no and analysis.confidence < 0.7:
        ambiguous_fields.append("order_no")
    if explicit_order_no:
        slots["order_no"] = explicit_order_no
        metadata["order_no"] = SlotMetadata(source="explicit_message", confidence=1.0)
    if selected_order_no:
        # 前端当前选择代表本轮实时上下文，优先级高于历史和模型推断。
        slots["order_no"] = selected_order_no
        metadata["order_no"] = SlotMetadata(source="selected_order", confidence=1.0)

    reason = _extract_after_sale_reason(message)
    if reason:
        slots["after_sale_reason"] = reason
        slots["description"] = reason
        metadata["after_sale_reason"] = SlotMetadata(source="explicit_message", confidence=1.0)
        metadata["description"] = SlotMetadata(source="derived", confidence=1.0)

    return_context = action_type == "return_goods" or pending_action_type == "return_goods"
    if return_context:
        method_aliases = {
            "上门取件": "pickup",
            "pickup": "pickup",
            "自行寄回": "self_ship",
            "自己寄回": "self_ship",
            "self_ship": "self_ship",
        }
        if slots.get("return_method"):
            normalized_method = method_aliases.get(str(slots["return_method"]).strip())
            if normalized_method:
                slots["return_method"] = normalized_method
            else:
                # 未知枚举值只作为待澄清字段，不能让 Pydantic 校验异常中断客户请求。
                slots.pop("return_method", None)
                metadata.pop("return_method", None)
                ambiguous_fields.append("return_method")
        if slots.get("pickup_time_window"):
            normalized_time = _extract_pickup_time_window(str(slots["pickup_time_window"]))
            if normalized_time:
                slots["pickup_time_window"] = normalized_time
            else:
                slots.pop("pickup_time_window", None)
                metadata.pop("pickup_time_window", None)
                ambiguous_fields.append("pickup_time_window")
        fulfillment_slots = _extract_return_fulfillment_slots(message, pending, allow_unbound_time=True)
        for key, value in fulfillment_slots.items():
            slots[key] = value
            metadata[key] = SlotMetadata(source="explicit_message", confidence=1.0)

        # 退货槽位统一经过 Pydantic 校验；自行寄回时旧取件时间不再有效。
        if slots.get("return_method") == "self_ship":
            slots.pop("pickup_time_window", None)
            metadata.pop("pickup_time_window", None)
        allowed = set(ReturnGoodsSlots.model_fields)
        validated = ReturnGoodsSlots(**{key: value for key, value in slots.items() if key in allowed})
        return_slots = validated.model_dump(exclude_none=True)
        slots = {**{key: value for key, value in slots.items() if key not in allowed}, **return_slots}

    if action_type is None and selected_order_no and slots.get("after_sale_reason"):
        action_type = "return_goods"

    return ActionTurnExtraction(
        operation=operation,
        action_type=action_type if action_type in ACTION_SLOT_RULES else None,
        explicit_action=explicit_action,
        slots=_clean_slots(slots),
        slot_metadata=metadata,
        ambiguous_fields=list(dict.fromkeys(ambiguous_fields)),
    )


def merge_action_turn_slots(
    pending_action_request: dict[str, Any] | None,
    turn: ActionTurnExtraction,
    *,
    extra_slots: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按历史 pending、当前消息、当前选中订单的优先级归并槽位及来源元数据。"""
    pending = pending_action_request or {}
    slots = _clean_slots(pending.get("action_slots") or {})
    metadata = dict(pending.get("slot_metadata") or {})
    slots.update(_clean_slots(extra_slots or {}))
    slots.update(_clean_slots(turn.slots))
    metadata.update({key: value.model_dump() for key, value in turn.slot_metadata.items()})

    if slots.get("return_method") == "self_ship":
        # 客户改为自行寄回时，清除此前可能遗留的取件时间，避免脏数据进入工单。
        slots.pop("pickup_time_window", None)
        metadata.pop("pickup_time_window", None)
    return slots, metadata


def _resolve_pending_reason(message: str, turn: ActionTurnExtraction) -> str | None:
    """在多槽位消息中保留纯原因片段，单独补原因时保留客户完整原话。"""
    extracted = turn.slots.get("after_sale_reason")
    has_other_slots = bool(
        {"return_method", "pickup_time_window"}.intersection(turn.slots)
    )
    if extracted and has_other_slots:
        return str(extracted)
    return _extract_pending_reason(message) or (str(extracted) if extracted else None)


def enrich_action_analysis(
    analysis: IntentResult,
    *,
    message: str,
    selected_order_no: str | None,
    pending_action_request: dict[str, Any] | None,
    conversation_context: dict[str, Any] | None = None,
) -> tuple[IntentResult, dict[str, Any] | None]:
    """合并本轮意图、前端选中订单和上一轮 pending，计算业务动作下一步。"""
    normalized = analysis.model_copy(deep=True)
    pending_state = _pending_time_state(pending_action_request)
    pending = _usable_pending(pending_action_request)
    context_resolution = resolve_order_context(conversation_context, message)
    context_order_no = context_resolution.get("order_no") if context_resolution.get("status") == "usable" else None
    context_ticket_no = _context_value(conversation_context, "last_ticket")
    explicit_order_no = _extract_order_no(message)
    context_conflict = _detect_context_conflict(message, explicit_order_no, selected_order_no, pending)
    turn = extract_action_turn(
        normalized,
        message=message,
        selected_order_no=selected_order_no,
        pending_action_request=pending_action_request,
    )

    short_action_type = _selected_order_short_action_type(message, selected_order_no)
    if short_action_type:
        # 前端已明确选中订单时，“退货/退款/换货”这类单词本身就是对当前订单的动作意图，
        # 不能被前置的规则咨询快速路径误改写为政策问答。
        rule = ACTION_SLOT_RULES[short_action_type]
        normalized.intent = str(rule["intent"])
        normalized.user_goal = "action_request"
        normalized.action_type = short_action_type
        normalized.summary = message.strip()
        normalized.order_related = True
        normalized.need_order_query = True
        normalized.need_human = False
        normalized.need_ticket = False
        normalized.risk_reasons = [
            reason
            for reason in normalized.risk_reasons
            if reason not in {"low_confidence", "refund_commitment", "action_or_dispute_requires_human"}
        ]

    if not pending and _is_standalone_unwanted_message(message) and not turn.explicit_action:
        # 单独的“不想要了”只表达了原因，尚不足以证明客户授权发起退货，先做动作确认。
        action_type = "return_goods"
        immediate_slots = {"order_no": turn.slots.get("order_no")} if turn.slots.get("order_no") else {}
        deferred_slots = {
            key: value
            for key, value in turn.slots.items()
            if key in {"after_sale_reason", "description", "return_method", "pickup_time_window", "pickup_status"}
        }
        normalized.intent = "refund"
        normalized.user_goal = "action_request"
        normalized.action_type = action_type
        normalized.action_slots = immediate_slots
        normalized.missing_slots = ["action_confirmation"]
        normalized.next_action = "collect_slots"
        normalized.order_related = bool(immediate_slots.get("order_no"))
        normalized.need_order_query = False
        normalized.need_human = False
        normalized.need_ticket = False
        ambiguous_pending = _build_pending(
            normalized,
            immediate_slots,
            message,
            "collect_slots",
            ["action_confirmation"],
            None,
            turn=turn,
        )
        ambiguous_pending["status"] = "awaiting_confirmation"
        ambiguous_pending["flow_state"] = "AWAITING_CONFIRMATION"
        ambiguous_pending["confirmation_reason"] = "ambiguous_unwanted_item"
        ambiguous_pending["deferred_slots"] = deferred_slots
        return normalized, ambiguous_pending

    if pending and _is_confirm_message(message) and "order_no" in set(pending.get("missing_slots") or []):
        # 兼容旧版本已输出候选订单问题、但尚未写入 order_confirmation 的会话状态。
        confirmation_resolution = resolve_order_context(conversation_context, "这个订单")
        candidate_order_no = confirmation_resolution.get("order_no")
        confirmed_order_no = _resolve_confirmation_order(
            selected_order_no=selected_order_no,
            explicit_order_no=explicit_order_no,
            candidate_order_no=(
                str(candidate_order_no)
                if candidate_order_no and confirmation_resolution.get("status") in {"usable", "needs_confirmation"}
                else None
            ),
        )
        if confirmed_order_no:
            slots, slot_metadata = merge_action_turn_slots(pending, turn)
            slots["order_no"] = confirmed_order_no
            confirmed_reason = _extract_confirmed_reason(message)
            if confirmed_reason:
                # “是的，原因是拍错了”同时完成订单确认和原因补槽，避免再次重复追问。
                slots["after_sale_reason"] = confirmed_reason
                slots["description"] = confirmed_reason
            return _continue_pending_action(
                normalized,
                pending,
                pending.get("action_type"),
                slots,
                message,
                slot_metadata=slot_metadata,
                turn=turn,
            )

    if pending and _is_pending_resume_confirmation(pending) and _is_confirm_message(message):
        # 5-30 分钟的旧流程必须先经客户确认；确认后才合并暂存槽位，避免直接执行历史动作。
        action_type = pending.get("action_type") or normalized.action_type
        slots, slot_metadata = merge_action_turn_slots(
            pending,
            turn,
            extra_slots=_clean_slots(pending.get("deferred_slots") or {}),
        )
        slots.pop("candidate_order_no", None)
        confirmed_order_no = _resolve_confirmation_order(
            selected_order_no=selected_order_no,
            explicit_order_no=explicit_order_no,
        )
        if confirmed_order_no:
            # 确认时的前端实时选择优先于旧 pending，避免流程恢复后再次索要订单号。
            slots["order_no"] = confirmed_order_no
        confirmed_reason = _extract_confirmed_reason(message)
        if confirmed_reason:
            # 允许客户在“重新发起/继续”确认中顺便给出原因，下一步直接进入退回方式收集。
            slots["after_sale_reason"] = confirmed_reason
            slots["description"] = confirmed_reason
        return _continue_pending_action(
            normalized,
            pending,
            action_type,
            slots,
            message,
            slot_metadata=slot_metadata,
            turn=turn,
        )

    if pending and _is_pending_resume_confirmation(pending) and _is_reject_confirmation_message(message):
        # 客户否认继续旧流程时立即终止，后续消息不会再继承该订单或退货原因。
        return _cancel_pending_action(normalized, pending, message, "resume_rejected")

    if pending and _is_pending_order_confirmation(pending) and _is_confirm_message(message):
        # 用户确认 5-30 分钟内的候选订单后，才把候选订单提升为可执行动作订单。
        action_type = pending.get("action_type") or infer_action_type(message, normalized)
        candidate_order_no = str((pending.get("action_slots") or {}).get("candidate_order_no") or "").strip()
        confirmed_order_no = _resolve_confirmation_order(
            selected_order_no=selected_order_no,
            explicit_order_no=explicit_order_no,
            candidate_order_no=candidate_order_no,
        )
        if action_type and confirmed_order_no:
            slots, slot_metadata = merge_action_turn_slots(pending, turn)
            slots.pop("candidate_order_no", None)
            slots["order_no"] = confirmed_order_no
            confirmed_reason = _extract_confirmed_reason(message)
            if confirmed_reason:
                slots["after_sale_reason"] = confirmed_reason
                slots["description"] = confirmed_reason
            rule = ACTION_SLOT_RULES.get(action_type)
            missing_slots = compute_missing_slots(action_type, slots)
            next_action = "collect_slots" if missing_slots else (rule or {}).get("default_next_action", "collect_slots")
            normalized.intent = (rule or {}).get("intent", normalized.intent)
            normalized.user_goal = "action_request"
            normalized.action_type = action_type
            normalized.action_slots = slots
            normalized.missing_slots = missing_slots
            normalized.next_action = next_action
            normalized.order_related = True
            normalized.need_order_query = bool((rule or {}).get("need_order_validation", False))
            normalized.need_human = next_action == "create_ticket"
            normalized.need_ticket = next_action == "create_ticket"
            if confirmed_order_no not in normalized.order_no:
                normalized.order_no.append(confirmed_order_no)
            pending_payload = _build_pending(
                normalized,
                slots,
                message,
                next_action,
                missing_slots,
                pending,
                slot_metadata=slot_metadata,
                turn=turn,
            )
            if next_action == "create_ticket":
                pending_payload["status"] = "ready"
                pending_payload["flow_state"] = "READY"
            return normalized, pending_payload

    if pending and _is_pending_order_confirmation(pending) and _is_reject_confirmation_message(message):
        # 用户否认候选订单时，清理候选订单，重新要求用户提供明确订单号或前端选择订单。
        action_type = pending.get("action_type") or infer_action_type(message, normalized)
        normalized.user_goal = "action_request"
        normalized.action_type = action_type
        normalized.action_slots = {}
        normalized.missing_slots = ["order_no"]
        normalized.next_action = "collect_slots"
        normalized.order_related = False
        normalized.need_order_query = False
        normalized.need_human = False
        normalized.need_ticket = False
        pending_payload = _build_pending(normalized, {}, message, "collect_slots", ["order_no"], pending)
        pending_payload["status"] = "waiting_for_user_input"
        pending_payload["flow_state"] = "COLLECTING"
        pending_payload["rejected_candidate_order_no"] = (pending.get("action_slots") or {}).get("candidate_order_no")
        return normalized, pending_payload

    if (
        pending
        and (_is_cancel_message(message) or _is_reset_pending_message(message))
        and not _has_new_status_query(message, explicit_order_no, context_ticket_no)
        and not (_is_pending_return_fulfillment(pending) and _extract_return_fulfillment_slots(message, pending))
    ):
        # 只有明确取消或重新开始才终止流程；不能再把任意“短句/越界”误判成取消。
        return _cancel_pending_action(normalized, pending, message, "user_cancel")

    if pending and normalized.user_goal == "action_request" and _is_explicit_topic_switch(message, pending):
        new_action_type = infer_action_type(message, normalized)
        if new_action_type and new_action_type != pending.get("action_type"):
            # 显式新动作会建立新的 pending，旧动作槽位不得混入新流程。
            _record_context_conflict(
                conversation_context,
                {
                    "type": "action_changed",
                    "pending_action_type": pending.get("action_type"),
                    "current_action_type": new_action_type,
                },
            )
            pending = None
            pending_state = "none"
            context_conflict = None

    if pending and _is_pending_after_sale_reason(pending) and pending_state == "needs_confirmation" and not context_conflict and not _is_explicit_topic_switch(message, pending):
        # 旧流程已超过 5 分钟时先确认是否继续，并暂存本轮原因；确认前绝不查单或建单。
        reason = _resolve_pending_reason(message, turn)
        if reason:
            action_type = pending.get("action_type") or normalized.action_type
            slots, slot_metadata = merge_action_turn_slots(pending, turn)
            normalized.intent = (ACTION_SLOT_RULES.get(action_type) or {}).get("intent", normalized.intent)
            normalized.user_goal = "action_request"
            normalized.action_type = action_type
            normalized.action_slots = slots
            normalized.missing_slots = ["pending_confirmation"]
            normalized.next_action = "collect_slots"
            normalized.need_order_query = False
            normalized.need_human = False
            normalized.need_ticket = False
            pending_payload = _build_pending(
                normalized,
                slots,
                message,
                "collect_slots",
                ["pending_confirmation"],
                pending,
                slot_metadata=slot_metadata,
                turn=turn,
            )
            pending_payload["status"] = "awaiting_confirmation"
            pending_payload["flow_state"] = "AWAITING_CONFIRMATION"
            pending_payload["deferred_slots"] = {
                **_clean_slots(turn.slots),
                "after_sale_reason": reason,
                "description": reason,
            }
            pending_payload["confirmation_reason"] = "stale_pending_action"
            return normalized, pending_payload

    if pending and _is_pending_after_sale_reason(pending) and pending_state == "fresh" and not context_conflict and not _is_explicit_topic_switch(message, pending):
        # 上一轮正在收集退货/退款/换货原因时，优先把“商品质量问题”等短句视为原因槽位。
        # 这能避免补充原因被重新识别成投诉，从而错误走投诉话术。
        reason = _resolve_pending_reason(message, turn)
        if reason:
            if (
                reason in message.strip()
                and len(message.strip()) <= 80
                and not {"return_method", "pickup_time_window"}.intersection(turn.slots)
            ):
                # 补槽短句优先保留客户原话，便于后续人工核实售后原因。
                reason = message.strip()
            action_type = pending.get("action_type") or normalized.action_type
            slots, slot_metadata = merge_action_turn_slots(pending, turn)
            slots["after_sale_reason"] = reason
            slots["description"] = reason
            rule = ACTION_SLOT_RULES.get(action_type)
            missing_slots = compute_missing_slots(action_type, slots)
            next_action = "collect_slots" if missing_slots else (rule or {}).get("default_next_action", "collect_slots")
            normalized.intent = (rule or {}).get("intent", normalized.intent)
            normalized.user_goal = "action_request"
            normalized.action_type = action_type
            normalized.action_slots = slots
            normalized.missing_slots = missing_slots
            normalized.next_action = next_action
            normalized.order_related = bool(slots.get("order_no")) or bool((rule or {}).get("need_order_validation", False))
            normalized.need_order_query = bool(slots.get("order_no")) and bool((rule or {}).get("need_order_validation", False))
            normalized.need_human = next_action == "create_ticket"
            normalized.need_ticket = next_action == "create_ticket"
            normalized.risk_reasons = [item for item in normalized.risk_reasons if item != "complaint"]
            normalized.priority = max_priority(normalized.priority, "medium")
            order_no = slots.get("order_no")
            if order_no and str(order_no) not in normalized.order_no:
                normalized.order_no.append(str(order_no))
            pending_payload = _build_pending(
                normalized,
                slots,
                message,
                next_action,
                missing_slots,
                pending,
                slot_metadata=slot_metadata,
                turn=turn,
            )
            if next_action == "create_ticket":
                pending_payload["status"] = "ready"
                pending_payload["flow_state"] = "READY"
            return normalized, pending_payload

    if (
        pending
        and pending.get("action_type") == "return_goods"
        and pending_state in {"fresh", "needs_confirmation"}
        and not context_conflict
        and not _is_explicit_topic_switch(message, pending)
    ):
        # 退货履约信息允许在任意阶段补充或修改，不依赖上一轮固定追问顺序。
        fulfillment_slots = {
            key: value
            for key, value in turn.slots.items()
            if key in {"return_method", "pickup_time_window", "pickup_status"}
        }
        if fulfillment_slots:
            action_type = str(pending.get("action_type") or "return_goods")
            slots, slot_metadata = merge_action_turn_slots(pending, turn)
            if pending_state == "needs_confirmation":
                normalized.intent = (ACTION_SLOT_RULES.get(action_type) or {}).get("intent", normalized.intent)
                normalized.user_goal = "action_request"
                normalized.action_type = action_type
                normalized.action_slots = slots
                normalized.missing_slots = ["pending_confirmation"]
                normalized.next_action = "collect_slots"
                normalized.need_order_query = False
                normalized.need_human = False
                normalized.need_ticket = False
                pending_payload = _build_pending(
                    normalized,
                    slots,
                    message,
                    "collect_slots",
                    ["pending_confirmation"],
                    pending,
                    slot_metadata=slot_metadata,
                    turn=turn,
                )
                pending_payload["status"] = "awaiting_confirmation"
                pending_payload["flow_state"] = "AWAITING_CONFIRMATION"
                pending_payload["deferred_slots"] = fulfillment_slots
                pending_payload["confirmation_reason"] = "stale_pending_action"
                return normalized, pending_payload
            slots.update(fulfillment_slots)
            return _continue_pending_action(
                normalized,
                pending,
                action_type,
                slots,
                message,
                slot_metadata=slot_metadata,
                turn=turn,
            )

    expired_reason = _extract_pending_reason(message)
    if (
        pending_state == "expired"
        and normalized.user_goal not in {"status_query", "policy_consult", "how_to", "human_request", "info_query"}
        and (expired_reason or _is_explicit_same_action_request(message, pending_action_request))
        and not _is_explicit_topic_switch(message, pending_action_request)
    ):
        # 超过 30 分钟后不继承旧订单；只暂存本轮原因，并询问是否重新发起新的退货流程。
        action_type = str((pending_action_request or {}).get("action_type") or "return_goods")
        normalized.intent = (ACTION_SLOT_RULES.get(action_type) or {}).get("intent", "refund")
        normalized.user_goal = "action_request"
        normalized.action_type = action_type if action_type in ACTION_SLOT_RULES else "return_goods"
        normalized.action_slots = {}
        normalized.missing_slots = ["action_confirmation"]
        normalized.next_action = "collect_slots"
        normalized.order_related = False
        normalized.need_order_query = False
        normalized.need_human = False
        normalized.need_ticket = False
        restarted = _build_pending(
            normalized,
            {},
            message,
            "collect_slots",
            ["action_confirmation"],
            None,
            turn=turn,
        )
        restarted["status"] = "awaiting_confirmation"
        restarted["flow_state"] = "AWAITING_CONFIRMATION"
        if expired_reason:
            restarted["deferred_slots"] = {
                **_clean_slots(turn.slots),
                "after_sale_reason": expired_reason,
                "description": expired_reason,
            }
        restarted["confirmation_reason"] = "expired_pending_action"
        return normalized, restarted

    if pending and context_conflict and context_conflict.get("type") == "order_changed" and not _is_explicit_topic_switch(message, pending):
        # 当前明确选中/提及的订单优先于旧 pending；若本轮同时补充了原因，则在新订单上重新建立动作。
        reason = _extract_pending_reason(message)
        current_order_no = context_conflict.get("current_order_no")
        if reason and current_order_no:
            _record_context_conflict(conversation_context, context_conflict)
            action_type = str(pending.get("action_type") or "return_goods")
            slots = {**_clean_slots(turn.slots), "order_no": current_order_no, "after_sale_reason": reason, "description": reason}
            return _continue_pending_action(normalized, {}, action_type, slots, message, turn=turn)

    if normalized.user_goal != "action_request":
        # 政策咨询和状态查询不能继承旧动作上下文，避免“查看退货规则”被误拉进退货申请流程。
        preserve_human_request = normalized.user_goal == "human_request"
        if pending:
            pending = _build_pending(normalized, pending.get("action_slots") or {}, message, "cancel_pending", [], pending)
            pending["status"] = "cancelled"
            pending["flow_state"] = "CANCELLED"
            pending["completed"] = True
            pending["cancel_reason"] = "non_action_intent"
        normalized.action_slots = {}
        normalized.missing_slots = []
        normalized.next_action = "transfer_human" if preserve_human_request else None
        normalized.need_human = True if preserve_human_request else False
        normalized.need_ticket = False
        return normalized, pending if pending else None

    if pending and (context_conflict or _is_cancel_message(message)):
        # 用户明确取消或切换目标时，先结束上一轮 pending，避免旧动作污染新查询。
        pending = _build_pending(normalized, pending.get("action_slots") or {}, message, "cancel_pending", [], pending)
        pending["status"] = "cancelled"
        pending["flow_state"] = "CANCELLED"
        pending["completed"] = True
        if context_conflict:
            pending["cancel_reason"] = "context_conflict"
            _record_context_conflict(conversation_context, context_conflict)
        normalized.action_type = pending.get("action_type") or normalized.action_type
        normalized.action_slots = pending.get("action_slots") or {}
        normalized.missing_slots = []
        normalized.next_action = "cancel_pending"
        normalized.need_human = False
        normalized.need_ticket = False
        if _has_new_status_query(message, explicit_order_no, context_ticket_no):
            # 取消旧动作后继续处理本轮明确的新查询，不把本轮降级成单纯取消确认。
            normalized.action_type = None
            normalized.action_slots = {}
            normalized.missing_slots = []
            normalized.next_action = None
            normalized.user_goal = "status_query"
            normalized.intent = "logistics" if _is_logistics_query_message(message) else normalized.intent
            normalized.need_order_query = bool(explicit_order_no or selected_order_no or context_order_no)
            normalized.order_related = normalized.need_order_query
            normalized.need_ticket = False
            normalized.need_human = False
            candidate_order_no = explicit_order_no or selected_order_no or context_order_no
            if candidate_order_no and candidate_order_no not in normalized.order_no:
                normalized.order_no.append(candidate_order_no)
            return normalized, pending
        return normalized, pending

    action_type = turn.action_type or infer_action_type(message, normalized)
    inherited_slots: dict[str, Any] = {}
    inherited_action_type = None
    if pending:
        inherited_action_type = pending.get("action_type")
        inherited_slots.update(_clean_slots(pending.get("action_slots") or {}))

    if action_type is None and pending:
        # 用户只补充订单号或原因时，继续上一轮未完成动作。
        action_type = inherited_action_type
    if action_type is None and selected_order_no and turn.slots.get("after_sale_reason"):
        # 已选中订单且只输入售后原因时，按退货申请收集槽位。
        action_type = "return_goods"

    if action_type is None:
        normalized.action_type = None
        normalized.action_slots = {}
        normalized.missing_slots = []
        normalized.next_action = None
        return normalized, None

    if not selected_order_no and not explicit_order_no and context_resolution.get("status") == "needs_confirmation":
        # 5-30 分钟或工具推断得到的订单不能直接用于售后动作，必须先让客户确认。
        candidate_order_no = context_resolution.get("order_no")
        normalized.action_type = action_type
        normalized.action_slots = {"candidate_order_no": candidate_order_no}
        normalized.missing_slots = ["order_confirmation"]
        normalized.next_action = "collect_slots"
        normalized.order_related = False
        normalized.need_order_query = False
        normalized.need_human = False
        normalized.need_ticket = False
        if "order_context_needs_confirmation" not in normalized.risk_reasons:
            normalized.risk_reasons.append("order_context_needs_confirmation")
        return normalized, _build_pending(normalized, normalized.action_slots, message, "collect_slots", normalized.missing_slots, pending)

    selected_or_context_order = selected_order_no or context_order_no
    if action_type == "return_goods":
        slots, slot_metadata = merge_action_turn_slots(
            pending,
            turn,
            extra_slots=inherited_slots,
        )
    else:
        current_slots = extract_action_slots(message, normalized, selected_or_context_order)
        slots = {**inherited_slots, **_clean_slots(current_slots)}
        slot_metadata = dict((pending or {}).get("slot_metadata") or {})
    if selected_or_context_order and not slots.get("order_no"):
        # 前端选中或上下文订单只能作为候选，后续仍需 Java 接口校验归属。
        slots["order_no"] = selected_or_context_order

    rule = ACTION_SLOT_RULES.get(action_type)
    if not rule:
        normalized.action_type = "other"
        normalized.action_slots = slots
        normalized.missing_slots = []
        normalized.next_action = "unsupported"
        normalized.need_human = True
        normalized.need_ticket = True
        return normalized, _build_pending(normalized, slots, message, "unsupported", [], pending)

    missing_slots = compute_missing_slots(action_type, slots)
    next_action = "collect_slots" if missing_slots else rule["default_next_action"]
    normalized.intent = rule["intent"]
    normalized.user_goal = "action_request"
    normalized.action_type = action_type
    normalized.action_slots = slots
    normalized.missing_slots = missing_slots
    normalized.next_action = next_action
    normalized.order_related = bool(slots.get("order_no")) or rule.get("need_order_validation", False)
    normalized.need_order_query = bool(slots.get("order_no")) and rule.get("need_order_validation", False)
    normalized.need_human = next_action == "create_ticket"
    normalized.need_ticket = next_action == "create_ticket"
    normalized.priority = "high" if normalized.intent == "complaint" else max_priority(normalized.priority, "medium")

    order_no = slots.get("order_no")
    if order_no and order_no not in normalized.order_no:
        normalized.order_no.append(str(order_no))

    pending_payload = _build_pending(
        normalized,
        slots,
        message,
        next_action,
        missing_slots,
        pending,
        slot_metadata=slot_metadata,
        turn=turn,
    )
    if next_action == "create_ticket":
        pending_payload["status"] = "ready"
        pending_payload["flow_state"] = "READY"
    return normalized, pending_payload


def infer_action_type(message: str, analysis: IntentResult) -> str | None:
    """从用户表达和结构化意图推断真实业务动作，政策咨询不进入动作闭环。"""
    text = message.strip()
    if analysis.user_goal != "action_request" and not _looks_like_action_request(text):
        return None
    if analysis.action_type and analysis.action_type != "other":
        return analysis.action_type
    if _contains_any(text, ["退货", "退回商品", "退订单", "退这单", "退这个订单", "不要了"]):
        return "return_goods"
    if _contains_any(text, ["退款", "退钱", "返钱"]):
        return "refund_request"
    if _contains_any(text, ["换货", "更换", "换一个"]):
        return "exchange_goods"
    if _contains_any(text, ["维修", "报修", "修一下", "修理"]):
        return "repair_request"
    if _contains_any(text, ["发票", "开票"]):
        return "invoice_issue"
    if _contains_any(text, ["取消订单", "取消这单", "取消购买"]):
        return "cancel_order"
    if analysis.intent == "complaint" or _contains_any(text, ["投诉", "举报", "维权"]):
        return "complaint_submit"
    return None


def extract_action_slots(message: str, analysis: IntentResult, selected_order_no: str | None) -> dict[str, Any]:
    """从自然语言中抽取动作槽位，保守处理政策问题。"""
    slots = dict(analysis.action_slots or {})
    order_no = _extract_order_no(message) or selected_order_no or (analysis.order_no[0] if analysis.order_no else None)
    if order_no:
        slots["order_no"] = order_no
    if analysis.product_name:
        slots["product_name"] = analysis.product_name

    invoice_type = _extract_invoice_type(message)
    if invoice_type:
        slots["invoice_type"] = invoice_type
    invoice_title = _extract_invoice_title(message)
    if invoice_title:
        slots["invoice_title"] = invoice_title
    tax_no = _extract_tax_no(message)
    if tax_no:
        slots["tax_no"] = tax_no

    reason = _extract_after_sale_reason(message)
    if reason:
        slots["after_sale_reason"] = reason
        slots["description"] = reason

    if analysis.action_type == "return_goods" or _contains_any(message, ["上门取件", "自行寄回", "自己寄回", "快递寄回"]):
        # 取件偏好属于退货业务数据，不交给模型自由发挥，统一抽取为结构化槽位。
        slots.update(_extract_return_fulfillment_slots(message))

    fault = _extract_fault_description(message)
    if fault:
        slots["fault_description"] = fault
        slots["description"] = fault

    complaint = _extract_complaint_description(message)
    if complaint:
        slots["description"] = complaint
    return _clean_slots(slots)


def compute_missing_slots(action_type: str, slots: dict[str, Any]) -> list[str]:
    """根据动作规则计算缺失槽位。"""
    rule = ACTION_SLOT_RULES.get(action_type)
    if not rule:
        return []
    missing = [slot for slot in rule.get("required_slots", []) if not slots.get(slot)]
    for trigger_slot, required_slots in (rule.get("conditional_required_slots") or {}).items():
        if "=" in trigger_slot:
            slot_name, expected_value = trigger_slot.split("=", 1)
            triggered = str(slots.get(slot_name) or "") == expected_value
        else:
            triggered = bool(slots.get(trigger_slot))
        if triggered:
            missing.extend(slot for slot in required_slots if not slots.get(slot))
    return list(dict.fromkeys(missing))


def _should_use_context_order(message: str, context_order_no: str | None) -> bool:
    """判断本轮是否允许继承历史订单上下文，避免普通动作请求误绑定旧订单。"""
    if not context_order_no:
        return False
    return _contains_any(
        message,
        [
            "这单",
            "那单",
            "这个订单",
            "那个订单",
            "这笔订单",
            "那笔订单",
            "当前订单",
            "刚才那个",
            "刚刚那个",
            "上面那个",
            "继续",
        ],
    )


def max_priority(left: str, right: str) -> str:
    """返回更高的业务优先级。"""
    order = {"low": 0, "medium": 1, "high": 2, "urgent": 3}
    return left if order.get(left, 0) >= order.get(right, 0) else right


def _build_pending(
    analysis: IntentResult,
    slots: dict[str, Any],
    message: str,
    next_action: str,
    missing_slots: list[str],
    previous: dict[str, Any] | None,
    *,
    slot_metadata: dict[str, Any] | None = None,
    turn: ActionTurnExtraction | None = None,
) -> dict[str, Any]:
    """构造仅在当前会话内使用的待完成动作，并保留兼容字段供现有图流程消费。"""
    now = datetime.utcnow()
    pending_id = (previous or {}).get("pending_id") or f"PA-{uuid.uuid4().hex[:12]}"
    action_type = analysis.action_type
    clean_slots = _clean_slots(slots)
    collected_slots = dict(clean_slots)
    if action_type in {"return_goods", "refund_request", "exchange_goods"} and clean_slots.get("after_sale_reason"):
        # 对外呈现统一使用 return_reason，执行图继续使用兼容字段 after_sale_reason。
        collected_slots["return_reason"] = clean_slots["after_sale_reason"]
    pending_missing_slots = ["return_reason" if slot == "after_sale_reason" else slot for slot in missing_slots]
    issue_type = {
        "return_goods": "return",
        "refund_request": "refund",
        "exchange_goods": "exchange",
        "repair_request": "repair",
    }.get(str(action_type), str(action_type or "other"))
    created_at = (previous or {}).get("created_at") or now.isoformat()
    expires_at = (now + timedelta(seconds=PENDING_EXPIRE_SECONDS)).isoformat()
    flow_state = "COLLECTING" if next_action == "collect_slots" else "READY"
    return {
        "pending_id": pending_id,
        "status": "waiting_for_user_input" if next_action == "collect_slots" else "ready",
        "action_type": action_type,
        "issue_type": issue_type,
        "intent": analysis.intent,
        "order_no": clean_slots.get("order_no"),
        "action_slots": clean_slots,
        "collected_slots": collected_slots,
        "missing_slots": pending_missing_slots,
        "next_action": next_action,
        "last_message": message,
        "created_at": created_at,
        "updated_at": now.isoformat(),
        "expires_at": expires_at,
        # 兼容已经落库的旧字段，待历史数据自然过期后可移除。
        "expire_at": expires_at,
        "source": (previous or {}).get("source") or "current_session_explicit",
        "confidence": float((previous or {}).get("confidence") or 0.95),
        "flow_version": 2,
        "flow_state": flow_state,
        "operation": turn.operation if turn else (previous or {}).get("operation", "update"),
        "explicit_action": bool(turn.explicit_action) if turn else bool((previous or {}).get("explicit_action")),
        "slot_metadata": slot_metadata or (previous or {}).get("slot_metadata") or {},
        "ambiguous_fields": list(turn.ambiguous_fields) if turn else list((previous or {}).get("ambiguous_fields") or []),
        "completed": False,
    }


def _usable_pending(pending: dict[str, Any] | None) -> dict[str, Any] | None:
    """读取 30 分钟内仍可继续或需要确认的 pending 动作。"""
    if not pending or pending.get("completed") or pending.get("status") in {"cancelled", "completed"}:
        return None
    return None if _pending_time_state(pending) == "expired" else pending


def _pending_time_state(pending: dict[str, Any] | None) -> str:
    """按最后更新时间把动作划分为可直续、需确认和已过期三档。"""
    if not pending or pending.get("completed") or pending.get("status") in {"cancelled", "completed"}:
        return "none"
    now = datetime.utcnow()
    expires_at = pending.get("expires_at") or pending.get("expire_at")
    if expires_at:
        try:
            if datetime.fromisoformat(str(expires_at)) <= now:
                return "expired"
        except ValueError:
            pass
    updated_at = pending.get("updated_at") or pending.get("created_at")
    if not updated_at:
        # 兼容旧数据：没有时间字段的未完成动作按当前活跃流程处理。
        return "fresh"
    try:
        age_seconds = max(0.0, (now - datetime.fromisoformat(str(updated_at))).total_seconds())
    except ValueError:
        return "fresh"
    if age_seconds <= PENDING_DIRECT_CONTINUE_SECONDS:
        return "fresh"
    if age_seconds <= PENDING_EXPIRE_SECONDS:
        return "needs_confirmation"
    return "expired"


def _continue_pending_action(
    normalized: IntentResult,
    pending: dict[str, Any],
    action_type: str | None,
    slots: dict[str, Any],
    message: str,
    *,
    slot_metadata: dict[str, Any] | None = None,
    turn: ActionTurnExtraction | None = None,
) -> tuple[IntentResult, dict[str, Any]]:
    """在客户确认后恢复 pending，并根据已收集槽位决定继续追问还是创建工单。"""
    rule = ACTION_SLOT_RULES.get(str(action_type))
    missing_slots = compute_missing_slots(str(action_type), slots) if action_type else []
    next_action = "collect_slots" if missing_slots else (rule or {}).get("default_next_action", "collect_slots")
    normalized.intent = (rule or {}).get("intent", normalized.intent)
    normalized.user_goal = "action_request"
    normalized.action_type = action_type if action_type in ACTION_SLOT_RULES else "other"
    normalized.action_slots = slots
    normalized.missing_slots = missing_slots
    normalized.next_action = next_action
    normalized.order_related = bool(slots.get("order_no"))
    normalized.need_order_query = bool(slots.get("order_no")) and bool((rule or {}).get("need_order_validation"))
    normalized.need_human = next_action == "create_ticket"
    normalized.need_ticket = next_action == "create_ticket"
    normalized.risk_reasons = [item for item in normalized.risk_reasons if item != "complaint"]
    order_no = slots.get("order_no")
    if order_no and str(order_no) not in normalized.order_no:
        normalized.order_no.append(str(order_no))
    resumed = _build_pending(
        normalized,
        slots,
        message,
        next_action,
        missing_slots,
        pending,
        slot_metadata=slot_metadata,
        turn=turn,
    )
    resumed.pop("deferred_slots", None)
    resumed.pop("confirmation_reason", None)
    if next_action == "create_ticket":
        resumed["status"] = "ready"
        resumed["flow_state"] = "READY"
    return normalized, resumed


def _cancel_pending_action(
    normalized: IntentResult,
    pending: dict[str, Any],
    message: str,
    reason: str,
) -> tuple[IntentResult, dict[str, Any]]:
    """确定性结束当前动作，避免取消语句再次进入意图模型自由判断。"""
    cancelled = _build_pending(normalized, pending.get("action_slots") or {}, message, "cancel_pending", [], pending)
    cancelled["status"] = "cancelled"
    cancelled["flow_state"] = "CANCELLED"
    cancelled["completed"] = True
    cancelled["cancel_reason"] = reason
    normalized.user_goal = "action_request"
    normalized.action_type = pending.get("action_type") or normalized.action_type
    normalized.action_slots = pending.get("action_slots") or {}
    normalized.missing_slots = []
    normalized.next_action = "cancel_pending"
    normalized.need_human = False
    normalized.need_ticket = False
    normalized.need_order_query = False
    return normalized, cancelled


def _looks_like_action_request(text: str) -> bool:
    """区分“帮我做某事”和“怎么做/规则是什么”。"""
    if _contains_any(text, ["怎么", "如何", "规则", "政策", "流程", "多久", "能不能", "可以吗"]):
        return False
    return _contains_any(
        text,
        [
            "我要",
            "帮我",
            "申请",
            "提交",
            "开一开",
            "开发票",
            "开票",
            "开个人发票",
            "开企业发票",
            "取消",
            "处理一下",
            "给我",
            "想退",
            "想换",
            "要退",
            "要换",
        ],
    )


def _is_cancel_message(message: str) -> bool:
    """识别用户取消当前业务动作的表达。"""
    return _contains_any(message, ["不用", "不用了", "取消", "算了", "先不", "不退了", "不换了", "不修了"])


def _is_confirm_message(message: str) -> bool:
    """识别纯确认或“确认 + 补充槽位”的复合肯定表达。"""
    text = message.strip().lower()
    confirm_terms = {"是", "是的", "对", "对的", "确认", "没错", "就是", "就是这个", "yes", "y"}
    if text in confirm_terms:
        return True
    # 只接受“确认词 + 分隔符 + 后续内容”，避免把“确认不了”等否定表达误判成确认。
    return bool(re.match(r"^(?:是的|确认|没错|对的|对|就是这个|就是|yes|y)[\s，,。；;：:]+.+$", text))


def _extract_confirmed_reason(message: str) -> str | None:
    """从“确认，原因……”中提取售后原因，支持一次回复同时完成确认与补槽。"""
    if not _is_confirm_message(message):
        return None
    remainder = re.sub(
        r"^(?:是的|确认|没错|对的|对|就是这个|就是|yes|y)[\s，,。；;：:]*",
        "",
        message.strip(),
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    if not remainder:
        return None

    reason_marker = re.match(r"^(?:退货原因|换货原因|原因|理由)(?:是|为|：|:)?[\s，,]*", remainder)
    if reason_marker:
        reason_text = remainder[reason_marker.end():].strip()
        return _extract_pending_reason(reason_text) if reason_text else None

    # 没有“原因”标记时只接收已知原因词，防止把“订单是 XXX”误存为退货原因。
    extracted = _extract_after_sale_reason(remainder)
    return remainder if extracted and len(remainder) <= 80 else extracted


def _is_reject_confirmation_message(message: str) -> bool:
    """识别用户否认候选订单的表达。"""
    text = message.strip().lower()
    return text in {"不是", "不对", "不是这个", "不是这单", "否", "no", "n"}


def _is_pending_order_confirmation(pending: dict[str, Any]) -> bool:
    """判断上一轮 pending 是否正在等待订单上下文确认。"""
    slots = pending.get("action_slots") or {}
    missing_slots = pending.get("missing_slots") or []
    return bool(slots.get("candidate_order_no") and "order_confirmation" in missing_slots)


def _is_pending_resume_confirmation(pending: dict[str, Any]) -> bool:
    """判断 pending 是否正在等待继续旧流程或重新发起动作的明确确认。"""
    missing_slots = set(pending.get("missing_slots") or [])
    return pending.get("status") == "awaiting_confirmation" and bool(
        missing_slots.intersection({"pending_confirmation", "action_confirmation"})
    )


def _has_explicit_action_phrase(message: str, selected_order_no: str | None = None) -> bool:
    """识别客户明确要求执行动作的表达，避免把原因陈述误当成执行授权。"""
    if _selected_order_short_action_type(message, selected_order_no):
        return True
    if not _looks_like_action_request(message):
        return False
    return _contains_any(
        message,
        [
            "我要退货", "想退货", "申请退货", "帮我退货", "办理退货",
            "我要退款", "想退款", "申请退款", "帮我退款",
            "我要换货", "想换货", "申请换货", "帮我换货",
            "我要维修", "申请维修", "帮我报修", "我要开票", "帮我开票",
        ],
    )


def _is_standalone_unwanted_message(message: str) -> bool:
    """识别只有“不想要/用不上”等原因、但没有明确动作授权的模糊表达。"""
    text = re.sub(r"[，。！？!?\s]", "", message)
    return text in {"我不想要了", "不想要了", "不想要", "用不上了", "我用不上了"}


def _is_pending_after_sale_reason(pending: dict[str, Any]) -> bool:
    """判断上一轮 pending 是否正在等待售后原因。"""
    action_type = pending.get("action_type")
    missing_slots = pending.get("missing_slots") or []
    return action_type in {"return_goods", "refund_request", "exchange_goods"} and bool(
        {"after_sale_reason", "return_reason"}.intersection(missing_slots)
    )


def _is_pending_return_fulfillment(pending: dict[str, Any]) -> bool:
    """判断退货流程是否正在等待退回方式或上门取件时间。"""
    return pending.get("action_type") == "return_goods" and bool(
        {"return_method", "pickup_time_window"}.intersection(pending.get("missing_slots") or [])
    )


def _is_short_pending_reply(message: str, analysis: IntentResult) -> bool:
    """识别槽位追问后的短上下文回复，避免“算了”等被误走越界兜底。"""
    text = message.strip()
    if analysis.user_goal != "out_of_scope":
        return False
    return 0 < len(text) <= 8


def _is_reset_pending_message(message: str) -> bool:
    """识别仅用于结束当前流程的重新开始或换题表达。"""
    return _contains_any(message, ["换个问题", "换一个问题", "重新开始", "换个话题", "先说别的"])


def _is_explicit_topic_switch(message: str, pending: dict[str, Any] | None) -> bool:
    """识别客户明确切换业务目标，显式新意图优先于旧 pending 槽位填充。"""
    if pending and _is_pending_return_fulfillment(pending) and _extract_return_fulfillment_slots(message, pending):
        # “不用上门、我自己寄回”是在回答退回方式，不能因为包含“不用”就误判为取消退货。
        return False
    if _is_cancel_message(message) or _is_reset_pending_message(message):
        return True
    action_type = str((pending or {}).get("action_type") or "")
    if _is_logistics_query_message(message) or _contains_any(message, ["查订单", "订单状态", "开票", "发票", "投诉", "举报", "维权"]):
        return True
    action_switch_terms = {
        "return_goods": ["换货", "维修", "报修"],
        "refund_request": ["退货", "换货", "维修", "报修"],
        "exchange_goods": ["退货", "退款", "维修", "报修"],
        "repair_request": ["退货", "退款", "换货"],
    }
    return _contains_any(message, action_switch_terms.get(action_type, []))


def _is_explicit_same_action_request(message: str, pending: dict[str, Any] | None) -> bool:
    """识别客户在旧流程过期后明确重新表达同一业务动作。"""
    action_type = str((pending or {}).get("action_type") or "")
    terms = {
        "return_goods": ["我要退货", "想退货", "申请退货", "帮我退货"],
        "refund_request": ["我要退款", "想退款", "申请退款", "帮我退款"],
        "exchange_goods": ["我要换货", "想换货", "申请换货", "帮我换货"],
        "repair_request": ["我要维修", "申请维修", "帮我报修"],
    }
    return _contains_any(message, terms.get(action_type, []))


def _extract_order_no(message: str) -> str | None:
    match = re.search(r"(?<![A-Za-z])(?:EC)?\d{10,18}", message, flags=re.IGNORECASE)
    return match.group(0) if match else None


def _extract_after_sale_reason(message: str) -> str | None:
    """抽取退换货原因。"""
    explicit = re.search(r"(?:因为|原因是|理由是|退货原因是|换货原因是)([^，,。；;！？!?]+)", message)
    if explicit:
        return explicit.group(1).strip(" ，。?.")
    reason_words = [
        "质量不好",
        "商品质量问题",
        "质量问题",
        "买错",
        "拍错",
        "拍错了",
        "不想要",
        "七天无理由",
        "配件缺失",
        "尺码不合适",
        "尺寸不合适",
        "颜色不喜欢",
        "与描述不符",
        "少件",
        "漏发",
        "错发",
        "破损",
        "有瑕疵",
        "不能用",
        "坏了",
    ]
    for word in reason_words:
        if word in message:
            return word
    # 使用通用语义形态覆盖“商品有问题、产品不能正常使用”等表达，避免为单句话打补丁。
    generic_patterns = [
        r"(?:商品|产品|货物|东西).{0,8}(?:有问题|有故障|有瑕疵|损坏|坏了|不能用|无法使用)",
        r"(?:不需要|用不上|不合适|不喜欢|不满意)(?:了)?",
        r"(?:收到|到货|拆开).{0,8}(?:破损|少件|漏发|错发|有问题)",
    ]
    for pattern in generic_patterns:
        match = re.search(pattern, message)
        if match:
            return match.group(0).strip(" ，。?.")
    return None


def _selected_order_short_action_type(message: str, selected_order_no: str | None) -> str | None:
    """识别已选订单上的单词式售后动作，规则、条件和流程咨询不在此范围内。"""
    if not selected_order_no:
        return None
    text = re.sub(r"[，。！？!?.\s]", "", message).lower()
    action_map = {
        "退货": "return_goods",
        "退款": "refund_request",
        "换货": "exchange_goods",
        "维修": "repair_request",
        "报修": "repair_request",
    }
    return action_map.get(text)


def _extract_pending_reason(message: str) -> str | None:
    """在等待退货原因时优先解释安全短句，避免依赖本轮 LLM 新意图结果。"""
    extracted = _extract_after_sale_reason(message)
    if extracted:
        return message.strip() if len(message.strip()) <= 80 else extracted
    text = message.strip(" ，。?？.!！")
    if not 2 <= len(text) <= 80:
        return None
    if _is_explicit_topic_switch(text, None) or _is_confirm_message(text) or _is_reject_confirmation_message(text):
        return None
    if _contains_any(text, ["我要退货", "想退货", "申请退货", "帮我退货", "我要退款", "申请退款", "我要换货", "申请换货"]):
        return None
    if _contains_any(text, ["怎么", "如何", "为什么", "规则", "政策", "流程", "多久", "能不能", "可以吗", "我是谁", "你是谁"]):
        return None
    return text


def _extract_return_fulfillment_slots(
    message: str,
    pending: dict[str, Any] | None = None,
    *,
    allow_unbound_time: bool = False,
) -> dict[str, Any]:
    """抽取退回方式和取件时间偏好；时间仅表示客户偏好，不代表承运方已确认预约。"""
    text = message.strip(" ，。?？.!！")
    slots: dict[str, Any] = {}
    if _contains_any(text, ["不上门", "不需要上门", "不用上门", "自行寄回", "自己寄回", "我自己寄", "快递寄回"]):
        slots["return_method"] = "self_ship"
        slots["pickup_status"] = "NOT_REQUIRED"
        return slots
    if _contains_any(text, ["上门取件", "上门取", "安排取件", "需要取件", "来取"]):
        slots["return_method"] = "pickup"
        slots["pickup_status"] = "PREFERENCE_RECORDED"

    pending_slots = (pending or {}).get("action_slots") or {}
    expects_pickup_time = slots.get("return_method") == "pickup" or (
        pending_slots.get("return_method") == "pickup"
        and "pickup_time_window" in ((pending or {}).get("missing_slots") or [])
    )
    if expects_pickup_time or allow_unbound_time:
        time_window = _extract_pickup_time_window(text)
        if time_window:
            slots["pickup_time_window"] = time_window
            if slots.get("return_method") == "pickup" or pending_slots.get("return_method") == "pickup":
                slots.setdefault("pickup_status", "PREFERENCE_RECORDED")
    return slots


def _extract_pickup_time_window(message: str) -> str | None:
    """保守提取客户提供的取件时间段，避免把普通说明误存为预约时间。"""
    text = message.strip(" ，。?？.!！")
    if not text or len(text) > 80:
        return None
    time_terms = [
        "今天", "明天", "后天", "周一", "周二", "周三", "周四", "周五", "周六", "周日",
        "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日",
        "上午", "中午", "下午", "晚上", "工作日", "周末", "点", ":", "：",
    ]
    if not _contains_any(text, time_terms):
        return None
    # 优先定位包含时间的最小分句，避免把退货动作和原因一并保存到 pickup_time_window。
    clauses = [item.strip() for item in re.split(r"[，,。；;！？!?]", text) if item.strip()]
    time_clause = next((item for item in clauses if _contains_any(item, time_terms)), text)
    cleaned = re.sub(
        r"(?:我要退货|申请退货|帮我退货|商品有问题|商品质量问题|质量问题|"
        r"需要|请|可以|麻烦|帮我|安排|上门取件|上门取|取件|来取件|来取|时间|方便|的话|我想)",
        "",
        time_clause,
    ).strip(" ，。来")
    return cleaned or text


def _extract_fault_description(message: str) -> str | None:
    fault_words = ["连不上", "无法使用", "坏了", "故障", "报错", "不能开机", "没反应", "断网", "发热"]
    return message.strip() if _contains_any(message, fault_words) else None


def _extract_complaint_description(message: str) -> str | None:
    return message.strip() if _contains_any(message, ["投诉", "举报", "维权", "不处理", "拒绝", "太慢"]) else None


def _extract_invoice_type(message: str) -> str | None:
    if _contains_any(message, ["企业", "公司", "专票", "增值税专用"]):
        return "company_invoice"
    if _contains_any(message, ["个人", "普票", "普通发票"]):
        return "personal_invoice"
    return None


def _extract_invoice_title(message: str) -> str | None:
    match = re.search(r"(?:抬头|发票抬头)(?:是|为|：|:)?([\u4e00-\u9fa5A-Za-z0-9（）()·.\-_\s]{2,40})", message)
    return match.group(1).strip() if match else None


def _extract_tax_no(message: str) -> str | None:
    match = re.search(r"(?:税号|纳税人识别号)(?:是|为|：|:)?([A-Z0-9]{10,30})", message, flags=re.IGNORECASE)
    return match.group(1).strip() if match else None


def _clean_slots(slots: dict[str, Any]) -> dict[str, Any]:
    """清理空槽位，避免空字符串被误判为已收集。"""
    return {key: value for key, value in slots.items() if value is not None and str(value).strip() != ""}


def _resolve_confirmation_order(
    *,
    selected_order_no: str | None,
    explicit_order_no: str | None,
    candidate_order_no: str | None = None,
) -> str | None:
    """按确认阶段优先级选择订单，禁止已过期的旧订单被隐式恢复。"""
    # 前端当前选择代表本轮明确操作对象；消息订单次之，历史候选只能作为最后兜底。
    for order_no in (selected_order_no, explicit_order_no, candidate_order_no):
        normalized_order_no = str(order_no or "").strip()
        if normalized_order_no:
            return normalized_order_no
    return None


def _contains_any(text: str, words: list[str]) -> bool:
    return any(word in text for word in words)


def _context_value(conversation_context: dict[str, Any] | None, key: str) -> str | None:
    """读取结构化上下文实体。"""
    item = (conversation_context or {}).get(key) or {}
    value = item.get("value")
    return str(value) if value else None


def _detect_context_conflict(
    message: str,
    explicit_order_no: str | None,
    selected_order_no: str | None,
    pending: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """识别本轮是否明确切换目标。"""
    if not pending:
        return None
    pending_order_no = str((pending.get("action_slots") or {}).get("order_no") or "").strip()
    current_order_no = explicit_order_no or selected_order_no
    if current_order_no and pending_order_no and current_order_no.lower() != pending_order_no.lower():
        return {
            "type": "order_changed",
            "pending_order_no": pending_order_no,
            "current_order_no": current_order_no,
        }
    if _is_cancel_message(message) and _has_new_status_query(message, explicit_order_no, None):
        return {
            "type": "cancel_pending_with_new_query",
            "pending_order_no": pending_order_no or None,
            "current_order_no": explicit_order_no,
        }
    return None


def _record_context_conflict(conversation_context: dict[str, Any] | None, conflict: dict[str, Any]) -> None:
    """把上下文冲突写入 debug_context，便于后续排查。"""
    if conversation_context is None:
        return
    debug_context = conversation_context.setdefault("debug_context", {})
    debug_context["context_conflict"] = conflict


def _has_new_status_query(message: str, explicit_order_no: str | None, context_ticket_no: str | None) -> bool:
    """判断取消旧动作后，本轮是否还有新的查询诉求。"""
    return bool(
        explicit_order_no
        or context_ticket_no
        or _is_logistics_query_message(message)
        or _contains_any(message, ["查", "查询", "物流", "进度", "状态", "到哪", "催一下", "加急"])
    )


def _is_logistics_query_message(message: str) -> bool:
    """统一复用业务意图层的物流识别，避免 pending 状态机维护另一套不完整关键词。"""
    return normalize_is_logistics_message(message)
