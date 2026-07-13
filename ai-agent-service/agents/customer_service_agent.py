import os
import re
from datetime import datetime
from typing import Any

from langchain_core.runnables import RunnableLambda
from pydantic import ValidationError

from agents.action_request import ACTION_SLOT_RULES, enrich_action_analysis
from agents.intent_normalizer import (
    infer_intent as normalize_intent,
    infer_user_goal as normalize_user_goal,
    is_action_request_message as normalize_is_action_request_message,
    is_high_risk_out_of_scope_message,
    is_how_to_message,
    is_human_request_message,
    is_identity_message as normalize_is_identity_message,
    is_logistics_message as normalize_is_logistics_message,
    is_order_query_message as normalize_is_order_query_message,
    is_out_of_scope_message,
)
from agents.llm_intent_analyzer import LLMIntentAnalyzer
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

    def reply(self, request: AgentReplyRequest) -> AgentReply:
        """执行完整客服 Agent 图，输出回复内容、是否转人工、引用和工具结果。"""
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
            }
        )

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
            return "已提交，待分派"
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

    def reply_with_stream(self, request: AgentReplyRequest, on_delta) -> AgentReply:
        """执行既有 Agent 图，并把最终生成节点的模型原生 token 回调给 Worker。"""
        self._stream_delta_callback = on_delta
        try:
            return self.reply(request)
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

        if analysis.user_goal == "info_query" and self._is_agent_identity_message(state["message"]):
            return self._compose_agent_identity_answer(state)

        if analysis.user_goal == "out_of_scope":
            return self._compose_out_of_scope_answer(state)

        if analysis.user_goal == "human_request":
            return self._compose_human_request_answer(state)

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
        if analysis.need_order_query and not analysis.order_no and not self._has_customer_order_attempt(state):
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
                "随后用“不过我主要负责订单、物流、售后、工单和发票等业务问题”这类自然转折说明服务边界。"
                "不要自我介绍成企业客服助手，不要使用生硬的身份声明。"
                "不要声称查过知识库，不要转人工，不要创建工单，不要引用当前订单或历史会话中的业务对象。"
            ),
            extra_context={"allowed_scope": ["订单", "物流", "售后", "工单", "发票"]},
            use_business_context=False,
        )
        if llm_answer:
            return self._ensure_out_of_scope_boundary(llm_answer)

        # 模型未配置或调用失败时保留安全兜底，但不伪装成已经回答了用户问题。
        return (
            "抱歉，我暂时无法可靠回答这个问题。"
            "我主要帮助处理订单、物流、售后、工单和发票问题，您可以直接告诉我相关诉求。"
        )

    @staticmethod
    def _ensure_out_of_scope_boundary(answer: str) -> str:
        """统一普通越界回复的边界表达，避免模型生成突兀的客服身份声明。"""
        normalized = answer.strip()
        boundary = "不过我主要负责订单、物流、售后、工单和发票等业务问题。如果您有这些方面的疑问，也可以继续问我。"
        if not normalized:
            return boundary

        # 低风险越界回答只保留常识简答，把能力边界收敛成统一自然话术。
        paragraphs = [item.strip() for item in re.split(r"\n{1,}", normalized) if item.strip()]
        kept: list[str] = []
        boundary_terms = [
            "企业客服助手",
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
        for paragraph in paragraphs:
            # 模型常把边界单独放在第二段；统一替换这类段落，避免客户感到突兀。
            if any(term in paragraph for term in boundary_terms):
                continue
            kept.append(paragraph)

        answer_body = "\n".join(kept).strip()
        if not answer_body:
            return boundary
        return f"{answer_body}\n\n{boundary}"

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

        if "order_no" in missing and "after_sale_reason" in missing:
            suffix = f"\n\n您也可以从这些订单中选择：\n{order_options}" if order_options else ""
            return f"请问您要处理哪一笔订单？可以选择订单或直接回复订单号，并说明{action_name}原因。{suffix}"
        if "order_no" in missing:
            suffix = f"\n\n您也可以从这些订单中选择：\n{order_options}" if order_options else ""
            return f"请问您要处理哪一笔订单？可以选择订单或直接回复订单号。{suffix}"
        if "after_sale_reason" in missing:
            prefix = f"已关联订单 {order_no}，" if order_no else ""
            return f"{prefix}请补充{action_name}原因，例如商品质量问题、拍错、不想要、配件缺失等。"
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
            f"预计送达时间：{self._format_time(logistics.get('estimatedDeliveryTime'))}",
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

        if self._is_agent_identity_message(message):
            # “你是谁/你能做什么”是基础信息咨询，不继承订单、工单或售后动作上下文。
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
        if not conversation_context or not self._has_context_reference(message):
            return result
        normalized = result.model_copy(deep=True)
        explicit_order_no = re.findall(r"(?<![A-Za-z])(?:EC)?\d{10,18}", message, flags=re.IGNORECASE)
        if not explicit_order_no:
            order_no = self._conversation_context_value(conversation_context, "last_order")
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
        """只把安全摘要和结构化实体交给回复模型，不传历史原文或调试依据。"""
        context = state.get("conversation_context") or {}
        if not context:
            return None
        return {
            "last_order": context.get("last_order"),
            "last_product": context.get("last_product"),
            "last_ticket": context.get("last_ticket"),
            "last_action": context.get("last_action"),
            "safe_context_summary": context.get("safe_context_summary"),
        }

    def _has_context_reference(self, message: str) -> bool:
        """识别需要依赖历史上下文解析的指代表达。"""
        return self._contains_any(message, ["刚才", "上面", "之前", "那个", "这个", "这单", "那单", "还是", "继续", "它", "那就"])

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
