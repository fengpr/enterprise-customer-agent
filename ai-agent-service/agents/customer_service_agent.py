import os
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from langchain_core.runnables import RunnableLambda
from pydantic import ValidationError

from agents.action_request import ACTION_SLOT_RULES, enrich_action_analysis
from agents.intent_normalizer import (
    is_delivery_not_received_message,
    infer_intent as normalize_intent,
    infer_user_goal as normalize_user_goal,
    is_action_request_message as normalize_is_action_request_message,
    is_high_risk_out_of_scope_message,
    is_how_to_message,
    is_human_request_message,
    is_identity_message as normalize_is_identity_message,
    is_logistics_message as normalize_is_logistics_message,
    is_order_detail_query_message,
    is_order_query_message as normalize_is_order_query_message,
    is_order_statistics_message,
    is_out_of_scope_message,
)
from agents.llm_intent_analyzer import LLMIntentAnalyzer
from agents.order_context import has_fuzzy_order_reference, resolve_order_context
from graphs.ticket_process_graph import TicketProcessState, build_ticket_process_graph
from rag.quality import build_rag_trace, ensure_citation_ids
from rag.rag_chain import RagChain
from repositories.agent_call_log_repository import AgentCallLogRepository
from schemas.intent_schema import AgentReply, AgentReplyRequest, Citation, IntentResult
from tools.order_tools import OrderTools
from tools.ticket_tools import TicketTools


class CustomerServiceAgent:
    """企业客服 Agent 门面，负责组织 LangChain 分析链与 LangGraph 工单处理流程。"""

    def __init__(self) -> None:
        """初始化 RAG、业务工具、调用日志和客服处理图。"""
        self.rag = RagChain()
        self.order_tools = OrderTools()
        self.ticket_tools = TicketTools()
        self.call_logs: list[dict[str, Any]] = []
        self.log_repository = AgentCallLogRepository()
        self.llm_init_error: str | None = None
        self.llm_analyzer = self._create_llm_analyzer()
        # 流式回调只在单次 Worker 执行期间设置，避免跨请求泄露输出。
        self._stream_delta_callback = None
        self._cancellation_token = None

        # 真实 LLM 识别失败时自动降级，保证客服链路不会因模型异常中断。
        self.analyzer_chain = RunnableLambda(self._analyze_with_llm_fallback)
        self.graph = build_ticket_process_graph(
            analyzer_chain=self.analyzer_chain,
            retrieve_knowledge=self.rag.retrieve,
            query_order=self.order_tools.query_order,
            query_customer_orders=self.order_tools.query_customer_orders,
            query_order_logistics=self.order_tools.query_order_logistics,
            create_ticket=self.ticket_tools.create_ticket,
            auto_assign_ticket=self.ticket_tools.auto_assign_ticket,
            list_customer_tickets=self.ticket_tools.list_customer_tickets,
            query_ticket_status=self.ticket_tools.query_ticket_status,
            urge_ticket=self.ticket_tools.urge_ticket,
            prepare_action=self._prepare_action_state,
            compose_answer=self._compose_answer,
            log_tool_call=self._log,
            append_ticket_information=self.ticket_tools.append_ticket_information,
        )

    def analyze(self, message: str) -> IntentResult:
        """对单条用户消息做意图、情绪、订单号和风险结构化识别。"""
        return self.analyzer_chain.invoke(message)

    def llm_status(self) -> dict[str, Any]:
        """返回当前 LLM 接入状态，帮助前端判断 DeepSeek 是否启用。"""
        if not self.llm_analyzer:
            return {"enabled": False, "provider": None, "model": None, "base_url": None}
        return {
            "enabled": True,
            "provider": self.llm_analyzer.provider,
            "model": self.llm_analyzer.model_name,
            "base_url": self.llm_analyzer.base_url,
            "timeout": self.llm_analyzer.timeout,
            "analysis_temperature": self.llm_analyzer.analysis_temperature,
            "response_temperature": self.llm_analyzer.response_temperature,
        }

    def reply(self, request: AgentReplyRequest, cancellation_token=None) -> AgentReply:
        """执行完整客服 Agent 图，输出回复内容、是否转人工、引用和工具结果。"""
        if cancellation_token:
            cancellation_token.check()
        self._cancellation_token = cancellation_token
        try:
            state = self.graph.invoke(
            {
                "message": request.message,
                "session_id": request.session_id,
                "customer_id": request.customer_id,
                "auth_token": request.auth_token,
                "selected_order_no": request.selected_order_no,
                "selected_ticket_no": request.selected_ticket_no,
                "pending_action_request": request.pending_action_request,
                "conversation_context": request.conversation_context,
                "tool_results": [],
                "citations": [],
                "cancellation_token": cancellation_token,
            }
            )
        finally:
            # 实例会被 Worker 复用，必须避免取消令牌跨请求泄漏。
            self._cancellation_token = None

        if cancellation_token:
            cancellation_token.check()

        customer_message = self._build_customer_message(state)
        # 记录回答与本轮召回证据的对应关系，供线上审计和离线回归使用。
        citation_validation = build_rag_trace(
            request.message,
            state.get("citations", []),
            customer_message,
            intent=state["analysis"].intent,
            user_goal=state["analysis"].user_goal,
        )["citation_validation"]
        if (
            os.getenv("RAG_ENFORCE_GROUNDEDNESS", "").strip().lower() in {"1", "true", "yes", "on"}
            and state.get("citations")
            and citation_validation["hallucination_detected"]
        ):
            # 严格模式下，无依据的业务断言不得自动发送，统一降级为人工审核。
            state["analysis"].need_human = True
            if "rag_groundedness_failed" not in state["risk_reasons"]:
                state["risk_reasons"].append("rag_groundedness_failed")
        internal_suggestion = state["answer"]
        decision_type = self._resolve_decision_type(state)
        service_status = self._resolve_service_status(decision_type, state.get("ticket_result"))

        return AgentReply(
            answer=customer_message,
            customer_message=customer_message,
            internal_suggestion=internal_suggestion,
            decision_type=decision_type,
            service_status=service_status,
            auto_send=decision_type == "auto_reply",
            need_human=decision_type in {"human_takeover", "review_required"},
            analysis=state["analysis"],
            citations=state.get("citations", []),
            citation_validation=citation_validation,
            tool_results=state.get("tool_results", []),
            ticket_result=state.get("ticket_result"),
            risk_reasons=state.get("risk_reasons", []),
            pending_action_request=state.get("pending_action_request"),
        )

    def _prepare_action_state(self, state: TicketProcessState) -> TicketProcessState:
        """合并通用业务动作槽位和 pending 状态，统一决定下一步动作。"""
        analysis, pending = enrich_action_analysis(
            state["analysis"],
            message=state["message"],
            selected_order_no=state.get("selected_order_no"),
            pending_action_request=state.get("pending_action_request"),
            conversation_context=state.get("conversation_context"),
        )
        return {"analysis": analysis, "pending_action_request": pending}

    def _resolve_decision_type(self, state: TicketProcessState) -> str:
        """将 Agent 内部状态转换为客户侧可理解的处理决策类型。"""
        analysis = state["analysis"]
        risk_reasons = set(state.get("risk_reasons", []))
        if "model_analyze_failed" in risk_reasons or analysis.emotion == "strong_complaint":
            # 模型失败或强投诉不能自动承诺处理结果，客户侧只提示人工跟进。
            return "human_takeover"
        if analysis.need_ticket or analysis.need_human:
            # 售后争议已进入工单闭环，客户侧展示受理进度，内部建议稿留给坐席。
            return "review_required"
        return "auto_reply"

    def _resolve_service_status(self, decision_type: str, ticket_result: dict[str, Any] | None) -> str:
        """根据决策类型和建单结果生成客户侧处理进度。"""
        if ticket_result and ticket_result.get("status") == "success":
            data = ticket_result.get("data") or {}
            if data.get("status") == "PENDING_ASSIGN":
                return "已提交，待分派"
            return "已受理，处理中"
        if decision_type == "auto_reply":
            return "自动回复"
        if decision_type == "review_required":
            # 没有 Java 成功建单证据时不能展示“已提交”，避免客户误以为工单已经落库。
            return "待人工核实，尚未建单"
        return "人工客服将跟进"

    def _build_customer_message(self, state: TicketProcessState) -> str:
        """生成仅面向客户展示的安全话术，隐藏风险原因、工具结果和内部审核建议。"""
        decision_type = self._resolve_decision_type(state)
        if decision_type == "auto_reply":
            return state["answer"]

        if state["analysis"].user_goal == "human_request":
            return self._compose_human_request_answer(state)

        if decision_type == "review_required":
            ticket_text = self._format_customer_ticket_text(state.get("ticket_result"))
            if (
                is_delivery_not_received_message(state.get("message", ""))
                and "delivery_status_conflict" in set(state.get("risk_reasons", []))
            ):
                # 签收状态与客户反馈冲突时使用确定性异常话术，不能让模型继续重复“已签收”。
                if ticket_text:
                    return (
                        "我已核对到系统物流记录显示包裹已签收，但您反馈实际没有收到，两者存在不一致。"
                        "我已按物流签收异常为您登记核实，不会仅凭系统签收记录否定您的反馈。"
                        f"{ticket_text}您也可以先确认门卫、快递柜、代收点或家人是否代收；"
                        "后续结果以物流工作人员核实为准。"
                    )
                return (
                    "我已核对到系统物流记录显示包裹已签收，但您反馈实际没有收到，两者存在不一致。"
                    "该情况需要进入物流异常核实；当前工单登记暂未成功，请稍后重试或转人工客服处理。"
                )
            if (
                state["analysis"].user_goal == "action_request"
                and state.get("ticket_result")
                and state["ticket_result"].get("status") != "success"
            ):
                # Java 建单失败时不得使用“已受理”话术，保留槽位供客户稍后重试或人工接管。
                return (
                    "订单和申请信息已经核对，但售后工单暂时未能创建成功。"
                    "请稍后重试；如持续失败，可转人工客服继续处理。"
                )
            if (
                state["analysis"].user_goal == "action_request"
                and "order_validation_failed" in set(state.get("risk_reasons", []))
                and not ticket_text
            ):
                # 未通过订单归属校验时必须明确告知“尚未建单”，禁止让模型生成已提交、已登记等假成功话术。
                return (
                    "您的申请信息已经记录，但当前暂时无法验证该订单的归属，因此尚未创建售后工单。"
                    "请稍后重试；如持续失败，可转人工客服进一步核实。"
                )
            if state["analysis"].user_goal == "action_request" and state["analysis"].action_type == "return_goods" and ticket_text:
                # 退货申请已成功落到工单时，客户侧必须明确展示“退货申请已提交”，避免退化成泛化人工核实话术。
                slots = state["analysis"].action_slots or {}
                ticket_result = state.get("ticket_result") or {}
                if ticket_result.get("deduplicated"):
                    supplement_result = ticket_result.get("supplement_result")
                    if supplement_result and supplement_result.get("status") != "success":
                        return (
                            f"已找到您现有的退货工单。{ticket_text}"
                            "但本次补充信息暂未成功写入，请稍后重试或联系人工客服核实；原取件安排没有变更。"
                        )
                    supplement_data = (supplement_result or {}).get("data") or {}
                    update_mode = supplement_data.get("updateMode")
                    if update_mode == "UNCHANGED":
                        return (
                            f"已关联您现有的退货工单。{ticket_text}"
                            "本次信息与原工单一致，因此没有重复追加或新建工单。"
                        )
                    reason_text = (
                        f"已把退货原因“{slots.get('after_sale_reason')}”追加到原工单。"
                        if slots.get("after_sale_reason")
                        else "本次说明已追加到原工单。"
                    )
                    if update_mode == "REVIEW_REQUIRED":
                        return (
                            f"已关联您现有的退货工单。{ticket_text}{reason_text}"
                            "由于工单或取件已经进入处理阶段，新的取件方式/时间仅登记为变更申请，"
                            "不会直接覆盖原安排，请以工作人员或承运方后续确认为准。"
                        )
                    if supplement_result:
                        return (
                            f"已关联您现有的退货工单。{ticket_text}{reason_text}"
                            "本次提供的退回方式和取件时间偏好已更新到原工单，具体安排仍以后续确认为准。"
                        )
                    # 兼容未接入追加接口的旧测试图，仅说明复用工单，不声称新信息已经写入。
                    return f"已关联您现有的退货工单。{ticket_text}本次没有重复创建新工单。"
                if slots.get("return_method") == "pickup":
                    fulfillment_text = (
                        f"已登记上门取件偏好：{slots.get('pickup_time_window')}。"
                        "具体取件安排以工作人员或承运方后续确认为准。"
                    )
                else:
                    fulfillment_text = "已登记为自行寄回，后续请按工作人员提供的退回信息寄送。"
                reason_text = f"已记录退货原因：{slots.get('after_sale_reason')}。" if slots.get("after_sale_reason") else ""
                return (
                    f"已为您提交退货申请，售后工单已进入受理流程。{ticket_text}"
                    f"{reason_text}{fulfillment_text}工作人员会结合订单状态、退货原因和相关凭证继续核实处理。"
                )
            if state["analysis"].user_goal in {"complaint", "dispute"}:
                # 投诉、赔付争议属于高风险场景，使用受控模板而不让自由生成补充未被证据支持的处理承诺。
                citation_marker = self._primary_citation_marker(state.get("citations", []))
                parts = [
                    f"已收到您的投诉，相关诉求已转交人工客服进一步核实处理{citation_marker}。",
                    "后续将由专人跟进，请您稍候。",
                ]
                if ticket_text:
                    parts.append(ticket_text)
                return "\n\n".join(parts)
            if self._is_deduplicated_ticket(state.get("ticket_result")) and ticket_text:
                # 重复工单命中时仍交给 LLM 做自然表达，但事实只允许使用已有工单信息。
                llm_message = self._compose_llm_customer_answer(
                    state,
                    reply_mode="deduplicated_ticket",
                    service_instruction=(
                        "客户重复提交了同一类申请。请直接提醒已有在途工单，必须提到工单号和当前状态，"
                        "语气自然但简洁；不要说本次又创建了新工单，不要承诺处理结果。"
                    ),
                    ticket=self._customer_safe_ticket_payload(state.get("ticket_result")),
                    extra_context={"fallback_message": ticket_text},
                )
                if llm_message:
                    return llm_message
                return ticket_text
            llm_message = self._compose_llm_review_message(state, ticket_text)
            if llm_message:
                return llm_message
            order_context = self._format_order_context_text(state.get("tool_results", []))
            order_hint = "，并关联您的订单信息" if order_context else ""
            analysis = state["analysis"]
            if analysis.user_goal in {"action_request", "complaint", "dispute"}:
                parts = [
                    f"已收到您的反馈。该问题需要人工客服进一步核实处理{order_hint}。",
                    "我们会结合业务规则、订单或会话记录继续跟进，AI 不会直接承诺退款、赔付或处置结果。",
                ]
                if ticket_text:
                    parts.append(ticket_text)
                return "\n\n".join(parts)
            parts = [
                f"已收到您的反馈。我们已为您记录相关问题{order_hint}。",
                "当前问题已进入售后核实流程，客服会进一步核对商品问题描述、订单状态和商家处理记录。",
            ]
            if ticket_text:
                parts.append(ticket_text)
            return "\n\n".join(parts)

        # 模型降级或人工接管分支也必须保留高风险处置依据，不能返回无引用的固定话术。
        fallback_citation = self._primary_citation_marker(state.get("citations", []))
        if state["analysis"].user_goal in {"complaint", "dispute"}:
            return (
                f"已收到您的投诉，相关诉求已转交人工客服进一步处理{fallback_citation}。\n\n"
                "后续将由专人跟进，请您稍候。"
            )
        return (
            f"已收到您的反馈。该问题已为您转交人工客服处理{fallback_citation}。\n\n"
            "后续将由专人跟进，请您稍候。"
        )

    def reply_with_stream(self, request: AgentReplyRequest, on_delta, cancellation_token=None) -> AgentReply:
        """执行既有 Agent 图，并把最终生成节点的模型原生 token 回调给 Worker。"""
        self._stream_delta_callback = on_delta
        try:
            return self.reply(request, cancellation_token=cancellation_token)
        finally:
            # 无论模型、工具或图节点是否异常，都不能让后续请求复用旧连接回调。
            self._stream_delta_callback = None

    @staticmethod
    def _primary_citation_marker(citations: list[Citation]) -> str:
        """返回首条真实召回证据的客户可见标记；无证据时不伪造引用。"""
        if not citations:
            return ""
        ensure_citation_ids(citations)
        citation_id = citations[0].citation_id
        return f"【来源：{citation_id}】" if citation_id else ""

    def _rule_based_analyze(self, message: str) -> IntentResult:
        """使用规则兜底识别用户意图，保障无模型配置时 Demo 仍可运行。"""
        text = message.strip()
        order_no = re.findall(r"(?<![A-Za-z])(?:EC)?\d{10,18}", text, flags=re.IGNORECASE)
        intent = normalize_intent(text)
        user_goal = self._infer_user_goal(text)
        priority = "low"
        risk_reasons: list[str] = []

        if self._is_return_goods_policy_message(text):
            # 退货规则查询是低风险政策咨询，不进入真实退货动作或人工工单流程。
            return IntentResult(
                intent="refund",
                user_goal="policy_consult",
                emotion="normal",
                order_related=bool(order_no),
                order_no=order_no,
                product_name=None,
                need_order_query=False,
                need_ticket=False,
                need_human=False,
                priority="medium",
                confidence=0.9,
                summary=text[:120],
                risk_reasons=[],
                action_type="return_goods",
            )

        if user_goal == "how_to":
            # 操作步骤咨询只回答怎么做，不主动查询订单、物流或工单。
            return IntentResult(
                intent=intent if intent != "other" else "consult",
                user_goal="how_to",
                emotion="normal",
                order_related=False,
                order_no=[],
                product_name=None,
                need_order_query=False,
                need_ticket=False,
                need_human=False,
                priority="medium",
                confidence=0.88,
                summary=text[:120],
                risk_reasons=[],
            )

        if user_goal == "out_of_scope":
            # 非客服业务问题不需要转人工，直接给出边界说明或简短常识答复。
            risk_reasons = ["high_risk_out_of_scope"] if is_high_risk_out_of_scope_message(text) else ["out_of_scope"]
            return IntentResult(
                intent="other",
                user_goal="out_of_scope",
                emotion="normal",
                order_related=False,
                order_no=[],
                product_name=None,
                need_order_query=False,
                need_ticket=False,
                need_human=False,
                priority="low",
                confidence=0.86,
                summary=text[:120],
                risk_reasons=risk_reasons,
            )

        if self._contains_any(text, ["退款", "退钱", "取消订单"]):
            intent = "refund"
            priority = "high" if user_goal == "action_request" else "medium"
            if user_goal == "action_request":
                # 真实退款申请属于资金动作，必须进入人工审核；规则咨询不触发该风险。
                risk_reasons.append("refund_commitment")
        if self._contains_any(text, ["换货", "更换", "型号不对", "质量不好", "质量问题", "不给更换", "不给换"]):
            intent = "exchange"
            priority = "high" if self._is_after_sale_dispute_message(text) else "medium"
        if self._contains_any(text, ["维修", "报修", "修一下", "坏了", "故障", "无法使用", "连接不上", "报错"]):
            intent = "repair"
            priority = "medium"
        if self._contains_any(text, ["投诉", "举报", "差评", "欺骗", "维权"]):
            intent = "complaint"
            user_goal = "complaint"
            priority = "high"
            # 投诉类问题需要保留人工判断，避免 AI 直接给出处置承诺。
            risk_reasons.append("complaint")
        if self._contains_any(text, ["发票", "抬头", "税号", "开票"]):
            intent = "invoice"
        if self._contains_any(text, ["会员", "权益", "续费", "到期"]):
            intent = "member"

        emotion = "normal"
        if self._contains_any(text, ["急", "马上", "立刻"]):
            emotion = "anxious"
        if self._contains_any(text, ["不满", "太差", "垃圾", "不给换", "不给更换", "商家不给"]):
            emotion = "dissatisfied"
        if self._contains_any(text, ["投诉", "举报", "维权"]):
            emotion = "strong_complaint"

        if self._is_order_query_message(text) and intent == "other":
            intent = "logistics"
            user_goal = "status_query"
            priority = "medium"

        order_related = bool(order_no) or intent in {"logistics", "refund", "exchange", "repair"} or self._is_order_query_message(text)
        confidence = 0.82 if intent != "other" else 0.55
        # 风控按业务域和用户目的组合判断，规则咨询和只读查询不因业务域本身转人工。
        need_human = (
            confidence < 0.7
            or user_goal in {"human_request", "action_request", "complaint", "dispute"}
            or priority == "urgent"
            or emotion == "strong_complaint"
        )
        need_ticket = need_human or user_goal in {"human_request", "action_request", "complaint", "dispute"} or intent == "complaint"
        if self._is_after_sale_dispute_message(text):
            # 商品质量与商家拒绝处理属于售后争议，不能只自动返回订单信息。
            need_human = True
            need_ticket = True
            if "after_sale_dispute" not in risk_reasons:
                risk_reasons.append("after_sale_dispute")

        if confidence < 0.7:
            risk_reasons.append("low_confidence")

        return IntentResult(
            intent=intent,
            user_goal=user_goal,
            emotion=emotion,
            order_related=order_related,
            order_no=order_no,
            product_name=None,
            need_order_query=order_related,
            need_ticket=need_ticket,
            need_human=need_human,
            priority=priority,
            confidence=confidence,
            summary=text[:120],
            risk_reasons=risk_reasons,
        )

    def _compose_answer(self, state: TicketProcessState) -> str:
        """根据风险校验结果和知识库引用生成客服候选回复。"""
        analysis = state["analysis"]
        citations = state.get("citations", [])
        order_text = self._format_order_tool_text(state.get("tool_results", []))

        if self._is_user_identity_message(state["message"]):
            return self._compose_user_identity_answer(state)

        if self._is_session_memory_question(state["message"]):
            return self._compose_session_memory_answer(state)

        delivery_contingency_answer = self._compose_delivery_contingency_answer(state)
        if delivery_contingency_answer:
            return delivery_contingency_answer

        order_context_answer = self._compose_order_context_guardrail_answer(state)
        if order_context_answer:
            return order_context_answer

        other_identity_order_answer = self._compose_other_identity_order_answer(state)
        if other_identity_order_answer:
            return other_identity_order_answer

        # 前端已明确选中订单时，工具失败只能说明下游暂时不可用，绝不能误导客户再次提供订单号。
        if self._has_failed_selected_order_query(state):
            return order_text or "已关联您当前选中的订单，但暂时无法获取最新状态。请稍后重试，或联系人工客服继续核实。"

        if analysis.user_goal == "info_query" and self._is_agent_identity_message(state["message"]):
            return self._compose_agent_identity_answer(state)

        if analysis.user_goal == "out_of_scope":
            return self._compose_out_of_scope_answer(state)

        if analysis.user_goal == "human_request":
            return self._compose_human_request_answer(state)

        order_statistics_answer = self._compose_order_statistics_answer(state)
        if order_statistics_answer:
            # 件数和金额由确定性代码聚合，避免 LLM 算术误差或把真实查询改写成操作指引。
            return order_statistics_answer

        if analysis.user_goal == "policy_consult" and analysis.action_type == "return_goods" and not analysis.need_human:
            return self._compose_return_goods_policy_answer(state, citations)

        if analysis.user_goal == "policy_consult" and citations and not analysis.need_human:
            # 规则咨询先由 LLM 结合知识库和订单上下文生成自然回复；失败时再使用可控模板兜底。
            llm_answer = self._compose_llm_customer_answer(
                state,
                reply_mode="policy_consult",
                service_instruction="优先回答客户问到的规则、时效或条件，只使用知识库和订单事实，不扩展无关售后项目。",
            )
            if llm_answer:
                return llm_answer
            policy_text = self._policy_citation_text(analysis.intent, citations)
            order = self._first_success_order(state.get("tool_results", []))
            if order:
                return (
                    f"您当前咨询的是订单 {order.get('orderNo')}，商品 {order.get('productName') or '未记录'}。"
                    f"{policy_text} 这笔订单当前状态为 {self._order_status(order)}，"
                    f"售后状态为 {order.get('afterSaleStatus') or 'NONE'}；到账时间通常从退款审核通过后开始计算。"
                )
            return f"关于您咨询的问题：{policy_text or '当前知识库没有找到足够明确的规则说明，已转人工客服进一步确认。'}"

        if analysis.user_goal == "action_request" and analysis.next_action == "cancel_pending":
            # 用户主动取消上一轮动作时，只结束 pending，不再创建工单。
            return "好的，已取消本次业务申请。后续如果还需要退换货、维修或发票等服务，可以随时再告诉我。"

        if analysis.user_goal == "action_request" and analysis.next_action == "collect_slots":
            # 动作请求缺槽位时进入多轮补齐，不输出政策建议，也不提前建单。
            deterministic_slots = {
                "pending_confirmation",
                "action_confirmation",
                "order_confirmation",
                "order_no",
                "after_sale_reason",
                "return_method",
                "pickup_time_window",
                "fault_description",
                "invoice_title",
                "invoice_type",
                "tax_no",
            }
            if deterministic_slots.intersection(analysis.missing_slots or []):
                # 确认和关键业务槽位必须按真实缺失字段追问，不能让模型泛化成“还有其他信息吗”。
                return self._format_action_slot_question(state)
            llm_answer = self._compose_llm_customer_answer(
                state,
                reply_mode="collect_slots",
                service_instruction="客户正在发起业务动作，但信息未补齐。只追问缺失信息，不解释政策，不说已建单。",
                extra_context=self._slot_question_context(state),
            )
            if llm_answer:
                return llm_answer
            return self._format_action_slot_question(state)

        if analysis.user_goal == "how_to":
            # 操作步骤类问题不依赖业务工具，避免被选中订单或工单上下文劫持。
            return self._compose_how_to_answer(state)

        ticket_answer = self._format_ticket_progress_answer(state)
        if ticket_answer:
            llm_answer = self._compose_llm_customer_answer(
                state,
                reply_mode="ticket_urge" if self._first_ticket_tool_result(state, "ticket_urge") else "ticket_status",
                service_instruction=(
                    "客户在查询或催办工单进度。请基于工单事实自然说明当前状态、是否已记录催办、"
                    "催办次数和后续处理方式，不要承诺具体完成时间。"
                ),
                ticket=self._ticket_payload_from_tool_results(state),
            )
            if llm_answer:
                return llm_answer
            return ticket_answer

        order_answer = self._format_order_intent_answer(state, citations)

        if order_answer and not analysis.need_human:
            if is_order_detail_query_message(state["message"]):
                # 可信字段先确定性展示，再由模型补充自然解读；模型失败时不影响订单事实回复。
                supplement = self._compose_llm_customer_answer(
                    state,
                    reply_mode="product_overview",
                    service_instruction=(
                        "只生成两到四句商品补充说明，不要重复订单号、数量、金额、状态等已展示字段。"
                        "可以根据已核验的商品名称和分类，用‘从品类看’‘通常适合’等审慎表达说明常见用途；"
                        "不得编造芯片、速率、覆盖范围、材质、兼容协议等具体参数。"
                        "性价比只能结合当前成交价作条件式说明，不得声称全网最低或绝对值得购买。"
                        "输入没有真实评价摘要时，必须明确暂无可核验的用户评价数据，不得虚构评分、好评率或用户口碑。"
                    ),
                    extra_context={
                        "product_interpretation_scope": "仅允许常见用途和条件式性价比分析",
                        "review_summary": None,
                        "review_evidence_available": False,
                    },
                )
                if supplement:
                    return f"{order_answer}\n\n补充说明：{supplement}"
                return order_answer
            # 物流状态、位置与预计送达时间属于实时业务事实；直接使用工具格式化结果，禁止 LLM 把绝对日期误写成“今天/明天”。
            if (
                analysis.user_goal == "status_query"
                and (analysis.intent == "logistics" or self._is_logistics_message(state["message"]))
                and (self._first_logistics_result(state.get("tool_results", [])) or {}).get("status") == "success"
            ):
                return order_answer
            # 订单信息只是上下文，最终回答需要按用户真实意图区分物流、政策或普通查单。
            llm_answer = self._compose_llm_customer_answer(
                state,
                reply_mode="status_query" if analysis.user_goal == "status_query" else "info_query",
                service_instruction="结合订单或物流工具事实回答客户当前问题；如果是物流路线，保留关键节点，不编造未返回的轨迹。",
            )
            if llm_answer:
                return llm_answer
            return order_answer

        if analysis.need_human:
            # 高风险场景只生成人工处理提示，不直接承诺退款、赔付或投诉结果。
            llm_answer = self._compose_llm_customer_answer(
                state,
                reply_mode="review_required",
                service_instruction="这是需要人工核实的高风险或争议场景。自然回应客户诉求，说明会进入人工或工单流程，不承诺退款、赔付、处罚或审核结果。",
                ticket=self._customer_safe_ticket_payload(state.get("ticket_result")),
            )
            if llm_answer:
                return llm_answer
            ticket_text = self._format_ticket_text(state.get("ticket_result"))
            order_context = self._format_order_context_text(state.get("tool_results", []))
            if analysis.user_goal in {"action_request", "complaint", "dispute"}:
                return (
                    "已收到您的反馈。该问题需要人工客服结合订单、规则和处理记录进一步核实，"
                    f"{order_context}我们会通过工单继续跟进，不会由 AI 直接承诺退款、赔付或处置结果。{ticket_text}"
                )
            return (
                "已为您记录商品质量和售后处理问题，"
                "当前涉及换货争议，需要人工客服核实订单、商品问题描述和商家处理记录。"
                f"{order_context}客服会结合企业售后规则继续跟进。{ticket_text}"
            )
        if (
            analysis.need_order_query
            and not analysis.order_no
            and not state.get("selected_order_no")
            and not self._has_customer_order_attempt(state)
        ):
            # 物流、退款、售后类问题需要订单号才能查业务系统，先引导用户补充关键信息。
            llm_answer = self._compose_llm_customer_answer(
                state,
                reply_mode="collect_slots",
                service_instruction="客户的问题需要订单号才能查询。自然请客户提供订单号或选择订单，不要编造查询结果。",
                extra_context={"missing_slots": ["order_no"]},
            )
            if llm_answer:
                return llm_answer
            return "请您提供订单号，我会帮您查询订单物流或售后处理进度。"
        if citations:
            # 自动回复必须带有知识库依据，确保回答可追溯。
            llm_answer = self._compose_llm_customer_answer(
                state,
                reply_mode="auto_reply",
                service_instruction="基于知识库片段自然回答客户问题，不要输出内部依据字段，不要扩展无关业务。",
            )
            if llm_answer:
                return llm_answer
            citation_text = self._customer_visible_citation_text(citations[0])
            return f"关于您咨询的问题：{citation_text}" if citation_text else self._compose_no_evidence_answer(state)
        return self._compose_no_evidence_answer(state)

    def _compose_out_of_scope_answer(self, state: TicketProcessState) -> str:
        """回答非客服业务范围问题，普通常识可简答，高风险只给安全边界。"""
        message = state["message"]
        if is_high_risk_out_of_scope_message(message):
            return (
                "这个问题超出了我作为客户服务助手的能力范围，我不能提供医疗、法律、金融投资或危险操作方面的具体建议。"
                "建议您咨询对应领域的专业人士。订单、物流、售后、工单或发票问题，我可以继续帮您处理。"
            )

        # 普通低风险越界问题交给大模型简答，避免为个别问题维护僵硬的话术分支。
        llm_answer = self._compose_llm_customer_answer(
            state,
            reply_mode="out_of_scope",
            service_instruction=(
                "用户问的是客服业务范围之外的普通低风险问题。先直接、准确地简短回答用户问题，控制在一到两句；"
                "随后由你自然生成一句服务边界提醒，不要套用固定模板；可以换用“如果后面想查订单、物流或售后进度，我也可以继续帮您看”这类柔和说法。"
                "边界提醒必须覆盖订单、物流、售后、工单、发票中的至少两个业务范围，但不要自我介绍成企业客服助手，不要使用生硬的身份声明。"
                "不要声称查过知识库，不要转人工，不要创建工单，不要引用当前订单或历史会话中的业务对象。"
            ),
            extra_context={"allowed_scope": ["订单", "物流", "售后", "工单", "发票"]},
            use_business_context=False,
        )
        if llm_answer:
            cleaned_answer = self._ensure_out_of_scope_boundary(llm_answer)
            if self._has_natural_out_of_scope_boundary(cleaned_answer):
                return cleaned_answer
            dynamic_boundary = self._compose_llm_out_of_scope_boundary(state, cleaned_answer)
            if dynamic_boundary:
                return f"{cleaned_answer}\n\n{dynamic_boundary}" if cleaned_answer else dynamic_boundary
            fallback_boundary = self._fallback_out_of_scope_boundary()
            return f"{cleaned_answer}\n\n{fallback_boundary}" if cleaned_answer else fallback_boundary

        # 模型未配置或调用失败时保留安全兜底，但不伪装成已经回答了用户问题。
        return (
            "抱歉，我暂时无法可靠回答这个问题。"
            "我主要帮助处理订单、物流、售后、工单和发票问题，您可以直接告诉我相关诉求。"
        )

    @staticmethod
    def _ensure_out_of_scope_boundary(answer: str) -> str:
        """清理普通越界回复中的生硬身份声明；不再在这里追加固定模板。"""
        normalized = answer.strip()
        if not normalized:
            return ""

        # 模型可能把“常识答案 + 客服边界”写在同一段，先剥离突兀身份前缀，再判断是否可直接保留。
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = re.sub(
            r"我(?:是|作为)?(?:您的)?[^。！？!?；;，,]*(?:客服助手|客户服务助手|企业客服助手|自助客服助手)[，,]?",
            "",
            normalized,
        )
        if CustomerServiceAgent._has_natural_out_of_scope_boundary(normalized):
            return normalized.strip()

        boundary_terms = [
            "企业客服助手",
            "自助客服助手",
            "客户服务助手",
            "客服助手",
            "主要处理",
            "主要负责",
            "主要帮助处理",
            "订单",
            "物流",
            "售后",
            "工单",
            "发票",
            "继续提问",
            "继续问我",
        ]

        kept: list[str] = []
        sentences = re.findall(r"[^。！？!?；;]+[。！？!?；;]?", normalized)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            # 只丢弃边界句，保留前面的普通常识回答。
            if any(term in sentence for term in boundary_terms):
                continue
            kept.append(sentence)

        answer_body = "".join(kept).strip()
        return answer_body

    def _compose_llm_out_of_scope_boundary(self, state: TicketProcessState, answer_body: str) -> str:
        """让大模型单独生成服务边界句，避免低风险越界回复每次落到固定模板。"""
        if not getattr(self, "llm_analyzer", None):
            return ""
        analysis = state["analysis"]
        payload = {
            "message": state["message"],
            "intent": analysis.intent,
            "user_goal": analysis.user_goal,
            "summary": analysis.summary,
            "reply_mode": "out_of_scope_boundary",
            "citations": [],
            "order": None,
            "logistics": None,
            "ticket": None,
            "extra_context": {
                "answer_body": answer_body,
                "allowed_scope": ["订单", "物流", "售后", "工单", "发票"],
            },
            "service_instruction": (
                "不要回答原问题，不要重复前面的常识解释。只生成一句自然的服务范围提醒，语气轻一点，"
                "覆盖订单、物流、售后、工单、发票中的至少两个业务范围。"
                "不要使用“我是/作为/客服助手/主要负责/主要处理”这类身份声明，也不要承诺转人工或创建工单。"
            ),
        }
        try:
            boundary = self._generate_llm_reply(payload)
        except Exception as exc:
            self._log(
                "llm_out_of_scope_boundary",
                {"intent": analysis.intent, "user_goal": analysis.user_goal},
                {"status": "failed", "error_type": self._classify_model_error(exc), "error": str(exc)},
            )
            return ""
        boundary = self._ensure_out_of_scope_boundary(boundary)
        if boundary and self._has_natural_out_of_scope_boundary(boundary):
            return boundary
        return ""

    @staticmethod
    def _fallback_out_of_scope_boundary() -> str:
        """模型不可用时的最后兜底；正常在线链路优先使用 LLM 生成的边界句。"""
        return "如果后面想查订单进度、物流状态、售后处理、工单或发票信息，我也可以继续帮您看。"

    @staticmethod
    def _has_natural_out_of_scope_boundary(answer: str) -> bool:
        """判断模型是否已经生成自然的服务边界，避免每次都套固定收尾。"""
        scope_words = ["订单", "物流", "售后", "工单", "发票"]
        scope_hits = sum(1 for word in scope_words if word in answer)
        if scope_hits < 2:
            return False
        stiff_markers = ["我是", "作为", "客服助手", "客户服务助手", "企业客服助手", "自助客服助手", "主要处理", "主要负责", "主要帮助处理"]
        if any(marker in answer for marker in stiff_markers):
            return False
        friendly_markers = ["如果", "可以", "也能", "也可以", "继续", "帮您", "帮你", "这方面", "相关"]
        return any(marker in answer for marker in friendly_markers)

    def _compose_human_request_answer(self, state: TicketProcessState) -> str:
        """处理客户明确转人工请求，避免走知识库缺失模板。"""
        ticket_text = self._format_ticket_text(state.get("ticket_result"))
        if ticket_text:
            return f"已为您转接人工客服，并记录本次服务请求。{ticket_text}请您稍候，工作人员会继续跟进。"
        return "已为您转接人工客服，请您稍候，工作人员会继续跟进。"

    def _compose_no_evidence_answer(self, state: TicketProcessState) -> str:
        """低风险问题缺少知识库或工具依据时，不直接转人工，先做安全兜底或澄清。"""
        analysis = state["analysis"]
        if analysis.user_goal == "policy_consult":
            return "目前没有找到这条规则的明确说明。您可以换个说法补充一下具体业务场景，或告诉我需要转人工进一步确认。"
        if analysis.user_goal == "status_query":
            return "目前还无法确认最新状态。请您补充订单号或工单号，或稍后重试；如果情况比较紧急，也可以告诉我转人工核实。"
        return "目前没有找到足够明确的业务依据。您可以补充订单号、工单号或具体问题，我再继续帮您判断下一步。"

    def _compose_how_to_answer(self, state: TicketProcessState) -> str:
        """生成操作步骤类回复，不调用业务工具，也不引用当前订单或工单状态。"""
        analysis = state["analysis"]
        message = state["message"]
        if analysis.intent == "logistics" or self._is_logistics_message(message):
            return (
                "您可以这样查询物流状态：\n"
                "1. 在页面上方“我的订单”中选择对应订单；\n"
                "2. 点击订单卡片里的“查看详情”，或点击聊天区下方的“查看物流”；\n"
                "3. 系统会展示承运商、运单号、最新位置、预计送达时间和物流轨迹；\n"
                "4. 如果物流长时间未更新或显示异常，可以点击“联系客服”或“转人工客服”继续核实。"
            )
        if analysis.intent == "refund":
            return (
                "您可以这样申请退货或查看退货入口：\n"
                "1. 先在“我的订单”中选择需要处理的订单；\n"
                "2. 点击订单卡片里的“申请售后”，或点击聊天区下方的“申请退货退款”；\n"
                "3. 按页面提示补充退货原因、商品情况和必要凭证；\n"
                "4. 提交后等待售后审核，最终是否通过以审核结果为准。"
            )
        if analysis.intent == "invoice":
            return (
                "您可以这样处理发票问题：\n"
                "1. 选择对应订单后点击“发票问题”；\n"
                "2. 按页面提示填写发票类型、抬头和税号等信息；\n"
                "3. 如果订单或抬头信息无法确认，可以转人工客服继续核实。"
            )
        return "您可以先在页面中选择对应订单或工单，再点击相关快捷按钮；如果页面没有对应入口，可以直接描述您的问题，我会继续帮您判断下一步。"

    def _default_reply_context(self, state: TicketProcessState) -> dict[str, Any]:
        """整理给回复模型使用的非敏感上下文，让模型理解动作槽位和当前流程位置。"""
        analysis = state["analysis"]
        return {
            "action_type": analysis.action_type,
            "action_slots": analysis.action_slots,
            "missing_slots": analysis.missing_slots,
            "next_action": analysis.next_action,
            "pending_action_request": state.get("pending_action_request"),
            "conversation_context": self._safe_conversation_context(state),
            "recent_order_options": self._format_recent_order_options(state.get("tool_results", [])),
        }

    def _safe_citation_payloads(self, citations: list[Citation], limit: int = 3) -> list[dict[str, Any]]:
        """把知识库引用转换为客户可见事实，避免把检索说明、示例表达或内部话术送入 LLM。"""
        payloads: list[dict[str, Any]] = []
        for citation in citations:
            text = self._customer_visible_citation_text(citation)
            if not text:
                continue
            item = citation.model_dump()
            item["paragraph"] = text
            payloads.append(item)
            if len(payloads) >= limit:
                break
        return payloads

    @staticmethod
    def _customer_visible_citation_text(citation: Citation) -> str:
        """清洗单条 citation 正文，只保留能直接回答客户的规则事实。"""
        blocked_terms = [
            "适用范围",
            "适合回答",
            "典型表达",
            "本文档适用于",
            "本文档只说明",
            "检索提示",
            "禁止话术",
            "可用标准话术",
            "标准回复要点",
            "AI 应",
            "AI 不得",
            "不得直接承诺",
        ]
        text = f"{' '.join(citation.heading_path)} {citation.paragraph}"
        if any(term in text for term in blocked_terms):
            return ""
        sentences = [item.strip() for item in re.split(r"(?<=[。！？；;])\s*", citation.paragraph) if item.strip()]
        kept = [
            sentence
            for sentence in sentences
            if not any(term in sentence for term in blocked_terms)
            and not sentence.lstrip().startswith(("-", "“", "\""))
        ]
        return " ".join(kept).strip()

    def _compose_agent_identity_answer(self, state: TicketProcessState) -> str:
        """回答客户对智能客服身份和能力的询问，优先用 LLM 生成自然表达。"""
        fallback = (
            "您好，我是您的智能客服助手。可以帮您查询物流、咨询退换货和发票规则、查看或催办工单；"
            "如果问题需要人工核实，我也会帮您转接客服继续处理。"
        )
        llm_answer = self._compose_llm_customer_answer(
            state,
            reply_mode="info_query",
            service_instruction=(
                "客户在询问你是谁或你有什么能力。请用自然、有温度的中文介绍你是智能客服助手；"
                "只说明这些能力：咨询售后规则、查询订单物流、查看工单进度、催办工单、发票问题、必要时转人工。"
                "不要说知识库不足，不要创建工单，不要索要订单号，不要夸大为能直接退款或直接审核通过。"
            ),
            extra_context={
                "assistant_identity": "企业客户自助服务智能客服助手",
                "capabilities": [
                    "咨询售后规则",
                    "查询订单物流",
                    "查看工单进度",
                    "催办工单",
                    "解答发票问题",
                    "必要时转接人工客服",
                ],
                "fallback_message": fallback,
            },
        )
        return llm_answer or fallback

    def _compose_user_identity_answer(self, state: TicketProcessState) -> str:
        """回答“我是谁”类问题，登录态身份永远优先于会话自称。"""
        context = state.get("conversation_context") or {}
        login_user = context.get("login_user_context") or {}
        session_memory = context.get("session_memory") or {}
        display_name = str(login_user.get("display_name") or "").strip()
        role = str(login_user.get("role") or "customer").strip()
        role_text = "客户" if role == "customer" else role
        preferred_name = str(session_memory.get("preferred_name") or session_memory.get("self_claimed_name") or "").strip()
        if display_name and preferred_name and session_memory.get("identity_conflict"):
            return (
                f"当前登录账号显示为{display_name}，身份是{role_text}；"
                f"本次会话中您提到希望称呼为{preferred_name}。"
                "涉及订单、工单和身份校验时，以当前登录账号为准。"
            )
        if display_name:
            return f"当前登录账号显示为{display_name}，身份是{role_text}。涉及订单、工单和身份校验时，我会以当前登录账号为准。"
        if preferred_name:
            return f"本次会话中您提到希望称呼为{preferred_name}。不过涉及订单、工单和身份校验时，仍以当前登录账号为准。"
        return "您是当前已登录并正在咨询的客户。为了保护隐私，我不会展示更多账号信息；涉及订单、工单和身份校验时，会以当前登录账号为准。"

    def _compose_session_memory_answer(self, state: TicketProcessState) -> str:
        """回答“刚才问了什么/刚才聊了什么”类问题，只读取当前会话短期记忆。"""
        session_memory = ((state.get("conversation_context") or {}).get("session_memory") or {})
        recent_user_messages = session_memory.get("recent_user_messages") or []
        recent_ai_messages = session_memory.get("recent_ai_messages") or []
        last_question = session_memory.get("last_user_question")
        message = state["message"]
        if self._contains_any(message, ["问了什么", "上一句", "刚刚问", "刚才问", "前一句"]):
            if last_question:
                return f"您刚才问的是：“{last_question}”。"
            return "当前会话里我还没有看到更早的问题。您可以继续描述，我会从这条消息开始帮您记住上下文。"
        if not recent_user_messages and not recent_ai_messages:
            return "当前会话里暂时没有更早的聊天内容。您可以继续提问，我会基于本次会话继续衔接。"
        parts: list[str] = []
        for item in recent_user_messages[-3:]:
            content = item.get("content")
            if content:
                parts.append(f"您提到：“{content}”")
        if recent_ai_messages:
            answer = recent_ai_messages[-1].get("content")
            if answer:
                parts.append(f"我上一轮回复的大意是：“{answer}”")
        return "刚才我们主要聊了：" + "；".join(parts) + "。"

    def _compose_other_identity_order_answer(self, state: TicketProcessState) -> str:
        """阻止按会话自称或其他姓名查询订单，订单权限只能来自登录态 customer_id。"""
        message = state["message"]
        if not self._is_other_identity_order_request(message, state.get("conversation_context")):
            return ""
        return (
            "为了保护账号和订单隐私，我不能仅凭会话里提到的姓名查询他人订单。"
            "订单、工单和售后信息会以当前登录账号为准；如果需要查询其他人的订单，请切换到对应账号，或联系人工客服完成身份核验。"
        )

    def _compose_order_context_guardrail_answer(self, state: TicketProcessState) -> str:
        """在历史订单需要确认或已过期时，优先给出确定性追问，避免误查单或误建单。"""
        message = state["message"]
        if not has_fuzzy_order_reference(message):
            return ""
        if str(state.get("selected_order_no") or "").strip():
            # 本轮前端明确选择优先于历史模糊指代；订单归属仍由本轮 Java Tool 查询结果校验。
            return ""
        resolution = resolve_order_context(state.get("conversation_context"), message)
        if resolution.get("status") == "needs_confirmation" and resolution.get("order_no"):
            return f"请确认您说的是订单 {resolution['order_no']} 吗？确认后我再继续帮您查询或处理。"
        if resolution.get("status") in {"none", "expired"}:
            return "我还不能确定您指的是哪一笔订单。请先在上方选择订单，或直接发送订单号，我再继续帮您查询或处理。"
        return ""

    def _slot_question_context(self, state: TicketProcessState) -> dict[str, Any]:
        """为缺槽位追问提供聚焦上下文，避免模型展开无关政策说明。"""
        analysis = state["analysis"]
        slots = analysis.action_slots or {}
        order = self._first_success_order(state.get("tool_results", []))
        return {
            "action_type": analysis.action_type,
            "action_name": self._action_display_name(analysis.action_type),
            "known_slots": slots,
            "missing_slots": analysis.missing_slots,
            "linked_order_no": slots.get("order_no") or (order or {}).get("orderNo"),
            "conversation_context": self._safe_conversation_context(state),
            "recent_order_options": self._format_recent_order_options(state.get("tool_results", [])),
        }

    def _compose_return_goods_policy_answer(self, state: TicketProcessState, citations: list[Citation]) -> str:
        """生成稳定的退货规则咨询回复，避免 LLM 把维修、保修、质保内容混入退货政策。"""
        analysis = state["analysis"]
        related_citations = [citation for citation in citations if self._is_return_goods_citation(citation)]
        rewritten_queries = [
            str(citation.metadata.get("rewritten_query"))
            for citation in citations
            if citation.metadata.get("rewritten_query")
        ]
        recalled_chunks = [
            {
                "doc_name": citation.doc_name,
                "business_scope": citation.business_scope,
                "heading_path": citation.heading_path,
                "paragraph": citation.paragraph,
                "score": citation.score,
            }
            for citation in citations
        ]

        policy_text = self._return_goods_policy_text(related_citations)
        if not policy_text:
            answer = "当前知识库未找到明确退货规则。您可以在订单售后页面查看可申请的售后类型，最终是否支持退货以售后审核结果为准。"
            answer_source = "fallback_no_policy"
        else:
            # 规则咨询优先让 LLM 基于干净事实自然改写，失败时再回退到确定性模板。
            answer = self._compose_llm_return_goods_policy_answer(state, policy_text)
            answer_source = "llm" if answer else "template"
            if not answer:
                answer = self._return_goods_policy_fallback_answer(policy_text)

        answer = self._sanitize_return_goods_answer(answer)
        self._log(
            "return_goods_policy_trace",
            {
                "message": state["message"],
                "intent": analysis.intent,
                "user_goal": analysis.user_goal,
                "business_scope": "return_goods",
                "rewritten_query": rewritten_queries,
            },
            {"citations": recalled_chunks, "final_answer": answer, "answer_source": answer_source},
        )
        return answer

    def _compose_llm_return_goods_policy_answer(self, state: TicketProcessState, policy_text: str) -> str:
        """让模型只基于清洗后的退货规则事实生成自然客服话术，失败时交给模板兜底。"""
        if not self.llm_analyzer or not policy_text:
            return ""

        analysis = state["analysis"]
        clean_citation = Citation(
            doc_name="return_goods_policy",
            version="cleaned",
            paragraph=policy_text,
            score=1.0,
            collection="return_policy",
            business_scope="return_goods",
            heading_path=["退货规则", "客户可见规则"],
            risk_level="medium",
            answerable_intents=["refund", "consult"],
            retrieval_source="sanitized",
            metadata={"category": "return_policy", "sanitized": True},
        )
        payload = {
            "message": state["message"],
            "intent": analysis.intent,
            "user_goal": analysis.user_goal,
            "summary": analysis.summary,
            "reply_mode": "policy_consult",
            # 只把清洗后的客户可见事实交给 LLM，避免复述知识库管理说明。
            "citations": [clean_citation.model_dump()],
            # 纯规则咨询不能引用当前选中订单或历史订单，避免把“查规则”误答成“判断订单能否退”。
            "order": None,
            "logistics": None,
            "ticket": None,
            "extra_context": {
                "return_goods_policy_facts": policy_text,
                "conversation_context": self._safe_conversation_context(state),
            },
            "service_instruction": (
                "用户只是咨询退货规则，请把给定退货规则事实整理成自然、简洁的客服话术。"
                "只能回答退货条件、退货时效、退货流程、退款周期或审核边界；"
                "不要复述文档标题、适用范围、典型表达、检索提示、禁止话术或示例问题；"
                "不要主动引用当前订单、历史订单或选中的订单；"
                "不要索要订单号、不要建议转人工、不要说需要先核实订单后才能回答；"
                "不得承诺一定可退，必须说明最终是否通过以售后审核结果为准。"
                "如果给定事实不足，请回答“当前知识库未找到明确退货规则”。"
            ),
        }
        try:
            answer = self._generate_llm_reply(payload)
            if not self._is_safe_customer_reply(answer):
                return ""
            answer = self._sanitize_return_goods_answer(answer)
            if not self._is_return_goods_policy_answer_usable(answer):
                return ""
            self._log(
                "llm_return_goods_policy_reply",
                {
                    "intent": analysis.intent,
                    "user_goal": analysis.user_goal,
                    "business_scope": "return_goods",
                },
                {"status": "success", "final_answer": answer},
            )
            return answer
        except Exception as exc:
            # LLM 表达优化失败不能影响规则咨询主流程，后续会使用模板兜底。
            self._log(
                "llm_return_goods_policy_reply",
                {
                    "intent": analysis.intent,
                    "user_goal": analysis.user_goal,
                    "business_scope": "return_goods",
                },
                {"status": "failed", "error_type": self._classify_model_error(exc), "error": str(exc)},
            )
            return ""

    @staticmethod
    def _is_return_goods_citation(citation: Citation) -> bool:
        """判断召回片段是否真正属于退货规则，过滤退款到账、维修、保修等相邻政策。"""
        text = f"{citation.doc_name} {' '.join(citation.heading_path)} {citation.paragraph}"
        metadata = citation.metadata or {}
        if CustomerServiceAgent._is_policy_metadata_text(text):
            return False
        if citation.business_scope == "return_goods" or metadata.get("category") == "return_policy":
            return True
        return any(term in text for term in ["退货", "七天无理由", "二次销售", "签收后"]) and not any(
            term in text for term in ["保修", "维修", "质保", "报修", "故障维修"]
        )

    @staticmethod
    def _return_goods_policy_text(citations: list[Citation]) -> str:
        """从退货规则召回片段中提取客户可读正文，避免输出适用范围、示例表达等文档元信息。"""
        for citation in citations:
            text = CustomerServiceAgent._clean_return_goods_policy_text(citation.paragraph)
            if text:
                return text
        return ""

    @staticmethod
    def _clean_return_goods_policy_text(paragraph: str) -> str:
        """清理退货规则片段，只保留规则条件、流程和审核边界。"""
        blocked_terms = [
            "适用范围",
            "适合回答",
            "典型表达",
            "本文档适用于",
            "本文档只说明",
            "检索提示",
            "禁止话术",
            "可用标准话术",
            "标准回复要点",
        ]
        if any(term in paragraph for term in blocked_terms):
            return ""
        sentences = [item.strip() for item in re.split(r"(?<=[。！？；;])\s*", paragraph) if item.strip()]
        kept = [
            sentence
            for sentence in sentences
            if not any(term in sentence for term in blocked_terms)
            and not sentence.lstrip().startswith(("-", "“", "\""))
        ]
        text = " ".join(kept).strip()
        if len(text) > 260:
            text = text[:260].rstrip("，,；; ") + "。"
        return text

    @staticmethod
    def _return_goods_policy_fallback_answer(policy_text: str) -> str:
        """生成退货规则兜底话术，避免把内部写作要求或订单核实话术展示给客户。"""
        if CustomerServiceAgent._is_return_goods_policy_text_specific(policy_text):
            return (
                f"根据退货规则，{policy_text} "
                "如果商品仍在规则允许范围内，您可以在订单售后页面提交退货申请，最终是否通过以售后审核结果为准。"
            )
        return (
            "根据退货规则，商品通常需要在平台规定的售后时效内提交退货申请，并保持商品完好、"
            "不影响二次销售；部分商品或特殊场景可能不支持退货。您可以在订单售后页面提交申请，"
            "最终是否通过以售后审核结果为准。"
        )

    @staticmethod
    def _is_return_goods_policy_text_specific(policy_text: str) -> bool:
        """判断退货规则片段是否包含可直接回答客户的具体规则事实。"""
        if not policy_text or not policy_text.strip():
            return False
        weak_terms = [
            "AI 应",
            "不得直接承诺",
            "无法直接承诺",
            "需要结合具体商品规则",
            "需要结合商品规则",
            "订单状态来判断",
            "系统无法确认",
            "建议联系人工",
        ]
        if any(term in policy_text for term in weak_terms):
            return False
        concrete_terms = ["7 天", "七天", "签收", "二次销售", "商品完好", "售后页面", "提交退货申请", "售后时效"]
        return any(term in policy_text for term in concrete_terms)

    @staticmethod
    def _is_policy_metadata_text(text: str) -> bool:
        """识别知识库管理说明类片段，防止直接作为客户回复内容。"""
        metadata_terms = [
            "适用范围",
            "适合回答",
            "典型表达",
            "本文档适用于",
            "检索提示",
            "禁止话术",
            "可用标准话术",
            "标准回复要点",
        ]
        return any(term in text for term in metadata_terms)

    @staticmethod
    def _sanitize_return_goods_answer(answer: str) -> str:
        """退货规则回答后校验：删除混入维修、保修、质保等无关业务的句子。"""
        forbidden_terms = ["保修期", "维修", "质保", "报修", "故障维修"]
        if not any(term in answer for term in forbidden_terms):
            return answer.strip()
        sentences = [item.strip() for item in re.split(r"(?<=[。！？；;])\s*", answer) if item.strip()]
        kept = [sentence for sentence in sentences if not any(term in sentence for term in forbidden_terms)]
        return " ".join(kept).strip() or "根据退货规则，商品通常需要在规定时效内提交退货申请，并保持商品完好、不影响二次销售；最终是否通过以售后审核结果为准。"

    @staticmethod
    def _is_return_goods_policy_answer_usable(answer: str) -> bool:
        """校验模型生成的退货规则话术是否适合直接展示给客户。"""
        if not answer or not answer.strip():
            return False
        # 调用方已经确认存在可用 policy_text；模型若仍声称知识库无规则，属于与检索证据冲突的生成结果。
        no_policy_phrases = [
            "当前知识库未找到明确退货规则",
            "知识库未找到退货规则",
            "没有找到明确的退货规则",
            "未检索到退货规则",
        ]
        if any(phrase in answer for phrase in no_policy_phrases):
            return False
        metadata_terms = [
            "适用范围",
            "适合回答",
            "典型表达",
            "本文档适用于",
            "检索提示",
            "禁止话术",
            "可用标准话术",
            "标准回复要点",
            "我想退货",
            "这个商品能退吗",
            "提供订单号",
            "订单号",
            "联系人工",
            "人工客服",
            "进一步核实后操作",
            "进一步核实后",
            "系统中暂未找到具体的时效",
            "暂未找到具体的时效",
            "暂未找到具体时效",
            "具体商品规则和订单状态",
            "结合商品规则和订单状态",
            "结合具体商品规则",
            "无法直接承诺一定可以退货",
        ]
        return not any(term in answer for term in metadata_terms)

    def _format_action_slot_question(self, state: TicketProcessState) -> str:
        """根据缺失槽位生成客户侧追问，引导用户补齐业务动作所需信息。"""
        analysis = state["analysis"]
        slots = analysis.action_slots or {}
        missing = set(analysis.missing_slots or [])
        order = self._first_success_order(state.get("tool_results", []))
        order_no = slots.get("order_no") or (order or {}).get("orderNo")
        action_name = self._action_display_name(analysis.action_type)
        order_options = self._format_recent_order_options(state.get("tool_results", []))

        if "pending_confirmation" in missing:
            pending = state.get("pending_action_request") or {}
            pending_order_no = (pending.get("action_slots") or {}).get("order_no")
            order_hint = f"订单 {pending_order_no} 的" if pending_order_no else "之前的"
            return f"您之前正在办理{order_hint}{action_name}申请，已间隔一段时间。请确认是否继续该申请？"
        if "action_confirmation" in missing:
            pending = state.get("pending_action_request") or {}
            if pending.get("confirmation_reason") == "ambiguous_unwanted_item":
                order_no = (pending.get("action_slots") or {}).get("order_no")
                order_hint = f"订单 {order_no}" if order_no else "一笔订单"
                return f"请确认您是想为{order_hint}发起退货申请吗？确认后我会继续核对尚缺的信息。"
            return f"之前的{action_name}流程已超时，我不会继续关联原订单。请确认是否重新发起一笔{action_name}申请？"
        if "order_confirmation" in missing:
            pending = state.get("pending_action_request") or {}
            summary = pending.get("candidate_order_summary") or {}
            candidate_order_no = (pending.get("action_slots") or {}).get("candidate_order_no") or summary.get("order_no")
            product_name = summary.get("product_name")
            product_hint = f"（{product_name}）" if product_name else ""
            return f"请确认您要办理的是订单 {candidate_order_no}{product_hint} 吗？回复“是”后我会继续收集{action_name}所需信息。"
        if analysis.action_type == "return_goods" and missing.intersection(
            {"order_no", "after_sale_reason", "return_method", "pickup_time_window"}
        ):
            # 退货流程根据当前真实缺口组合问题，允许客户一轮补充多个槽位，不再固定逐项询问。
            questions: list[str] = []
            if "order_no" in missing:
                questions.append("请选择要退货的订单，或直接回复订单号")
            if "after_sale_reason" in missing:
                questions.append("说明退货原因")
            if "return_method" in missing:
                questions.append("选择上门取件或自行寄回；如需上门取件，也可以同时告诉我方便的时间")
            elif "pickup_time_window" in missing:
                questions.append("告诉我方便上门取件的时间段")
            suffix = f"\n\n您也可以从这些订单中选择：\n{order_options}" if "order_no" in missing and order_options else ""
            prefix = f"已关联订单 {order_no}，" if order_no and "order_no" not in missing else ""
            return prefix + "；".join(questions) + "。" + suffix
        if "order_no" in missing and "after_sale_reason" in missing:
            suffix = f"\n\n您也可以从这些订单中选择：\n{order_options}" if order_options else ""
            return f"请问您要处理哪一笔订单？可以选择订单或直接回复订单号，并说明{action_name}原因。{suffix}"
        if "order_no" in missing:
            suffix = f"\n\n您也可以从这些订单中选择：\n{order_options}" if order_options else ""
            return f"请问您要处理哪一笔订单？可以选择订单或直接回复订单号。{suffix}"
        if "after_sale_reason" in missing:
            prefix = f"已关联订单 {order_no}，" if order_no else ""
            return f"{prefix}请补充{action_name}原因，例如商品质量问题、拍错、不想要、配件缺失等。"
        if "return_method" in missing:
            reason = slots.get("after_sale_reason")
            reason_hint = f"已记录您的退货原因：{reason}。" if reason else ""
            return f"{reason_hint}您希望选择上门取件，还是自行寄回商品？"
        if "pickup_time_window" in missing:
            return (
                "好的，已选择上门取件。请告诉我方便取件的时间段，例如明天下午、工作日 18:00 后或周六上午。"
                "该时间会作为取件偏好登记，最终安排以工作人员或承运方确认为准。"
            )
        if "fault_description" in missing:
            prefix = f"已关联订单 {order_no}，" if order_no else ""
            return f"{prefix}请描述需要维修的具体故障现象，例如无法联网、无法开机、报错或配件异常。"
        if "invoice_title" in missing:
            return "请补充发票抬头；如果是企业发票，也请一并提供税号。"
        if "invoice_type" in missing:
            return "请说明要开个人发票还是企业发票。"
        if "tax_no" in missing:
            return "企业发票需要补充纳税人识别号，请您提供税号。"
        if "description" in missing:
            return "请再补充一下具体诉求或问题描述，我会继续为您记录处理。"
        return "还需要您补充一点信息后才能继续处理，请说明具体订单和诉求。"

    def _compose_llm_customer_answer(
        self,
        state: TicketProcessState,
        *,
        reply_mode: str = "auto_reply",
        service_instruction: str = "直接回答客户问题；有政策或业务工具依据时结合依据说明。",
        ticket: dict[str, Any] | None = None,
        extra_context: dict[str, Any] | None = None,
        use_business_context: bool = True,
    ) -> str:
        """调用大模型生成客户可读回复，严格限定只能使用知识库和工具返回的可信事实。"""
        if not getattr(self, "llm_analyzer", None):
            return ""

        analysis = state["analysis"]
        if analysis.user_goal == "policy_consult" and analysis.action_type == "return_goods":
            # 退货规则回复的 Prompt 额外收窄到退货条件、时效、流程和退款周期，禁止主动扩展到维修/保修/质保。
            service_instruction = (
                f"{service_instruction} 用户当前咨询的是退货规则。请只回答退货条件、退货时效、退货流程、退款周期等内容；"
                "这是纯规则咨询，不要引用当前订单、历史订单或选中的订单；只有用户明确问具体订单能否退时，才使用订单上下文。"
                "禁止回答维修、保修、质保、报修相关内容，除非用户问题中明确提到这些内容。"
                "如果知识库没有相关规则，请回答“当前知识库未找到明确退货规则”。"
            )
        payload = {
            "message": state["message"],
            "intent": analysis.intent,
            "user_goal": analysis.user_goal,
            "summary": analysis.summary,
            "reply_mode": reply_mode,
            "citations": self._safe_citation_payloads(state.get("citations", []), limit=3) if use_business_context else [],
            "order": self._first_success_order(state.get("tool_results", [])) if use_business_context else None,
            "logistics": self._first_logistics_data(state.get("tool_results", [])) if use_business_context else None,
            "ticket": ticket if use_business_context else None,
            "extra_context": extra_context or self._default_reply_context(state),
            "service_instruction": service_instruction,
        }
        try:
            answer = self._generate_llm_reply(payload)
            if not self._is_safe_customer_reply(answer):
                return ""
            if analysis.user_goal == "policy_consult" and analysis.action_type == "return_goods":
                # 退货规则咨询的模型回复必须做业务域后校验，避免混入维修、保修、质保内容。
                answer = self._sanitize_return_goods_answer(answer)
            self._log(
                "llm_customer_reply",
                {"intent": analysis.intent, "user_goal": analysis.user_goal, "reply_mode": reply_mode},
                {
                    "status": "success",
                    "rewritten_query": [
                        citation.metadata.get("rewritten_query")
                        for citation in state.get("citations", [])
                        if citation.metadata.get("rewritten_query")
                    ],
                    "final_answer": answer,
                },
            )
            return answer
        except Exception as exc:
            # 回复生成失败不能影响主流程，保留日志后回退到模板化安全话术。
            self._log(
                "llm_customer_reply",
                {"intent": analysis.intent, "user_goal": analysis.user_goal},
                {"status": "failed", "error_type": self._classify_model_error(exc), "error": str(exc)},
            )
            return ""

    def _compose_llm_review_message(self, state: TicketProcessState, ticket_text: str) -> str:
        """让大模型在安全边界内生成建单或人工跟进话术，避免客户侧回复过度模板化。"""
        if not self.llm_analyzer:
            return ""

        analysis = state["analysis"]
        payload = {
            "message": state["message"],
            "intent": analysis.intent,
            "user_goal": analysis.user_goal,
            "summary": analysis.summary,
            "reply_mode": "review_required",
            "citations": self._safe_citation_payloads(state.get("citations", []), limit=2),
            "order": self._first_success_order(state.get("tool_results", [])),
            "logistics": self._first_logistics_data(state.get("tool_results", [])),
            "ticket": self._customer_safe_ticket_payload(state.get("ticket_result")),
            "extra_context": self._default_reply_context(state),
            "service_instruction": (
                "这是需要人工或受控流程继续处理的场景。请自然回应客户诉求，"
                "结合订单和工单事实说明已进入受理/待分派/已分派状态；"
                "不要承诺退款、赔付、处罚、通过审核或具体完成时间。"
            ),
        }
        try:
            answer = self._generate_llm_reply(payload)
            if self._is_safe_customer_reply(answer):
                self._log(
                    "llm_review_customer_reply",
                    {"intent": analysis.intent, "user_goal": analysis.user_goal},
                    {"status": "success"},
                )
                return answer
        except Exception as exc:
            # 建单已经完成时，LLM 只影响表达，不影响主业务闭环。
            self._log(
                "llm_review_customer_reply",
                {"intent": analysis.intent, "user_goal": analysis.user_goal},
                {"status": "failed", "error_type": self._classify_model_error(exc), "error": str(exc)},
            )
        return ""

    def _generate_llm_reply(self, payload: dict) -> str:
        """兼容旧 LLM 测试替身；仅真实流式执行时向生成器传入 token 回调。"""
        try:
            if getattr(self, "_stream_delta_callback", None) is None:
                return self.llm_analyzer.generate_customer_reply(payload, cancellation_token=getattr(self, "_cancellation_token", None))
            return self.llm_analyzer.generate_customer_reply(payload, on_delta=self._stream_delta_callback, cancellation_token=getattr(self, "_cancellation_token", None))
        except TypeError:
            # 旧版测试替身与扩展点尚未声明取消参数时保持既有调用协议。
            if getattr(self, "_stream_delta_callback", None) is None:
                return self.llm_analyzer.generate_customer_reply(payload)
            return self.llm_analyzer.generate_customer_reply(payload, on_delta=self._stream_delta_callback)

    @staticmethod
    def _is_safe_customer_reply(answer: str) -> bool:
        """过滤明显泄露内部字段或调试信息的模型回复，失败时回退安全模板。"""
        if not answer or not answer.strip():
            return False
        internal_markers = ["risk_reasons", "tool_results", "internal_suggestion", "decision_type", "```json", "{", "}"]
        return not any(marker in answer for marker in internal_markers)

    def _has_customer_order_result(self, state: TicketProcessState) -> bool:
        """判断是否已经通过客户 ID 查询过订单列表，避免继续要求用户补充订单号。"""
        return any(
            item.get("query_type") == "customer_orders" and item.get("status") in {"success", "empty"}
            for item in state.get("tool_results", [])
        )

    def _has_customer_order_attempt(self, state: TicketProcessState) -> bool:
        """判断是否已经尝试按客户查询订单，即使失败也不再要求用户重复提供订单号。"""
        return any(item.get("query_type") == "customer_orders" for item in state.get("tool_results", []))

    def _compose_order_statistics_answer(self, state: TicketProcessState) -> str:
        """基于客户订单工具结果确定性统计商品件数与实付金额。"""
        message = state.get("message", "")
        if not is_order_statistics_message(message):
            return ""

        result = next(
            (item for item in state.get("tool_results", []) if item.get("query_type") == "customer_orders"),
            None,
        )
        if not result:
            return "暂时没有取得您的订单数据，无法准确完成统计。请稍后重试或联系人工客服。"
        if result.get("status") == "failed":
            return "订单服务暂时不可用，当前无法准确统计购买件数和实付金额。请稍后重试或联系人工客服。"
        if result.get("status") == "empty":
            range_label, _, _ = self._resolve_order_statistics_range(message)
            return f"我已查询您的账户，{range_label}没有找到订单记录。"

        range_label, start_time, end_time = self._resolve_order_statistics_range(message)
        paid_orders: list[tuple[dict[str, Any], datetime, Decimal, int]] = []
        for order in result.get("data") or []:
            raw_pay_time = order.get("payTime")
            if not raw_pay_time:
                # 没有支付时间的订单尚未形成真实消费，不纳入实付统计。
                continue
            pay_time = self._parse_order_datetime(raw_pay_time)
            if pay_time is None:
                return "查询到的订单中存在无法识别的支付时间，当前无法给出准确统计结果。请稍后重试。"
            if (start_time and pay_time < start_time) or pay_time > end_time:
                continue
            try:
                amount = Decimal(str(order.get("amount")))
            except (InvalidOperation, TypeError, ValueError):
                return "查询到的已支付订单金额不完整，当前无法准确计算总花费。请稍后重试。"
            if not amount.is_finite() or amount < 0:
                return "查询到的已支付订单金额异常，当前无法准确计算总花费。请稍后重试。"
            try:
                quantity = int(order.get("quantity"))
            except (TypeError, ValueError):
                return "查询到的订单商品数量不完整，当前无法准确计算购买件数。请稍后重试。"
            if quantity < 1:
                return "查询到的订单商品数量异常，当前无法准确计算购买件数。请稍后重试。"
            paid_orders.append((order, pay_time, amount, quantity))

        if not paid_orders:
            return f"我已查询您的账户，{range_label}没有已支付订单，因此暂无可统计的购买件数和实付金额。"

        paid_orders.sort(key=lambda item: item[1], reverse=True)
        total_amount = sum((item[2] for item in paid_orders), Decimal("0.00"))
        total_quantity = sum(item[3] for item in paid_orders)
        product_groups: dict[str, dict[str, Any]] = {}
        for order, pay_time, amount, quantity in paid_orders:
            product_name = str(order.get("productName") or "商品信息暂不可用")
            group = product_groups.setdefault(
                product_name,
                {"quantity": 0, "amount": Decimal("0.00"), "latest_pay_time": pay_time},
            )
            group["quantity"] += quantity
            group["amount"] += amount
            if pay_time > group["latest_pay_time"]:
                group["latest_pay_time"] = pay_time

        sorted_products = sorted(product_groups.items(), key=lambda item: item[1]["latest_pay_time"], reverse=True)
        lines = [
            f"{range_label}，您共有 {len(paid_orders)} 笔已支付订单，购买 {total_quantity} 件商品，订单实付合计 ¥{total_amount:.2f}。",
            "购买明细：",
        ]
        for product_name, group in sorted_products[:10]:
            lines.append(f"- {product_name} × {group['quantity']}，小计 ¥{group['amount']:.2f}")
        if len(sorted_products) > 10:
            lines.append(f"- 另有 {len(sorted_products) - 10} 种商品未逐项展开，已计入上述总件数和总金额。")
        lines.append("以上按订单支付时间和订单实付金额统计，未支付订单不计入；退款金额因当前订单数据未提供实退明细，暂未另行扣减。")
        return "\n".join(lines)

    @staticmethod
    def _resolve_order_statistics_range(
        message: str,
        now: datetime | None = None,
    ) -> tuple[str, datetime | None, datetime]:
        """解析客户指定的统计周期；未明确时按近九十天处理。"""
        current = now or datetime.now()
        if any(word in message for word in ["全部历史", "所有历史", "历史全部", "全部订单", "所有订单"]):
            return "全部历史范围内", None, current

        day_match = re.search(r"(?:近|最近|过去)\s*(\d{1,5})\s*天", message)
        if day_match:
            days = max(1, int(day_match.group(1)))
            return f"近 {days} 天内", current - timedelta(days=days), current
        if "本月" in message or "这个月" in message:
            return "本月内", current.replace(day=1, hour=0, minute=0, second=0, microsecond=0), current
        if "今年" in message or "本年度" in message:
            return "今年内", current.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0), current
        return "近 90 天内", current - timedelta(days=90), current

    @staticmethod
    def _parse_order_datetime(value: Any) -> datetime | None:
        """把业务接口的 ISO 时间统一为可比较的本地无时区时间。"""
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed

    def _format_order_tool_text(self, tool_results: list[dict[str, Any]]) -> str:
        """把订单工具结果转换为客服可读回复，避免用户只能看到右侧调试 JSON。"""
        for item in tool_results:
            if item.get("query_type") == "order_logistics":
                if item.get("status") == "failed":
                    return "已收到物流查询请求，但物流服务暂时不可用。请稍后重试，或由人工客服继续帮您核实。"
                if item.get("status") == "empty":
                    return f"已查询到订单 {item.get('order_no')}，但当前暂无物流轨迹，可能是尚未发货或物流信息还在同步中。"

            if item.get("query_type") == "customer_orders":
                if item.get("status") == "failed":
                    return f"已收到查询请求，但客户订单接口暂时调用失败：{item.get('error')}。请稍后重试或转人工处理。"
                if item.get("status") == "empty":
                    return "我已查询您的账号，当前没有找到关联订单。"
                if item.get("status") == "success":
                    orders = item.get("data") or []
                    lines = ["我已查询到您的订单："]
                    for order in orders[:5]:
                        lines.append(
                            f"- 订单 {order.get('orderNo')}，商品 {order.get('productName') or '未记录'}，"
                            f"状态 {self._order_status(order)}，金额 {order.get('amount')}，"
                            f"售后状态 {order.get('afterSaleStatus') or 'NONE'}"
                        )
                    return "\n".join(lines)

            if item.get("query_type") == "order_detail":
                if item.get("status") == "failed":
                    return (
                        f"已关联您当前选中的订单 {item.get('order_no')}，"
                        "但订单服务暂时无法获取最新信息。请稍后重试，或联系人工客服继续核实。"
                    )
                if item.get("status") == "empty":
                    return f"没有查询到订单 {item.get('order_no')}，请核对订单号是否正确。"
                if item.get("status") == "success":
                    order = item.get("data") or {}
                    return (
                        f"已查询到订单 {order.get('orderNo')}：商品 {order.get('productName') or '未记录'}，"
                        f"当前状态 {self._order_status(order)}，金额 {order.get('amount')}，"
                        f"售后状态 {order.get('afterSaleStatus') or 'NONE'}。"
                    )
        return ""

    @staticmethod
    def _has_failed_selected_order_query(state: TicketProcessState) -> bool:
        """判断当前前端已选订单是否在查询阶段失败，防止失败时退化为索要订单号。"""
        selected_order_no = str(state.get("selected_order_no") or "").strip()
        if not selected_order_no:
            return False
        return any(
            item.get("query_type") in {"order_detail", "order_logistics"}
            and str(item.get("order_no") or "") == selected_order_no
            and item.get("status") == "failed"
            for item in state.get("tool_results", [])
        )

    def _format_ticket_progress_answer(self, state: TicketProcessState) -> str:
        """根据工单查询或催办工具结果生成客户侧兜底回复。"""
        result = self._first_ticket_tool_result(state, "ticket_urge") or self._first_ticket_tool_result(state, "ticket_status")
        if not result:
            return ""
        if result.get("status") == "empty":
            return "我暂时没有找到可查询或催办的在途工单，请您核对工单号，或从历史记录中选择对应工单后再试。"
        if result.get("status") != "success":
            return "暂时无法同步该工单的最新进度，请稍后重试，或联系人工客服继续核实。"

        ticket = result.get("data") or {}
        ticket_no = ticket.get("ticketNo") or result.get("ticket_no") or "该工单"
        status = ticket.get("status") or "未知"
        urge_count = ticket.get("urgeCount") or 0
        last_urged_at = self._format_time(ticket.get("lastUrgedAt"))
        if result.get("query_type") == "ticket_urge":
            return (
                f"已帮您催办工单 {ticket_no}，当前状态为 {status}。"
                f"这是第 {urge_count} 次催办，最近催办时间为 {last_urged_at}。"
                "工作人员会在原工单中继续跟进处理。"
            )
        return (
            f"工单 {ticket_no} 当前状态为 {status}。"
            f"累计催办 {urge_count} 次，最近催办时间：{last_urged_at}。"
            "如果需要加急，可以回复“帮我催一下”并带上工单号。"
        )

    @staticmethod
    def _first_ticket_tool_result(state: TicketProcessState, query_type: str | None = None) -> dict[str, Any] | None:
        """提取第一条工单查询或催办结果，供回复生成使用。"""
        for item in state.get("tool_results", []):
            if item.get("query_type") in {"ticket_status", "ticket_urge"}:
                if query_type is None or item.get("query_type") == query_type:
                    return item
        return None

    def _ticket_payload_from_tool_results(self, state: TicketProcessState) -> dict[str, Any] | None:
        """把工单工具结果压缩成客户侧安全事实，交给 LLM 生成自然回复。"""
        result = self._first_ticket_tool_result(state)
        if not result or result.get("status") != "success":
            return None
        ticket = result.get("data") or {}
        return {
            "ticketNo": ticket.get("ticketNo"),
            "status": ticket.get("status"),
            "orderNo": ticket.get("orderNo"),
            "urgeCount": ticket.get("urgeCount") or 0,
            "lastUrgedAt": ticket.get("lastUrgedAt"),
            "lastUrgeReason": ticket.get("lastUrgeReason"),
            "queryType": result.get("query_type"),
        }

    def _format_order_intent_answer(self, state: TicketProcessState, citations: list[Citation]) -> str:
        """根据用户意图组织订单回复，避免所有订单相关问题都退化成订单状态复述。"""
        analysis = state["analysis"]
        message = state["message"]
        order = self._first_success_order(state.get("tool_results", []))
        if not order:
            return self._format_order_tool_text(state.get("tool_results", []))

        order_summary = self._format_order_summary(order)
        if is_order_detail_query_message(message):
            # 订单商品介绍只展示 Java 已核验的业务字段，不能凭商品名编造功能、参数或营销卖点。
            return self._format_selected_order_product_answer(order)
        if self._is_logistics_message(message) or analysis.intent == "logistics":
            logistics_answer = self._format_logistics_answer(state, order)
            if logistics_answer:
                return logistics_answer
            logistics_tip = self._first_citation_text(citations, "Logistics FAQ")
            if not logistics_tip:
                logistics_tip = "订单发货后可通过订单号在订单详情页查询物流状态。"
            return (
                f"{logistics_tip}\n\n"
                f"我已帮您查询当前订单：{order_summary}"
            )

        if analysis.user_goal == "policy_consult" or self._is_after_sale_policy_message(message) or analysis.intent in {"consult", "refund", "exchange"}:
            policy_text = self._policy_citation_text(analysis.intent, citations)
            if not policy_text:
                policy_text = "签收后 7 天内且商品不影响二次销售时，可申请退货；审核通过后通常 1-7 个工作日原路退回，具体以支付渠道为准。"
            return (
                f"关于您咨询的规则：{policy_text}\n\n"
                f"结合当前订单信息：{order_summary}"
            )

        return self._format_order_tool_text(state.get("tool_results", []))

    def _format_selected_order_product_answer(self, order: dict[str, Any]) -> str:
        """基于订单详情中的可信字段介绍商品，信息不足时明确边界而不是返回无依据兜底。"""
        lines = [
            f"您当前选中的是订单 {order.get('orderNo')}，其中的商品是 {order.get('productName') or '商品名称暂未记录'}。",
        ]
        details: list[str] = []
        if order.get("productCategory"):
            details.append(f"分类：{order.get('productCategory')}")
        if order.get("quantity") is not None:
            details.append(f"数量：{order.get('quantity')} 件")
        if order.get("amount") is not None:
            details.append(f"订单实付：¥{order.get('amount')}")
        if order.get("warrantyDays") is not None:
            details.append(f"质保期：{order.get('warrantyDays')} 天")
        if order.get("returnable") is not None:
            details.append(f"支持退货：{'是' if order.get('returnable') else '否'}（最终以售后规则和审核结果为准）")
        if details:
            lines.append("；".join(details) + "。")
        lines.append(f"当前订单状态：{self._order_status(order)}，售后状态：{order.get('afterSaleStatus') or 'NONE'}。")
        lines.append("以上是业务系统中已核验的商品与订单信息；更详细的功能参数需要以商品详情页为准。")
        return "\n".join(lines)

    def _compose_delivery_contingency_answer(self, state: TicketProcessState) -> str:
        """处理“若未送达则售后”的条件性诉求，避免把未来假设误当作立即退货申请。"""
        message = str(state.get("message") or "")
        if not self._is_delivery_contingency_message(message):
            return ""

        # 前端实时选中订单优先，其次仅展示本轮工具已核验的订单，不能从长期历史猜测订单。
        order_no = str(state.get("selected_order_no") or "").strip()
        order = self._first_success_order(state.get("tool_results", [])) or {}
        order_no = order_no or str(order.get("orderNo") or "").strip()
        if not order_no:
            return (
                "我理解您的诉求：如果到约定时间仍未收到商品，希望继续办理售后。"
                "目前还没有关联具体订单，请先在“我的订单”中选择对应订单或提供订单号。"
                "届时如仍未收到，直接告诉我“仍未收到”即可；系统会先核查最新物流和签收记录，"
                "再按核查结果处理物流异常或退货申请，不能提前承诺退货一定成功。"
            )

        product_name = str(order.get("productName") or "").strip()
        order_label = f"订单 {order_no}" + (f"（{product_name}）" if product_name else "")
        return (
            f"已记录您对{order_label}的处理诉求：若到约定时间仍未收到，希望办理售后。"
            "现在不宜提前提交退货申请，以免包裹仍在配送或物流状态尚未同步造成误处理。\n\n"
            "届时请保持该订单选中，并回复“仍未收到”或点击“查看物流”。我会先核查最新物流和签收记录："
            "仍在运输中会继续跟进配送；若系统显示已签收但您未收到，会登记物流异常并转人工核实；"
            "退货申请将以订单核验和售后审核结果为准。"
        )

    @staticmethod
    def _is_delivery_contingency_message(message: str) -> bool:
        """识别“未来未送达时再售后”的条件表达，不把普通退货或物流查询误拦截。"""
        text = message.strip()
        # “收不到”与“未收到”语义相同，但前者不一定命中通用未收货识别器，需在此覆盖自然表达。
        has_delivery_absence = is_delivery_not_received_message(text) or any(
            word in text
            for word in ["收不到", "没收到", "未收到", "还没收到", "包裹没到", "快递没到", "货没到"]
        )
        if not text or not has_delivery_absence:
            return False
        has_after_sale_request = any(word in text for word in ["退货", "退款", "售后", "处理"]) or bool(
            # 单独的“退了/退吧”只有处在动作语境时才视为售后请求，避免把普通“退回”误判。
            re.search(r"(?:帮我|给我|就|要|想|申请).{0,4}退(?:了|吧)?", text)
        )
        has_future_condition = any(
            word in text
            for word in ["如果", "要是", "若", "到时候", "明天", "后天", "届时", "再"]
        )
        return has_after_sale_request and has_future_condition

    def _format_logistics_answer(self, state: TicketProcessState, order: dict[str, Any]) -> str:
        """把物流工具结果整理成客户可读回复，包含路线、转运站和轨迹节点。"""
        logistics_result = self._first_logistics_result(state.get("tool_results", []))
        if not logistics_result:
            return ""
        if logistics_result.get("status") == "failed":
            return "暂时无法获取物流信息，我不会编造物流轨迹。请您稍后重试，或转人工客服继续核实。"
        if logistics_result.get("status") == "empty":
            return (
                f"我已查询到订单 {logistics_result.get('order_no')}，但当前暂无物流轨迹。"
                f"结合订单状态 {self._order_status(order)}，可能是尚未发货或物流节点还在同步中。"
            )

        logistics = logistics_result.get("data") or {}
        traces = logistics.get("traces") or []
        show_all = self._wants_full_logistics(state["message"])
        selected_traces = traces if show_all else list(reversed(traces[-3:]))
        trace_title = "完整物流轨迹" if show_all else "最近物流轨迹"

        lines = [
            "我已帮您查询到物流信息：",
            f"订单号：{logistics.get('orderNo') or order.get('orderNo')}",
            f"商品：{order.get('productName') or '未记录'}",
            f"订单状态：{self._order_status(order)}",
            f"承运商：{logistics.get('carrierName') or '未记录'}",
            f"运单号：{logistics.get('trackingNo') or '未记录'}",
            f"物流状态：{self._logistics_status(logistics.get('logisticsStatus'))}",
            f"最新位置：{logistics.get('latestLocation') or '暂无'}",
            f"预计送达时间：{self._format_estimated_delivery_time(logistics.get('estimatedDeliveryTime'))}",
        ]
        route_summary = logistics.get("routeSummary")
        if route_summary:
            lines.append(f"经过路线：{route_summary}")
        if selected_traces:
            lines.append(f"\n{trace_title}：")
            for trace in selected_traces:
                occurred_at = self._format_time(trace.get("occurredAt"))
                station = trace.get("stationName") or trace.get("location") or "未知站点"
                desc = trace.get("description") or self._logistics_status(trace.get("status"))
                lines.append(f"- {occurred_at}｜{station}｜{desc}")
        return "\n".join(lines)

    def _format_order_context_text(self, tool_results: list[dict[str, Any]]) -> str:
        """为人工处理场景补充简短订单上下文，避免订单信息喧宾夺主。"""
        for item in tool_results:
            if item.get("query_type") == "customer_orders" and item.get("status") == "success":
                orders = item.get("data") or []
                if orders:
                    order = orders[0]
                    return f"已关联订单 {order.get('orderNo')}，当前订单状态 {self._order_status(order)}。"
            if item.get("query_type") == "order_detail" and item.get("status") == "success":
                order = item.get("data") or {}
                return f"已关联订单 {order.get('orderNo')}，当前订单状态 {self._order_status(order)}。"
        return ""

    def _format_recent_order_options(self, tool_results: list[dict[str, Any]]) -> str:
        """把客户最近订单压缩成可选择列表，供动作槽位追问时展示。"""
        for item in tool_results:
            if item.get("query_type") == "customer_orders" and item.get("status") == "success":
                orders = item.get("data") or []
                lines = []
                for order in orders[:3]:
                    lines.append(
                        f"- {order.get('orderNo')} / {order.get('productName') or '商品信息待补充'} / "
                        f"状态 {self._order_status(order)}"
                    )
                return "\n".join(lines)
        return ""

    @staticmethod
    def _action_display_name(action_type: str | None) -> str:
        """将 action_type 转成客户可理解的动作名称。"""
        mapping = {
            "return_goods": "退货",
            "refund_request": "退款",
            "exchange_goods": "换货",
            "repair_request": "维修",
            "invoice_issue": "开票",
            "cancel_order": "取消订单",
            "complaint_submit": "投诉",
        }
        return mapping.get(str(action_type), "处理")

    @staticmethod
    def _order_status(order: dict[str, Any]) -> str:
        """兼容 Java 订单字段命名，优先读取当前 DTO 的 orderStatus。"""
        return str(order.get("orderStatus") or order.get("status") or "未知")

    def _first_success_order(self, tool_results: list[dict[str, Any]]) -> dict[str, Any] | None:
        """从工具结果中提取第一条成功订单，供意图化回复使用。"""
        for item in tool_results:
            if item.get("query_type") == "order_detail" and item.get("status") == "success":
                return item.get("data") or {}
            if item.get("query_type") == "customer_orders" and item.get("status") == "success":
                orders = item.get("data") or []
                if orders:
                    return orders[0]
        return None

    @staticmethod
    def _first_logistics_result(tool_results: list[dict[str, Any]]) -> dict[str, Any] | None:
        """提取第一条物流工具结果，供物流回复优先使用真实轨迹。"""
        for item in tool_results:
            if item.get("query_type") == "order_logistics":
                return item
        return None

    @staticmethod
    def _first_logistics_data(tool_results: list[dict[str, Any]]) -> dict[str, Any] | None:
        """提取成功物流数据，供 LLM 回复生成时使用真实轨迹而不是猜测。"""
        for item in tool_results:
            if item.get("query_type") == "order_logistics" and item.get("status") == "success":
                return item.get("data") or {}
        return None

    def _format_order_summary(self, order: dict[str, Any]) -> str:
        """格式化订单摘要，复用在物流、政策和普通查单回复中。"""
        return (
            f"订单 {order.get('orderNo')}，商品 {order.get('productName') or '未记录'}，"
            f"当前状态 {self._order_status(order)}，金额 {order.get('amount')}，"
            f"售后状态 {order.get('afterSaleStatus') or 'NONE'}。"
        )

    def _wants_full_logistics(self, message: str) -> bool:
        """识别客户是否明确要求完整路线、转运站或全流程轨迹。"""
        return self._contains_any(message, ["全流程", "完整", "经过", "路线", "转运", "转运站", "哪些地方", "到过哪里"])

    @staticmethod
    def _format_time(value: Any) -> str:
        """格式化 Java 返回的 ISO 时间，避免客户侧看到空值或过长时间串。"""
        if not value:
            return "暂无"
        text = str(value)
        return text.replace("T", " ")[:16]

    @classmethod
    def _format_estimated_delivery_time(cls, value: Any) -> str:
        """保留预计送达绝对时间；如果预计时间已过，明确说明其仅为历史预估。"""
        formatted = cls._format_time(value)
        if not value:
            return formatted
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            # 过期的预计时间不能再被描述为“今天送达”，应提醒客户以最新节点为准。
            if parsed < datetime.now():
                return f"{formatted}（该预计时间已过，请以最新物流节点为准）"
        except (TypeError, ValueError):
            # 格式异常时保留原始可见时间，避免因展示层异常丢弃可用业务信息。
            pass
        return formatted

    @staticmethod
    def _logistics_status(status: Any) -> str:
        """将物流状态枚举转换为客户可读中文，未知状态保留原值便于追踪。"""
        mapping = {
            "PENDING_SHIPMENT": "待发货",
            "SHIPPED": "已发货",
            "IN_TRANSIT": "运输中",
            "OUT_FOR_DELIVERY": "派送中",
            "SIGNED": "已签收",
            "EXCEPTION": "物流异常",
        }
        return mapping.get(str(status), str(status or "未知"))

    @staticmethod
    def _first_citation_text(citations: list[Citation], doc_name: str) -> str:
        """按文档名取知识库片段，避免物流问题误用退货政策。"""
        for citation in citations:
            if citation.doc_name == doc_name:
                text = CustomerServiceAgent._customer_visible_citation_text(citation)
                if text:
                    return text
        for citation in citations:
            text = CustomerServiceAgent._customer_visible_citation_text(citation)
            if text:
                return text
        return ""

    def _policy_citation_text(self, intent: str, citations: list[Citation]) -> str:
        """按业务域选择最贴合的政策片段，避免退款问题混入退货、维修等无关内容。"""
        preferred_docs = {
            "refund": ["Refund Arrival Policy", "Return Exchange Policy"],
            "exchange": ["Return Exchange Policy"],
            "repair": ["Repair Policy"],
            "invoice": ["Invoice FAQ"],
            "member": ["Member FAQ"],
            "logistics": ["Logistics FAQ"],
        }
        for doc_name in preferred_docs.get(intent, []):
            for citation in citations:
                if citation.doc_name == doc_name:
                    text = self._customer_visible_citation_text(citation)
                    if text:
                        return text
        for citation in citations:
            text = self._customer_visible_citation_text(citation)
            if text:
                return text
        return ""

    def _is_return_goods_order_status_message(self, message: str) -> bool:
        """识别用户是否在问具体订单能否退货，只有此类问题才需要查订单。"""
        order_reference = bool(re.search(r"(?<![A-Za-z])(?:EC)?\d{10,18}", message, flags=re.IGNORECASE)) or self._contains_any(
            message,
            ["我的订单", "这个订单", "这笔订单", "该订单", "当前订单", "订单号"],
        )
        return order_reference and self._contains_any(
            message,
            ["能退吗", "能不能退", "可以退吗", "可不可以退", "是否能退", "支持退货吗", "能退货吗"],
        )

    def _is_return_goods_policy_message(self, message: str) -> bool:
        """识别退货规则咨询，和“我要退货”等真实操作申请区分开。"""
        if self._is_action_request_message(message):
            return False
        if self._is_return_goods_order_status_message(message):
            return False
        return self._contains_any(
            message,
            [
                "查看退货规则",
                "退货规则是什么",
                "退货规则",
                "退货条件",
                "退货政策",
                "七天无理由",
                "商品能不能退",
                "能不能退货",
                "可以退货吗",
                "怎么申请退货",
                "如何申请退货",
                "退货流程",
            ],
        )

    def _is_after_sale_policy_message(self, message: str) -> bool:
        """识别退货、换货、售后规则类低风险政策咨询。"""
        return self._contains_any(
            message,
            ["退货规则", "退货政策", "退款规则", "退款政策", "多久到账", "多久退", "退款流程", "怎么退款", "退货", "换货规则", "换货政策", "售后规则", "售后政策"],
        )

    def _format_customer_ticket_text(self, ticket_result: dict[str, Any] | None) -> str:
        """将工单接口结果整理为客户可追踪的信息，只展示工单号和当前状态。"""
        if not ticket_result or ticket_result.get("status") != "success":
            return ""

        data = ticket_result.get("data") or {}
        ticket_no = data.get("ticketNo")
        status = data.get("status")
        if not ticket_no:
            return ""
        if ticket_result.get("deduplicated"):
            if status == "PENDING_ASSIGN":
                return f"您之前已提交过相关申请，工单号为 {ticket_no}，目前正在等待工作人员分派处理，请勿重复提交。"
            if status in {"PENDING_PROCESS", "PROCESSING"}:
                return f"您之前已提交过相关申请，工单号为 {ticket_no}，目前已分派给客服继续处理，请勿重复提交。"
            return f"您之前已提交过相关申请，工单号为 {ticket_no}，当前状态：{status or '待分派'}，请勿重复提交。"
        if status == "PENDING_ASSIGN":
            return f"您的问题已提交成功，工单号为 {ticket_no}，目前已进入受理流程，后续将由工作人员分派处理。"
        if status in {"PENDING_PROCESS", "PROCESSING"}:
            return f"您的问题已提交成功，工单号为 {ticket_no}，已分派给客服处理。"
        return f"您的问题已提交成功，工单号为 {ticket_no}，当前状态：{status or '待分派'}。"

    @staticmethod
    def _customer_safe_ticket_payload(ticket_result: dict[str, Any] | None) -> dict[str, Any] | None:
        """提取客户侧可使用的工单事实，避免把内部派单原因或风险字段交给回复模型。"""
        if not ticket_result or ticket_result.get("status") != "success":
            return None
        data = ticket_result.get("data") or {}
        return {
            "ticketNo": data.get("ticketNo"),
            "status": data.get("status"),
            "orderNo": data.get("orderNo"),
            "deduplicated": bool(ticket_result.get("deduplicated")),
        }

    @staticmethod
    def _is_deduplicated_ticket(ticket_result: dict[str, Any] | None) -> bool:
        """判断本次是否复用了已有未完成工单，用于客户侧展示专用提醒。"""
        return bool(ticket_result and ticket_result.get("status") == "success" and ticket_result.get("deduplicated"))

    def _format_ticket_text(self, ticket_result: dict[str, Any] | None) -> str:
        """把 Java 工单接口返回结果转换为用户可读提示，不暴露内部异常细节。"""
        if not ticket_result:
            return ""
        if ticket_result.get("status") != "success":
            return "工单创建暂未成功，客服会继续人工跟进。"

        data = ticket_result.get("data") or {}
        ticket_no = data.get("ticketNo")
        status = data.get("status")
        if not ticket_no:
            return "工单已提交。"
        if ticket_result.get("deduplicated"):
            return f"您已提交过相关申请，工单 {ticket_no} 当前状态：{status}。"
        if status == "PENDING_ASSIGN":
            return f"已创建工单 {ticket_no}，目前待工作人员分派处理。"
        if status in {"PENDING_PROCESS", "PROCESSING"}:
            return f"已创建工单 {ticket_no}，已分派给客服处理。"
        return f"已创建工单 {ticket_no}，当前状态：{status}。"

    def _log(self, tool_name: str, input_data: dict[str, Any], output_data: dict[str, Any]) -> None:
        """记录工具调用输入和输出，优先落库，内存列表仅保留兼容和排错用途。"""
        try:
            saved_log = self.log_repository.save(tool_name, input_data, output_data)
        except Exception as exc:
            # 日志系统异常不能阻断客服流程，失败时保留内存日志供临时排查。
            saved_log = {
                "tool_name": "agent_log_persist",
                "input_data": {"origin_tool_name": tool_name},
                "output_data": {"status": "failed", "error": str(exc)},
                "status": "failed",
                "error_message": str(exc),
                "created_at": datetime.utcnow().isoformat(),
            }
        self.call_logs.append(saved_log)

    def list_call_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        """从数据库读取最近 Agent 调用日志，服务重启后仍可追溯历史调用。"""
        return self.log_repository.list_recent(limit)

    @staticmethod
    def _contains_any(text: str, words: list[str]) -> bool:
        """判断文本是否命中任一业务关键词，用于规则兜底识别。"""
        return any(word in text for word in words)

    def _create_llm_analyzer(self) -> LLMIntentAnalyzer | None:
        """按环境变量创建真实 LLM 识别器，依赖或配置缺失时保持规则兜底可用。"""
        if not LLMIntentAnalyzer.is_configured():
            return None
        try:
            return LLMIntentAnalyzer()
        except Exception as exc:
            self.llm_init_error = str(exc)
            self._log(
                "init_llm_intent_analyzer",
                {},
                {
                    "status": "failed",
                    "error_type": self._classify_model_error(exc),
                    "error": str(exc),
                },
            )
            return None

    def _analyze_with_llm_fallback(self, payload: Any) -> IntentResult:
        """优先调用真实 LLM 做结构化识别，模型失败时降级为转人工结果。"""
        message = payload.get("message") if isinstance(payload, dict) else str(payload)
        conversation_context = payload.get("conversation_context") if isinstance(payload, dict) else None
        if self._is_return_goods_policy_message(message):
            # 退货规则是规则可确定识别的低风险意图，无需在 RAG 前再阻塞一次意图 LLM。
            return self._apply_context_guardrails(
                message,
                self._apply_business_guardrails(message, self._rule_based_analyze(message)),
                conversation_context,
            )
        llm_message = self._message_for_intent_analysis(message, conversation_context)
        if not self.llm_analyzer:
            if LLMIntentAnalyzer.is_configured():
                # 模型初始化失败时，低风险查单问题允许走规则兜底；高风险问题仍然转人工。
                return self._fallback_or_model_failure(message, "model_init_failed", self.llm_init_error, conversation_context)
            return self._apply_context_guardrails(
                message,
                self._apply_business_guardrails(message, self._rule_based_analyze(message)),
                conversation_context,
            )

        try:
            result = self.llm_analyzer.invoke(llm_message)
            self._log(
                "llm_intent_analyze",
                {"message": message, "used_context": bool(llm_message != message)},
                {
                    "status": "success",
                    "intent": result.intent,
                    "confidence": result.confidence,
                },
            )
            return self._apply_context_guardrails(message, self._apply_business_guardrails(message, result), conversation_context)
        except Exception as exc:
            error_type = self._classify_model_error(exc)
            # 模型失败需要保留日志；低风险查单可规则兜底，高风险场景仍转人工。
            self._log(
                "llm_intent_analyze",
                {"message": message},
                {"status": "failed", "error_type": error_type, "error": str(exc)},
            )
            return self._fallback_or_model_failure(message, error_type, str(exc), conversation_context)

    def _apply_business_guardrails(self, message: str, result: IntentResult) -> IntentResult:
        """对 LLM 输出补充强制业务规则，防止高风险场景被错误自动回复。"""
        normalized = result.model_copy(deep=True)
        normalized.user_goal = self._infer_user_goal(message, normalized.user_goal)
        extracted_order_no = re.findall(r"(?<![A-Za-z])(?:EC)?\d{10,18}", message, flags=re.IGNORECASE)
        for order_no in extracted_order_no:
            if order_no not in normalized.order_no:
                normalized.order_no.append(order_no)

        if self._is_agent_identity_message(message) or self._is_user_identity_message(message) or self._is_session_memory_question(message):
            # 身份和会话记忆问题是基础信息咨询，不继承订单、工单或售后动作上下文。
            normalized.intent = "consult"
            normalized.user_goal = "info_query"
            normalized.order_related = False
            normalized.order_no = []
            normalized.action_type = None
            normalized.action_slots = {}
            normalized.missing_slots = []
            normalized.next_action = None
            normalized.need_order_query = False
            normalized.need_human = False
            normalized.need_ticket = False
            normalized.priority = "low"
            normalized.confidence = max(normalized.confidence, 0.92)
            normalized.risk_reasons = [
                reason
                for reason in normalized.risk_reasons
                if reason not in {"low_confidence", "action_or_dispute_requires_human", "refund_commitment"}
            ]
            return normalized

        if is_human_request_message(message):
            # 用户明确要求真人客服时，尊重请求并直接进入人工流程，不走知识库缺失兜底。
            normalized.intent = "consult" if normalized.intent in {"other", "consult"} else normalized.intent
            normalized.user_goal = "human_request"
            normalized.order_related = False
            normalized.order_no = []
            normalized.action_type = None
            normalized.action_slots = {}
            normalized.missing_slots = []
            normalized.next_action = "transfer_human"
            normalized.need_order_query = False
            normalized.need_human = True
            normalized.need_ticket = False
            normalized.priority = "medium" if normalized.priority == "low" else normalized.priority
            normalized.confidence = max(normalized.confidence, 0.9)
            normalized.risk_reasons = [reason for reason in normalized.risk_reasons if reason != "low_confidence"]
            if "human_request" not in normalized.risk_reasons:
                normalized.risk_reasons.append("human_request")
            return normalized

        if normalized.user_goal in {"complaint", "dispute"} or normalize_user_goal(message, normalized.user_goal) in {"complaint", "dispute"}:
            # 投诉和争议属于客服受理范围，不能被越界兜底覆盖成 out_of_scope。
            normalized.intent = "complaint" if normalized.user_goal == "complaint" or "投诉" in message or "举报" in message else normalized.intent
            normalized.user_goal = normalize_user_goal(message, normalized.user_goal)
            normalized.need_order_query = bool(normalized.order_no)
            normalized.need_human = True
            normalized.need_ticket = True
            normalized.priority = "high"
            normalized.confidence = max(normalized.confidence, 0.86)
            if "complaint" not in normalized.risk_reasons:
                normalized.risk_reasons.append("complaint")
            return normalized

        if is_out_of_scope_message(message):
            # 普通越界问题不需要人工客服；高风险越界只做安全边界提示。
            normalized.intent = "other"
            normalized.user_goal = "out_of_scope"
            normalized.order_related = False
            normalized.order_no = []
            normalized.action_type = None
            normalized.action_slots = {}
            normalized.missing_slots = []
            normalized.next_action = None
            normalized.need_order_query = False
            normalized.need_human = False
            normalized.need_ticket = False
            normalized.priority = "low"
            normalized.confidence = max(normalized.confidence, 0.86)
            normalized.risk_reasons = ["high_risk_out_of_scope"] if is_high_risk_out_of_scope_message(message) else ["out_of_scope"]
            return normalized

        if is_order_statistics_message(message):
            # 订单统计属于低风险只读查询，即使模型误判为 how_to 或置信度偏低也必须调用真实订单工具。
            normalized.intent = "consult"
            normalized.user_goal = "info_query"
            normalized.order_related = True
            normalized.order_no = []
            normalized.action_type = None
            normalized.action_slots = {}
            normalized.missing_slots = []
            normalized.next_action = None
            normalized.need_order_query = True
            normalized.need_human = False
            normalized.need_ticket = False
            normalized.priority = "medium"
            normalized.confidence = max(normalized.confidence, 0.92)
            normalized.risk_reasons = [
                reason
                for reason in normalized.risk_reasons
                if reason not in {"low_confidence", "action_or_dispute_requires_human", "refund_commitment"}
            ]
            return normalized

        inferred_intent = normalize_intent(message, normalized.intent)
        if normalized.intent in {"other", "consult"} and inferred_intent != normalized.intent:
            normalized.intent = inferred_intent

        if normalized.user_goal == "how_to":
            # 操作步骤咨询是低风险说明，不绑定当前订单或工单，也不进入动作槽位。
            normalized.order_related = False
            normalized.order_no = []
            normalized.action_type = None
            normalized.action_slots = {}
            normalized.missing_slots = []
            normalized.next_action = None
            normalized.need_order_query = False
            normalized.need_human = False
            normalized.need_ticket = False
            normalized.priority = "medium"
            normalized.confidence = max(normalized.confidence, 0.86)
            normalized.risk_reasons = [
                reason
                for reason in normalized.risk_reasons
                if reason not in {"low_confidence", "action_or_dispute_requires_human", "refund_commitment"}
            ]
            return normalized

        if (self._is_logistics_message(message) or self._is_order_query_message(message)) and (
            normalized.intent in {"other", "consult", "logistics"} or normalized.user_goal in {"policy_consult", "status_query"}
        ):
            # 物流状态类问题即使包含“怎么/如何”，也应优先按只读查询处理，并复用客户侧选中订单上下文。
            normalized.intent = "logistics"
            normalized.user_goal = "status_query"
            normalized.order_related = True
            normalized.need_order_query = True
            normalized.need_human = False
            normalized.need_ticket = False
            normalized.priority = "medium" if normalized.priority == "low" else normalized.priority
            normalized.confidence = max(normalized.confidence, 0.78)
            if "low_confidence" in normalized.risk_reasons:
                normalized.risk_reasons.remove("low_confidence")

        if self._is_ticket_progress_message(message) or self._is_ticket_urge_message(message):
            # 工单进度查询和催办走专用 Tool，不创建新的售后工单。
            normalized.intent = "consult" if normalized.intent == "other" else normalized.intent
            normalized.user_goal = "status_query"
            normalized.order_related = False
            normalized.need_order_query = False
            normalized.need_human = False
            normalized.need_ticket = False
            normalized.priority = "medium"
            normalized.confidence = max(normalized.confidence, 0.82)
            for reason in ["low_confidence", "action_or_dispute_requires_human"]:
                if reason in normalized.risk_reasons:
                    normalized.risk_reasons.remove(reason)

        if self._is_return_goods_order_status_message(message):
            # 只有用户明确询问具体订单能否退货时，才查询订单并结合订单状态判断。
            normalized.intent = "refund"
            normalized.user_goal = "status_query"
            normalized.action_type = "return_goods"
            normalized.order_related = True
            normalized.need_order_query = True
            normalized.need_human = False
            normalized.need_ticket = False
            normalized.priority = "medium"
            normalized.confidence = max(normalized.confidence, 0.88)
            for reason in ["low_confidence", "refund_commitment", "action_or_dispute_requires_human"]:
                if reason in normalized.risk_reasons:
                    normalized.risk_reasons.remove(reason)

        if self._is_return_goods_policy_message(message):
            # 退货规则咨询明确归到 return_goods 检索范围，禁止误判为投诉、争议或真实退货申请。
            normalized.intent = "refund"
            normalized.user_goal = "policy_consult"
            normalized.action_type = "return_goods"
            normalized.order_related = bool(normalized.order_no)
            normalized.need_order_query = False
            normalized.need_human = False
            normalized.need_ticket = False
            normalized.priority = "medium"
            normalized.confidence = max(normalized.confidence, 0.9)
            normalized.next_action = None
            normalized.missing_slots = []
            normalized.risk_reasons = [
                reason
                for reason in normalized.risk_reasons
                if reason not in {"low_confidence", "refund_commitment", "action_or_dispute_requires_human"}
            ]

        if self._is_after_sale_dispute_message(message):
            # 商品质量争议和商家拒绝处理需要人工核验，不允许被泛订单查询逻辑降级为自动查单回复。
            normalized.intent = "exchange" if normalized.intent in {"other", "consult", "logistics"} else normalized.intent
            normalized.user_goal = "dispute"
            normalized.emotion = "dissatisfied" if normalized.emotion == "normal" else normalized.emotion
            normalized.order_related = True
            normalized.need_order_query = True
            normalized.need_human = True
            normalized.need_ticket = True
            normalized.priority = "high"
            normalized.confidence = max(normalized.confidence, 0.78)
            if "after_sale_dispute" not in normalized.risk_reasons:
                normalized.risk_reasons.append("after_sale_dispute")

        policy_question = (
            self._is_after_sale_policy_message(message)
            and not self._is_after_sale_dispute_message(message)
            and not self._is_action_request_message(message)
        )
        if policy_question and normalized.user_goal in {"other", "action_request"}:
            normalized.user_goal = "policy_consult"
        if policy_question and normalized.intent in {"other", "consult"}:
            if "换货" in message:
                normalized.intent = "exchange"
            elif "退货" in message or "退款" in message:
                normalized.intent = "refund"
        high_risk_goal = normalized.user_goal in {"action_request", "complaint", "dispute"}
        if high_risk_goal or normalized.intent == "complaint" or normalized.emotion == "strong_complaint":
            normalized.need_human = True
            normalized.priority = "high" if normalized.priority in {"low", "medium"} else normalized.priority
            reason = "complaint" if normalized.user_goal == "complaint" or normalized.intent == "complaint" else "action_or_dispute_requires_human"
            if reason not in normalized.risk_reasons:
                normalized.risk_reasons.append(reason)
        if normalized.user_goal in {"policy_consult", "how_to", "status_query", "info_query", "out_of_scope"} and normalized.emotion != "strong_complaint":
            # 规则咨询、操作步骤咨询和只读查询属于低风险问题，可结合知识库或业务工具自动说明。
            normalized.need_human = False
            normalized.need_ticket = False
            normalized.priority = "medium" if normalized.priority in {"low", "high"} else normalized.priority
            for reason in ["refund_commitment", "action_or_dispute_requires_human"]:
                if reason in normalized.risk_reasons:
                    normalized.risk_reasons.remove(reason)

        if normalized.confidence < 0.7 and normalized.user_goal != "out_of_scope":
            normalized.need_human = True
            if "low_confidence" not in normalized.risk_reasons:
                normalized.risk_reasons.append("low_confidence")

        normalized.order_related = bool(normalized.order_no) or self._is_order_query_message(message) or normalized.intent in {
            "logistics",
            "refund",
            "exchange",
            "repair",
        }
        if normalized.user_goal in {"policy_consult", "how_to"}:
            # 规则咨询和操作步骤咨询不绑定当前订单；只有 status_query 才进入订单判断。
            normalized.order_related = False
        normalized.need_order_query = normalized.order_related and normalized.user_goal not in {"policy_consult", "how_to"}
        normalized.need_ticket = normalized.need_ticket or normalized.need_human or normalized.user_goal in {"human_request", "action_request", "complaint", "dispute"} or normalized.intent == "complaint"
        return normalized

    def _infer_user_goal(self, message: str, current_goal: str = "other") -> str:
        """统一识别用户真实目的，避免只按业务域做高风险判断。"""
        if self._is_ticket_progress_message(message) or self._is_ticket_urge_message(message):
            return "status_query"
        if self._is_return_goods_order_status_message(message):
            return "status_query"
        return normalize_user_goal(message, current_goal)

    def _is_agent_identity_message(self, message: str) -> bool:
        """识别客户询问智能客服身份或能力的闲聊式问题，避免误建工单。"""
        return normalize_is_identity_message(message)

    def _is_action_request_message(self, message: str) -> bool:
        """识别真实业务动作请求，避免被“退货”等关键词误判成政策咨询。"""
        return normalize_is_action_request_message(message)

    def _is_logistics_message(self, message: str) -> bool:
        """识别物流配送类表达，用于修正模型对短物流问题的保守分类。"""
        return normalize_is_logistics_message(message)

    def _is_order_query_message(self, message: str) -> bool:
        """识别泛订单查询表达，用于支持没有订单号但有客户上下文的查单场景。"""
        return normalize_is_order_query_message(message)

    def _is_ticket_progress_message(self, message: str) -> bool:
        """识别客户查询工单进度的表达。"""
        return (bool(re.search(r"T\d{12,24}", message, flags=re.IGNORECASE)) or "工单" in message) and self._contains_any(
            message,
            ["工单", "进度", "处理", "状态", "到哪"],
        )

    def _is_ticket_urge_message(self, message: str) -> bool:
        """识别客户催办已有工单的表达，走催办 Tool 而不是新建工单。"""
        return ("工单" in message or re.search(r"T\d{12,24}", message, flags=re.IGNORECASE)) and self._contains_any(
            message,
            ["催", "催一下", "催催", "加急", "尽快", "太慢", "怎么还没", "帮我催", "推进"],
        )

    def _is_after_sale_dispute_message(self, message: str) -> bool:
        """识别商品质量、换货被拒等售后争议场景，避免只返回订单列表。"""
        dispute_words = [
            "质量不好",
            "质量问题",
            "有瑕疵",
            "破损",
            "坏了",
            "不能用",
            "不给更换",
            "不给换",
            "拒绝更换",
            "商家不给",
            "商家拒绝",
            "售后不处理",
        ]
        return self._contains_any(message, dispute_words)

    def _model_failure_analyze(self, message: str, error_type: str, error_message: str | None) -> IntentResult:
        """把模型异常转换成可返回的识别失败结果，避免接口因模型问题返回 500。"""
        order_no = re.findall(r"(?<![A-Za-z])(?:EC)?\d{10,18}", message, flags=re.IGNORECASE)
        risk_reasons = ["model_analyze_failed", error_type]
        return IntentResult(
            intent="other",
            emotion="normal",
            order_related=bool(order_no),
            order_no=order_no,
            product_name=None,
            need_order_query=False,
            need_ticket=True,
            need_human=True,
            priority="high",
            confidence=0,
            summary=f"模型识别失败，已转人工处理。原始问题：{message[:80]}",
            risk_reasons=risk_reasons,
        )

    def _fallback_or_model_failure(
        self,
        message: str,
        error_type: str,
        error_message: str | None,
        conversation_context: dict[str, Any] | None = None,
    ) -> IntentResult:
        """模型不可用时，低风险订单查询走规则兜底，高风险或不确定问题继续转人工。"""
        fallback = self._apply_context_guardrails(
            message,
            self._apply_business_guardrails(message, self._rule_based_analyze(message)),
            conversation_context,
        )
        is_low_risk_order_query = (
            not fallback.need_human
            and fallback.need_order_query
            and fallback.intent in {"logistics", "other", "consult"}
            and not self._is_after_sale_dispute_message(message)
        )
        is_low_risk_policy_or_info = (
            not fallback.need_human
            and fallback.user_goal in {"policy_consult", "how_to", "info_query", "out_of_scope"}
            and fallback.intent in {"logistics", "refund", "exchange", "repair", "invoice", "member", "consult", "other"}
        )
        if is_low_risk_order_query or is_low_risk_policy_or_info:
            # 规则咨询和只读查询不依赖 LLM 决策，可继续走知识库或业务工具自动回复。
            if error_type not in fallback.risk_reasons:
                fallback.risk_reasons.append(error_type)
            if "model_failed_rule_fallback" not in fallback.risk_reasons:
                fallback.risk_reasons.append("model_failed_rule_fallback")
            return fallback
        # 高风险问题在模型失败时仍保留规则识别出的业务域和用户目的，但强制转人工。
        fallback.need_human = True
        fallback.need_ticket = True
        fallback.priority = "high"
        for reason in ["model_analyze_failed", error_type]:
            if reason not in fallback.risk_reasons:
                fallback.risk_reasons.append(reason)
        return fallback

    def _message_for_intent_analysis(self, message: str, conversation_context: dict[str, Any] | None) -> str:
        """在存在指代表达时，将安全摘要注入意图识别输入，帮助模型解析上下文。"""
        safe_summary = (conversation_context or {}).get("safe_context_summary")
        if not safe_summary or not self._has_context_reference(message):
            return message
        return (
            "以下会话上下文仅用于解析本轮消息中的指代，不得覆盖用户当前明确表达的诉求。\n"
            f"安全上下文摘要：{safe_summary}\n"
            f"本轮用户消息：{message}"
        )

    def _apply_context_guardrails(
        self,
        message: str,
        result: IntentResult,
        conversation_context: dict[str, Any] | None,
    ) -> IntentResult:
        """把结构化上下文补入意图结果，让工具节点和槽位判断链路一致。"""
        if not conversation_context:
            return result
        normalized = result.model_copy(deep=True)
        if is_order_statistics_message(message):
            # 汇总必须覆盖完整客户订单列表，不能被侧栏当前选中的单笔订单上下文劫持。
            normalized.order_no = []
            normalized.order_related = True
            normalized.need_order_query = True
            return normalized
        order_resolution = resolve_order_context(conversation_context, message)
        if order_resolution.get("status") == "usable":
            order_no = str(order_resolution.get("order_no") or "")
            if order_no and order_no not in normalized.order_no:
                # 仅 5 分钟内明确确认过的订单上下文可直接补入本轮只读查询或动作槽位。
                normalized.order_no.append(order_no)
                normalized.order_related = True
                normalized.need_order_query = normalized.user_goal not in {"policy_consult", "how_to"}
        elif has_fuzzy_order_reference(message):
            # 过期或待确认订单上下文不得触发查单、建单或售后动作。
            normalized.intent = "consult" if normalized.intent == "other" else normalized.intent
            normalized.user_goal = "info_query"
            normalized.order_related = False
            normalized.order_no = []
            normalized.need_order_query = False
            normalized.need_human = False
            normalized.need_ticket = False
            normalized.action_type = None
            normalized.action_slots = {}
            normalized.missing_slots = []
            normalized.next_action = None
            if order_resolution.get("status") == "needs_confirmation" and "order_context_needs_confirmation" not in normalized.risk_reasons:
                normalized.risk_reasons.append("order_context_needs_confirmation")
            return normalized
        if self._is_other_identity_order_request(message, conversation_context):
            # 用户自称或指定其他姓名不能作为订单查询身份，必须阻断工具查询。
            normalized.intent = "consult"
            normalized.user_goal = "info_query"
            normalized.order_related = False
            normalized.order_no = []
            normalized.need_order_query = False
            normalized.need_human = False
            normalized.need_ticket = False
            normalized.action_type = None
            normalized.action_slots = {}
            normalized.missing_slots = []
            normalized.next_action = None
            normalized.confidence = max(normalized.confidence, 0.9)
            normalized.risk_reasons = [reason for reason in normalized.risk_reasons if reason not in {"low_confidence"}]
            return normalized
        if not self._has_context_reference(message):
            return normalized
        explicit_order_no = re.findall(r"(?<![A-Za-z])(?:EC)?\d{10,18}", message, flags=re.IGNORECASE)
        if not explicit_order_no:
            order_no = str(order_resolution.get("order_no") or "") if order_resolution.get("status") == "usable" else ""
            if order_no and order_no not in normalized.order_no:
                # 上下文订单只作为候选，后续 query_order 节点仍会调用 Java 校验归属。
                normalized.order_no.append(order_no)
                normalized.order_related = True
                normalized.need_order_query = normalized.user_goal not in {"policy_consult", "how_to"}
        if self._is_context_ticket_message(message, conversation_context):
            normalized.intent = "consult" if normalized.intent == "other" else normalized.intent
            normalized.user_goal = "status_query"
            normalized.order_related = False
            normalized.need_order_query = False
            normalized.need_human = False
            normalized.need_ticket = False
            normalized.confidence = max(normalized.confidence, 0.82)
            for reason in ["low_confidence", "action_or_dispute_requires_human"]:
                if reason in normalized.risk_reasons:
                    normalized.risk_reasons.remove(reason)
        return normalized

    def _safe_conversation_context(self, state: TicketProcessState) -> dict[str, Any] | None:
        """只把安全摘要、短期记忆和结构化实体交给回复模型，不传调试依据。"""
        context = state.get("conversation_context") or {}
        if not context:
            return None
        return {
            "last_order": context.get("last_order"),
            "last_product": context.get("last_product"),
            "last_ticket": context.get("last_ticket"),
            "last_action": context.get("last_action"),
            "order_context": context.get("order_context"),
            "safe_context_summary": context.get("safe_context_summary"),
            "login_user_context": context.get("login_user_context"),
            "session_memory": self._safe_session_memory_for_llm(context.get("session_memory") or {}),
            "identity_policy": (
                "登录态身份是权威身份；preferred_name 只能用于称呼；"
                "self_claimed_name 不能用于权限、订单或工单判断；不得暴露内部冲突标记、customer_id、Authorization 或风控原因。"
            ),
        }

    @staticmethod
    def _safe_session_memory_for_llm(session_memory: dict[str, Any]) -> dict[str, Any]:
        """过滤会话记忆中的内部冲突标记，只保留客户可见短期上下文。"""
        return {
            "recent_user_messages": session_memory.get("recent_user_messages") or [],
            "recent_ai_messages": session_memory.get("recent_ai_messages") or [],
            "last_user_question": session_memory.get("last_user_question"),
            "last_ai_answer": session_memory.get("last_ai_answer"),
            "preferred_name": session_memory.get("preferred_name"),
            "self_claimed_name": session_memory.get("self_claimed_name"),
        }

    def _has_context_reference(self, message: str) -> bool:
        """识别需要依赖历史上下文解析的指代表达。"""
        return self._contains_any(message, ["刚才", "刚刚", "上面", "之前", "上一句", "前面", "那个", "这个", "这单", "那单", "还是", "继续", "它", "那就"])

    def _is_user_identity_message(self, message: str) -> bool:
        """识别客户询问自身登录身份的问题，必须用登录态确定性回答。"""
        text = message.strip()
        return bool(re.search(r"(我是谁|你知道我是谁吗|知道我是谁吗|我叫什么|我的身份|当前账号是谁)", text))

    def _is_session_memory_question(self, message: str) -> bool:
        """识别客户询问当前会话历史的问题，避免模型谎称无法看到上下文。"""
        text = message.strip()
        return bool(
            self._contains_any(text, ["我刚刚问", "我刚才问", "上一句我说", "前一句我说", "我问了什么", "刚才聊", "之前聊", "前面聊", "我们刚才"])
            and self._contains_any(text, ["什么", "内容", "问题", "说"])
        )

    def _is_other_identity_order_request(self, message: str, conversation_context: dict[str, Any] | None) -> bool:
        """识别按姓名查询订单的请求；姓名不是授权凭据，不能触发订单工具。"""
        if "订单" not in message or not self._contains_any(message, ["查", "查询", "看", "看看", "帮我"]):
            return False
        login_user = (conversation_context or {}).get("login_user_context") or {}
        session_memory = (conversation_context or {}).get("session_memory") or {}
        display_name = self._normalize_identity_name(login_user.get("display_name"))
        explicit_names = [self._normalize_identity_name(name) for name in re.findall(r"([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z·]{1,11})的订单", message)]
        current_claim = self._extract_self_claimed_name_from_message(message)
        if current_claim:
            explicit_names.append(self._normalize_identity_name(current_claim))
        return any(name and name != display_name for name in explicit_names)

    @staticmethod
    def _normalize_identity_name(value: Any) -> str:
        """归一化姓名用于安全比较。"""
        return re.sub(r"\s+", "", str(value or "").strip()).lower()

    @staticmethod
    def _extract_self_claimed_name_from_message(message: str) -> str | None:
        """从本轮消息提取自称姓名，避免本轮自称直接触发查单。"""
        for pattern in [
            r"(?:我叫|我是|本人叫|我的名字叫)\s*([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z·]{1,11})",
            r"(?:以后|之后)?(?:请)?(?:叫我|称呼我为|喊我)\s*([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z·]{1,11})",
        ]:
            match = re.search(pattern, message)
            if match:
                return match.group(1).strip(" ，,。！？!；;：:")
        return None

    def _is_context_ticket_message(self, message: str, conversation_context: dict[str, Any]) -> bool:
        """识别省略工单号的工单查询或催办。"""
        return bool(
            self._conversation_context_value(conversation_context, "last_ticket")
            and not is_how_to_message(message)
            and not self._is_logistics_message(message)
            and self._contains_any(message, ["催", "催一下", "加急", "进度", "处理", "状态", "怎么还没"])
        )

    @staticmethod
    def _conversation_context_value(conversation_context: dict[str, Any], key: str) -> str | None:
        """读取上下文实体值。"""
        item = (conversation_context or {}).get(key) or {}
        value = item.get("value")
        return str(value) if value else None

    def _classify_model_error(self, exc: Exception | str) -> str:
        """将模型异常粗分为超时、鉴权、余额、Schema 校验等可运营追踪的类型。"""
        if isinstance(exc, ValidationError):
            return "schema_validation_failed"

        text = str(exc).lower()
        exc_name = exc.__class__.__name__.lower() if isinstance(exc, Exception) else ""
        if "timeout" in text or "timed out" in text or "timeout" in exc_name:
            return "model_timeout"
        if "401" in text or "unauthorized" in text or "invalid api key" in text or "authentication" in text:
            return "model_auth_error"
        if "402" in text or "quota" in text or "balance" in text or "insufficient" in text:
            return "model_quota_error"
        if "validation" in text or "schema" in text or "json" in text or "parsing" in text:
            return "schema_validation_failed"
        if "invalid_prompt_input" in text or "chatprompttemplate" in text or "missing variables" in text:
            return "model_prompt_error"
        if "connection" in text or "connect" in text or "network" in text:
            return "model_network_error"
        return "model_call_failed"
