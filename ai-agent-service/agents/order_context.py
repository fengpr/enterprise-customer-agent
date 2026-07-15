"""订单上下文有效期与确认策略，统一保护查单、售后动作和多轮指代。"""

import re
from datetime import datetime, timedelta
from typing import Any


RECENT_CONTEXT_SECONDS = 5 * 60
CONFIRM_CONTEXT_SECONDS = 30 * 60
CONFIRMED_SOURCES = {"selected_by_user", "mentioned_by_user", "confirmed_by_user"}
FUZZY_ORDER_KEYWORDS = [
    "这个订单",
    "那个订单",
    "这笔订单",
    "那笔订单",
    "当前订单",
    "这单",
    "那单",
    "刚才那个",
    "刚刚那个",
    "上面那个",
    "继续处理",
    "继续",
    "能退吗",
    "能退款吗",
    "怎么样了",
    "到哪了",
]


def has_fuzzy_order_reference(message: str) -> bool:
    """识别需要依赖订单上下文解析的模糊表达。"""
    return any(keyword in message for keyword in FUZZY_ORDER_KEYWORDS)


def resolve_order_context(
    conversation_context: dict[str, Any] | None,
    message: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """按来源、置信度和时间窗口判断历史订单是否可用。"""
    context = (conversation_context or {}).get("order_context") or {}
    order_no = context.get("order_no")
    if not order_no or not has_fuzzy_order_reference(message):
        return {"status": "none", "order_no": None, "reason": "no_fuzzy_reference"}

    source = str(context.get("source") or "")
    confidence = float(context.get("confidence") or 0)
    reference_time = _parse_datetime(context.get("confirmed_at") or context.get("last_used_at"))
    if reference_time is None:
        return {"status": "needs_confirmation", "order_no": str(order_no), "reason": "unconfirmed_context", "source": source}

    age_seconds = max(0.0, ((now or datetime.utcnow()) - reference_time).total_seconds())
    if age_seconds > CONFIRM_CONTEXT_SECONDS:
        return {"status": "expired", "order_no": str(order_no), "reason": "context_expired", "source": source, "age_seconds": age_seconds}

    if source in CONFIRMED_SOURCES and confidence >= 0.85 and age_seconds <= RECENT_CONTEXT_SECONDS:
        return {"status": "usable", "order_no": str(order_no), "reason": "recent_confirmed_context", "source": source, "age_seconds": age_seconds}

    return {"status": "needs_confirmation", "order_no": str(order_no), "reason": "context_requires_confirmation", "source": source, "age_seconds": age_seconds}


def build_order_context_record(
    *,
    order_no: str,
    source: str,
    confirmed_at: str | None,
    last_used_at: str | None,
    confidence: float,
) -> dict[str, Any]:
    """生成会话级订单上下文记录，过期时间只表示可确认窗口，不代表可直接执行动作。"""
    base_time = _parse_datetime(confirmed_at or last_used_at) or datetime.utcnow()
    expires_at = base_time + timedelta(seconds=CONFIRM_CONTEXT_SECONDS)
    return {
        "order_no": order_no,
        "source": source,
        "confirmed_at": confirmed_at,
        "last_used_at": last_used_at,
        "confidence": confidence,
        "expires_at": expires_at.isoformat(),
    }


def extract_order_no(message: str) -> str | None:
    """从消息中提取显式订单号。"""
    match = re.search(r"(?<![A-Za-z])(?:EC)?\d{10,18}", message, flags=re.IGNORECASE)
    return match.group(0) if match else None


def _parse_datetime(value: Any) -> datetime | None:
    """兼容 SQLite ISO 字符串和前端 ISO 时间，统一转为 naive UTC 比较。"""
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed
