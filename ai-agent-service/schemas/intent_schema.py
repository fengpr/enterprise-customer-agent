from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Intent = Literal["consult", "logistics", "refund", "exchange", "repair", "complaint", "invoice", "member", "other"]
UserGoal = Literal[
    "policy_consult",
    "how_to",
    "status_query",
    "action_request",
    "human_request",
    "out_of_scope",
    "complaint",
    "dispute",
    "info_query",
    "other",
]
Emotion = Literal["normal", "anxious", "dissatisfied", "strong_complaint"]
Priority = Literal["low", "medium", "high", "urgent"]
ActionType = Literal[
    "return_goods",
    "refund_request",
    "exchange_goods",
    "repair_request",
    "invoice_issue",
    "cancel_order",
    "complaint_submit",
    "other",
]
NextAction = Literal[
    "collect_slots",
    "validate_order",
    "call_business_tool",
    "create_ticket",
    "ask_clarification",
    "transfer_human",
    "cancel_pending",
    "unsupported",
]
ActionOperation = Literal["start", "update", "confirm", "cancel", "switch", "unknown"]
SlotSource = Literal["selected_order", "explicit_message", "llm", "pending", "derived"]


class ReturnGoodsSlots(BaseModel):
    """退货动作的标准槽位，统一校验自然语言抽取结果和历史 pending 数据。"""

    order_no: str | None = None
    after_sale_reason: str | None = None
    return_method: Literal["pickup", "self_ship"] | None = None
    pickup_time_window: str | None = None
    pickup_status: str | None = None
    product_name: str | None = None
    description: str | None = None


class SlotMetadata(BaseModel):
    """记录槽位来源和可信度，避免低置信度模型结果直接触发业务动作。"""

    source: SlotSource
    confidence: float = Field(ge=0, le=1)
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class ActionTurnExtraction(BaseModel):
    """单轮动作抽取结果，由状态归并器统一消费，不直接决定工具执行。"""

    operation: ActionOperation = "unknown"
    action_type: ActionType | None = None
    explicit_action: bool = False
    slots: dict[str, Any] = Field(default_factory=dict)
    slot_metadata: dict[str, SlotMetadata] = Field(default_factory=dict)
    ambiguous_fields: list[str] = Field(default_factory=list)


class AnalyzeRequest(BaseModel):
    """意图识别请求，承载用户原始消息和可选会话编号。"""

    message: str = Field(min_length=1)
    session_id: str | None = None


class IntentResult(BaseModel):
    """Agent 结构化分析结果，intent 表示业务域，user_goal 表示用户真实目的。"""

    intent: Intent
    user_goal: UserGoal = "other"
    emotion: Emotion
    order_related: bool
    order_no: list[str] = Field(default_factory=list)
    product_name: str | None = None
    need_order_query: bool
    need_ticket: bool
    need_human: bool
    priority: Priority
    confidence: float = Field(ge=0, le=1)
    summary: str
    risk_reasons: list[str] = Field(default_factory=list)
    action_type: ActionType | None = None
    action_slots: dict[str, Any] = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list)
    next_action: NextAction | None = None


class LLMIntentDraft(BaseModel):
    """LLM 原始意图草稿，允许缺少可由业务规则推导的字段以降低 Schema 失败率。"""

    intent: Intent = "other"
    user_goal: UserGoal = "other"
    emotion: Emotion = "normal"
    order_related: bool = False
    order_no: list[str] | None = None
    product_name: str | None = None
    need_order_query: bool | None = None
    need_ticket: bool | None = None
    need_human: bool = True
    priority: Priority = "medium"
    confidence: float = Field(default=0.6, ge=0, le=1)
    summary: str = ""
    risk_reasons: list[str] = Field(default_factory=list)
    action_type: ActionType | None = None
    action_slots: dict[str, Any] | None = None
    missing_slots: list[str] | None = None
    next_action: NextAction | None = None

    @field_validator("order_no", "missing_slots", mode="before")
    @classmethod
    def _none_to_list(cls, value: Any) -> list[Any]:
        """兼容 LLM 把空数组输出为 null 的情况，避免整轮意图识别失败。"""
        return [] if value is None else value

    @field_validator("action_slots", mode="before")
    @classmethod
    def _none_to_dict(cls, value: Any) -> dict[str, Any]:
        """兼容 LLM 把空对象输出为 null 的情况。"""
        return {} if value is None else value


class AgentReplyRequest(BaseModel):
    """客服回复生成请求，包含用户问题以及可选的会话和客户上下文。"""

    message: str = Field(min_length=1)
    session_id: str | None = None
    customer_id: int | None = None
    auth_token: str | None = None
    selected_order_no: str | None = None
    selected_ticket_no: str | None = None
    route_target: Literal["ai", "human", "both"] = "ai"
    pending_action_request: dict[str, Any] | None = None
    conversation_context: dict[str, Any] | None = None
    login_user_context: dict[str, Any] | None = None


class AgentExecutionJob(BaseModel):
    """Redis Stream 中的安全任务结构，不保存客户 Authorization 原始 Token。"""

    request_id: str
    customer_id: int
    message: str
    session_id: str | None = None
    selected_order_no: str | None = None
    selected_ticket_no: str | None = None
    route_target: Literal["ai", "human", "both"] = "ai"
    idempotency_key: str
    created_at: str | None = None
    expires_at: str | None = None
    route_source: str = "api"
    risk_level: str = "normal"
    trace_id: str | None = None
    execution_credential: str | None = None
    login_user_context: dict[str, Any] | None = None

    def to_request(self) -> AgentReplyRequest:
        """转换为执行服务所需请求；凭证仅代表内部执行身份，不是客户原始 Token。"""
        # Worker 只在内存中组合短期执行身份；队列本身仍不保存 Authorization 原始 Token。
        from services.downstream_identity import build_execution_identity

        execution_identity = None
        if self.execution_credential:
            execution_identity = build_execution_identity(
                self.customer_id,
                self.request_id,
                self.execution_credential,
            )
        return AgentReplyRequest(
            message=self.message,
            session_id=self.session_id,
            customer_id=self.customer_id,
            auth_token=execution_identity,
            selected_order_no=self.selected_order_no,
            selected_ticket_no=self.selected_ticket_no,
            route_target=self.route_target,
            login_user_context=self.login_user_context,
        )


class Citation(BaseModel):
    """知识库引用片段，用于证明 AI 回复的业务依据。"""

    citation_id: str | None = None
    doc_name: str
    version: str
    paragraph: str
    score: float
    collection: str | None = None
    business_scope: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    risk_level: str = "low"
    answerable_intents: list[str] = Field(default_factory=list)
    retrieval_source: str = "bm25"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentReply(BaseModel):
    """Agent 回复结果，统一返回候选话术、审核状态、引用来源和工具结果。"""

    session_id: str | None = None
    answer: str
    customer_message: str | None = None
    internal_suggestion: str | None = None
    decision_type: str = "auto_reply"
    service_status: str = "自动回复"
    auto_send: bool
    need_human: bool
    analysis: IntentResult
    citations: list[Citation] = Field(default_factory=list)
    citation_validation: dict[str, Any] = Field(default_factory=dict)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    ticket_result: dict[str, Any] | None = None
    risk_reasons: list[str] = Field(default_factory=list)
    pending_action_request: dict[str, Any] | None = None


class ToolCallRequest(BaseModel):
    """工具调用请求，限制工具名称和入参结构，便于做白名单控制。"""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
