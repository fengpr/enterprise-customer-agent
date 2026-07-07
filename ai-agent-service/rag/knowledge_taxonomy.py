from dataclasses import dataclass


@dataclass(frozen=True)
class KnowledgeCollection:
    """客服知识集合定义，用于约束检索范围和知识来源。"""

    name: str
    business_scopes: tuple[str, ...]
    answerable_intents: tuple[str, ...]
    source_priority: tuple[str, ...]


SOURCE_PRIORITY = (
    "official_policy",
    "operation_rule",
    "staff_script",
    "historical_ticket_summary",
)


KNOWLEDGE_COLLECTIONS: dict[str, KnowledgeCollection] = {
    "logistics_policy": KnowledgeCollection(
        name="logistics_policy",
        business_scopes=("logistics",),
        answerable_intents=("logistics", "consult", "other"),
        source_priority=SOURCE_PRIORITY,
    ),
    "refund_policy": KnowledgeCollection(
        name="refund_policy",
        business_scopes=("refund", "return_goods", "compensation_dispute"),
        answerable_intents=("refund", "consult", "other"),
        source_priority=SOURCE_PRIORITY,
    ),
    "exchange_policy": KnowledgeCollection(
        name="exchange_policy",
        business_scopes=("exchange",),
        answerable_intents=("exchange", "consult", "other"),
        source_priority=SOURCE_PRIORITY,
    ),
    "repair_policy": KnowledgeCollection(
        name="repair_policy",
        business_scopes=("repair",),
        answerable_intents=("repair", "consult", "other"),
        source_priority=SOURCE_PRIORITY,
    ),
    "invoice_policy": KnowledgeCollection(
        name="invoice_policy",
        business_scopes=("invoice",),
        answerable_intents=("invoice", "consult", "other"),
        source_priority=SOURCE_PRIORITY,
    ),
    "member_policy": KnowledgeCollection(
        name="member_policy",
        business_scopes=("member",),
        answerable_intents=("member", "consult", "other"),
        source_priority=SOURCE_PRIORITY,
    ),
    "complaint_policy": KnowledgeCollection(
        name="complaint_policy",
        business_scopes=("complaint", "compensation_dispute"),
        answerable_intents=("complaint", "consult", "other"),
        source_priority=SOURCE_PRIORITY,
    ),
    "general_faq": KnowledgeCollection(
        name="general_faq",
        business_scopes=("general",),
        answerable_intents=("consult", "other"),
        source_priority=SOURCE_PRIORITY,
    ),
}


INTENT_TO_COLLECTIONS: dict[str, tuple[str, ...]] = {
    "logistics": ("logistics_policy",),
    "refund": ("refund_policy",),
    "exchange": ("exchange_policy",),
    "repair": ("repair_policy",),
    "complaint": ("complaint_policy",),
    "invoice": ("invoice_policy",),
    "member": ("member_policy",),
    "consult": ("general_faq",),
    "other": ("general_faq",),
}


def collections_for_intent(intent: str, business_scope: str | None = None) -> list[str]:
    """根据意图和业务范围推导可检索知识集合，避免全库混搜。"""
    if business_scope:
        matched = [
            collection.name
            for collection in KNOWLEDGE_COLLECTIONS.values()
            if business_scope in collection.business_scopes
        ]
        if matched:
            return matched
    return list(INTENT_TO_COLLECTIONS.get(intent, ("general_faq",)))


def infer_business_scope(intent: str, user_goal: str | None = None) -> str:
    """把 Agent 意图转换为知识库业务范围。"""
    if intent in {"logistics", "refund", "exchange", "repair", "invoice", "member", "complaint"}:
        return intent
    if user_goal in {"complaint", "dispute"}:
        return "complaint"
    return "general"
