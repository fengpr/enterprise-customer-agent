import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from schemas.intent_schema import IntentResult


ACTION_SLOT_RULES: dict[str, dict[str, Any]] = {
    "return_goods": {
        "intent": "refund",
        "ticket_type": "refund",
        "required_slots": ["order_no", "after_sale_reason"],
        "optional_slots": ["product_name", "description", "evidence_hint"],
        "conditional_required_slots": {},
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
    pending = _usable_pending(pending_action_request)
    context_order_no = _context_value(conversation_context, "last_order")
    context_ticket_no = _context_value(conversation_context, "last_ticket")
    explicit_order_no = _extract_order_no(message)
    context_conflict = _detect_context_conflict(message, explicit_order_no, pending)

    if pending and (_is_cancel_message(message) or _is_short_pending_reply(message, normalized)) and not _has_new_status_query(message, explicit_order_no, context_ticket_no):
        # 用户在槽位追问中说“算了/不用了”时，语义依赖上一轮 pending，不能被当作越界闲聊。
        pending = _build_pending(normalized, pending.get("action_slots") or {}, message, "cancel_pending", [], pending)
        pending["status"] = "cancelled"
        pending["completed"] = True
        pending["cancel_reason"] = "user_cancel"
        normalized.user_goal = "action_request"
        normalized.action_type = pending.get("action_type") or normalized.action_type
        normalized.action_slots = pending.get("action_slots") or {}
        normalized.missing_slots = []
        normalized.next_action = "cancel_pending"
        normalized.need_human = False
        normalized.need_ticket = False
        normalized.need_order_query = False
        return normalized, pending

    if normalized.user_goal != "action_request":
        # 政策咨询和状态查询不能继承旧动作上下文，避免“查看退货规则”被误拉进退货申请流程。
        preserve_human_request = normalized.user_goal == "human_request"
        if pending:
            pending = _build_pending(normalized, pending.get("action_slots") or {}, message, "cancel_pending", [], pending)
            pending["status"] = "cancelled"
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

    action_type = infer_action_type(message, normalized)
    inherited_slots: dict[str, Any] = {}
    inherited_action_type = None
    if pending:
        inherited_action_type = pending.get("action_type")
        inherited_slots.update(_clean_slots(pending.get("action_slots") or {}))

    if action_type is None and pending:
        # 用户只补充订单号或原因时，继续上一轮未完成动作。
        action_type = inherited_action_type
    if action_type is None and selected_order_no and _extract_after_sale_reason(message):
        # 已选中订单且只输入售后原因时，按退货申请收集槽位。
        action_type = "return_goods"

    if action_type is None:
        normalized.action_type = None
        normalized.action_slots = {}
        normalized.missing_slots = []
        normalized.next_action = None
        return normalized, None

    selected_or_context_order = selected_order_no or context_order_no
    current_slots = extract_action_slots(message, normalized, selected_or_context_order)
    slots = {**inherited_slots, **_clean_slots(current_slots)}
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

    pending_payload = _build_pending(normalized, slots, message, next_action, missing_slots, pending)
    if next_action == "create_ticket":
        pending_payload["status"] = "ready"
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
        if slots.get(trigger_slot):
            missing.extend(slot for slot in required_slots if not slots.get(slot))
    return list(dict.fromkeys(missing))


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
) -> dict[str, Any]:
    """构造可跨轮使用的 pending 动作状态。"""
    now = datetime.utcnow()
    pending_id = (previous or {}).get("pending_id") or f"PA-{uuid.uuid4().hex[:12]}"
    return {
        "pending_id": pending_id,
        "status": "collecting" if next_action == "collect_slots" else "ready",
        "action_type": analysis.action_type,
        "intent": analysis.intent,
        "action_slots": _clean_slots(slots),
        "missing_slots": missing_slots,
        "next_action": next_action,
        "last_message": message,
        "updated_at": now.isoformat(),
        "expire_at": (now + timedelta(minutes=30)).isoformat(),
        "completed": False,
    }


def _usable_pending(pending: dict[str, Any] | None) -> dict[str, Any] | None:
    """读取仍可继续的 pending 动作。"""
    if not pending or pending.get("completed") or pending.get("status") in {"cancelled", "completed"}:
        return None
    expire_at = pending.get("expire_at")
    if not expire_at:
        return pending
    try:
        if datetime.fromisoformat(str(expire_at)) < datetime.utcnow():
            return None
    except ValueError:
        return pending
    return pending


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
    return _contains_any(message, ["不用了", "取消", "算了", "先不", "不退了", "不换了", "不修了"])


def _is_short_pending_reply(message: str, analysis: IntentResult) -> bool:
    """识别槽位追问后的短上下文回复，避免“算了”等被误走越界兜底。"""
    text = message.strip()
    if analysis.user_goal != "out_of_scope":
        return False
    return 0 < len(text) <= 8


def _extract_order_no(message: str) -> str | None:
    match = re.search(r"(?<![A-Za-z])(?:EC)?\d{10,18}", message, flags=re.IGNORECASE)
    return match.group(0) if match else None


def _extract_after_sale_reason(message: str) -> str | None:
    """抽取退换货原因。"""
    explicit = re.search(r"(?:因为|原因是|理由是|退货原因是|换货原因是)(.+)", message)
    if explicit:
        return explicit.group(1).strip(" ，。?.")
    reason_words = [
        "质量不好",
        "质量问题",
        "买错",
        "拍错",
        "不想要",
        "七天无理由",
        "配件缺失",
        "破损",
        "有瑕疵",
        "不能用",
        "坏了",
    ]
    for word in reason_words:
        if word in message:
            return word
    return None


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
    pending: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """识别本轮是否明确切换目标。"""
    if not pending:
        return None
    pending_order_no = str((pending.get("action_slots") or {}).get("order_no") or "").strip()
    if explicit_order_no and pending_order_no and explicit_order_no.lower() != pending_order_no.lower():
        return {
            "type": "order_changed",
            "pending_order_no": pending_order_no,
            "current_order_no": explicit_order_no,
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
    """识别物流查询表达。"""
    return _contains_any(message, ["物流", "快递", "配送", "发货", "签收", "到哪", "什么时候到", "路线"])
