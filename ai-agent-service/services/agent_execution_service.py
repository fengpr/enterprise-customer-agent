"""在线 Agent 执行服务，隔离 FastAPI 接口编排与真实业务执行。"""

import os
from datetime import datetime
from typing import Any, Callable

from agents.conversation_context import build_conversation_context
from agents.customer_service_agent import CustomerServiceAgent
from repositories.chat_repository import ChatMessageRepository, ChatSessionRepository
from repositories.evaluation_repository import EvaluationRepository
from schemas.intent_schema import AgentReplyRequest
from services.resilient_client import ResilienceError
from services.resilient_client import ResilientClient
from services.staff_presence_service import StaffPresenceService


class AgentExecutionAccessDenied(Exception):
    """表示已认证客户无权访问目标会话，由接口层映射为 403 响应。"""


class AgentExecutionService:
    """负责一次客户咨询的会话、Agent、人工接管和 Trace 完整执行。"""

    def __init__(
        self,
        *,
        agent: CustomerServiceAgent | None = None,
        chat_sessions: ChatSessionRepository | None = None,
        chat_messages: ChatMessageRepository | None = None,
        evaluation_repository: EvaluationRepository | None = None,
        staff_availability_loader: Callable[[], list[dict[str, Any]]] | None = None,
        staff_presence_checker: Callable[[str], bool] | None = None,
    ) -> None:
        """注入执行依赖，方便 API 与 Worker 复用同一服务并进行单元测试。"""
        self.agent = agent or CustomerServiceAgent()
        self.chat_sessions = chat_sessions or ChatSessionRepository()
        self.chat_messages = chat_messages or ChatMessageRepository(self.chat_sessions)
        self.evaluation_repository = evaluation_repository or EvaluationRepository()
        self.business_service_url = os.getenv("BUSINESS_SERVICE_URL", "http://localhost:8081")
        self.internal_secret = os.getenv("AGENT_INTERNAL_SECRET", "enterprise-customer-agent-demo-internal-secret")
        self.human_service_start = os.getenv("HUMAN_SERVICE_START", "09:00")
        self.human_service_end = os.getenv("HUMAN_SERVICE_END", "18:00")
        self.staff_availability_loader = staff_availability_loader or self._load_staff_members_internal
        self.staff_presence_checker = staff_presence_checker or StaffPresenceService().is_online
        self.staff_client = ResilientClient(downstream="java_staff")

    def execute(self, payload: AgentReplyRequest, event_publisher: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """执行完整客服 Agent 链路；调用前必须已由接口层完成客户鉴权并注入身份。"""
        if payload.customer_id is None:
            raise ValueError("AgentExecutionService 执行前必须注入 customer_id")
        def emit(event_type: str, event_payload: dict[str, Any] | None = None) -> None:
            """只向 SSE 发布客户可见阶段信息，事件写入失败不影响业务执行。"""
            if event_publisher:
                event_publisher(event_type, event_payload or {})

        emit("retrieving", {"status": "正在检索知识库"})
        session = self._get_or_create_session(payload)
        session_id = session["session_id"]
        route_target = payload.route_target or "ai"
        if route_target == "human":
            return self._save_manual_handoff_message(session_id, payload, session)

        pending_action_request = self._latest_pending_action_request(session_id)
        conversation_context = build_conversation_context(
            messages=self.chat_messages.list_by_session(session_id),
            pending_action_request=pending_action_request,
            selected_order_no=payload.selected_order_no,
            selected_ticket_no=payload.selected_ticket_no,
            login_user_context=payload.login_user_context,
        )
        self.chat_messages.save(
            session_no=session_id,
            sender_type="customer",
            sender_id=str(payload.customer_id),
            content=payload.message,
            extra_data={
                "route_target": route_target,
                # 仅保存订单号本身，不保存 Token；用于后续同一 session 内的短期订单上下文有效期判断。
                "selected_order_no": payload.selected_order_no,
            },
        )
        if route_target == "both":
            self._ensure_handoff_exists(session_id, session, "synced_by_customer")

        agent_payload = payload.model_copy(
            update={
                "session_id": session_id,
                "pending_action_request": pending_action_request,
                "conversation_context": conversation_context,
                "login_user_context": conversation_context.get("login_user_context"),
            }
        )
        try:
            emit("tool_calling", {"status": "正在调用业务工具"})
            emit("generating", {"status": "正在生成回答", "streaming_supported": bool(getattr(self.agent, "llm_analyzer", None))})
            if event_publisher and hasattr(self.agent, "reply_with_stream"):
                result = self.agent.reply_with_stream(
                    agent_payload,
                    lambda delta: emit("delta", {"text": delta}),
                ).model_dump()
            else:
                result = self.agent.reply(agent_payload).model_dump()
        except ResilienceError as exc:
            # 韧性层只负责归类错误，客户侧安全降级与转人工由执行服务统一决定。
            result = self._resilience_degraded_result(exc)
        result["session_id"] = session_id
        if result.get("degraded"):
            # 降级必须落入现有人工接管闭环；会话状态具备幂等保护，不会重复创建人工处理项。
            self._ensure_handoff_exists(session_id, session, f"degraded_{(result.get('risk_reasons') or ['unknown'])[0]}")
            result["handoff_result"] = {"status": "queued", "reason": "degraded", "ticket_no": None}
            result["customer_visible_message"] = result.get("customer_message") or result.get("answer")
            result["execution_status"] = "degraded"
        if (result.get("analysis") or {}).get("user_goal") == "human_request":
            handoff_result = self._prepare_handoff_response(session_id)
            result.update(
                {
                    "answer": handoff_result["message"],
                    "customer_message": handoff_result["message"],
                    "service_status": handoff_result["service_status"],
                    "decision_type": "human_takeover",
                    "need_human": True,
                    "auto_send": False,
                    "ticket_result": None,
                    "handoff_result": handoff_result,
                }
            )
        self._persist_result(session_id, payload, result, conversation_context)
        result.setdefault("execution_status", "success")
        result.setdefault("customer_visible_message", result.get("customer_message") or result.get("answer"))
        result.setdefault("ticket_no", ((result.get("ticket_result") or {}).get("data") or {}).get("ticketNo"))
        return result

    @staticmethod
    def _resilience_degraded_result(error: ResilienceError) -> dict[str, Any]:
        """将不可恢复的在线下游错误转换为可追踪、可转人工的安全结果。"""
        message = "当前服务繁忙，已为您转入人工客服队列，请稍后查看处理进度。"
        if error.error_type == "4xx":
            message = "当前请求暂未被业务服务接受，已为您转入人工客服进一步处理。"
        return {
            "answer": message,
            "customer_message": message,
            "internal_suggestion": None,
            "decision_type": "human_takeover",
            "service_status": "排队等待人工处理",
            "auto_send": False,
            "need_human": True,
            "analysis": {"intent": "other", "user_goal": "other", "risk_reasons": [f"resilience_{error.error_type}"], "confidence": 0.0},
            "citations": [],
            "citation_validation": {},
            "tool_results": [],
            "ticket_result": None,
            "risk_reasons": [f"resilience_{error.error_type}"],
            "pending_action_request": None,
            "degraded": True,
            "retry_after": 10 if error.retryable else None,
        }

    def _persist_result(self, session_id: str, payload: AgentReplyRequest, result: dict[str, Any], conversation_context: dict[str, Any]) -> None:
        """持久化客户可见回复和评测 Trace；评测采集失败不影响客户回复。"""
        self.chat_sessions.update_after_agent_reply(session_id, result["analysis"], self._resolve_session_status(result))
        persisted_context = self._safe_persisted_conversation_context(conversation_context)
        self.chat_messages.save(
            session_no=session_id,
            sender_type="ai",
            sender_id="agent",
            content=result.get("customer_message") or result["answer"],
            extra_data={
                "customer_message": result.get("customer_message"), "internal_suggestion": result.get("internal_suggestion"),
                "decision_type": result.get("decision_type"), "service_status": result.get("service_status"),
                "analysis": result["analysis"], "citations": result["citations"], "tool_results": result["tool_results"],
                "ticket_result": result.get("ticket_result"), "risk_reasons": result["risk_reasons"],
                "auto_send": result["auto_send"], "need_human": result["need_human"],
                "handoff_result": result.get("handoff_result"), "pending_action_request": result.get("pending_action_request"),
                "conversation_context": persisted_context,
                "context_conflict": (conversation_context.get("debug_context") or {}).get("context_conflict"),
            },
        )
        try:
            self.evaluation_repository.capture_online_trace({
                "customer_id": payload.customer_id, "message": payload.message,
                "answer": result.get("customer_message") or result["answer"], "citations": result.get("citations", []),
                "tool_results": result.get("tool_results", []), "analysis": result.get("analysis", {}),
                "citation_validation": result.get("citation_validation", {}), "decision_type": result.get("decision_type"),
            })
        except Exception:
            pass

    @staticmethod
    def _safe_persisted_conversation_context(conversation_context: dict[str, Any]) -> dict[str, Any]:
        """保存到客户消息扩展字段前移除内部身份冲突标记，避免前端拿到调试状态。"""
        safe_context = dict(conversation_context or {})
        session_memory = dict(safe_context.get("session_memory") or {})
        session_memory.pop("identity_conflict", None)
        # pending_action 属于后端流程控制状态，客户侧历史消息接口不得直接展示。
        session_memory.pop("pending_action", None)
        safe_context["session_memory"] = session_memory
        return safe_context

    def _get_or_create_session(self, payload: AgentReplyRequest) -> dict[str, Any]:
        """续接已授权会话或创建新会话，防止客户越权追加他人消息。"""
        if payload.session_id:
            existing = self.chat_sessions.get_by_session_no_for_customer(payload.session_id, payload.customer_id)
            if existing:
                return existing
            raise AgentExecutionAccessDenied("无权访问该会话")
        return self.chat_sessions.create(payload.customer_id, payload.message)

    def _latest_pending_action_request(self, session_id: str) -> dict[str, Any] | None:
        """读取最近未完成动作，保持多轮槽位补全行为不变。"""
        now = datetime.utcnow()
        for message in reversed(self.chat_messages.list_by_session(session_id)):
            if message.get("sender_type") != "ai":
                continue
            pending = (message.get("extra_data") or {}).get("pending_action_request")
            if not pending:
                continue
            if pending.get("completed") or pending.get("status") in {"completed", "cancelled"}:
                # 最新终止状态是该动作的墓碑；禁止继续向前扫描并复活同一会话里的旧 pending。
                return None
            expires_at = pending.get("expires_at") or pending.get("expire_at")
            if expires_at:
                try:
                    if datetime.fromisoformat(str(expires_at)) <= now:
                        # 已过期状态仍交给本轮规则做“重新确认诉求”，但不会作为可执行动作恢复。
                        return pending
                except ValueError:
                    pass
            return pending
        return None

    def _prepare_handoff_response(self, session_id: str) -> dict[str, Any]:
        """创建人工接管请求，并按服务时间和坐席容量生成客户可见话术。"""
        current = self.chat_sessions.get_by_session_no(session_id) or {}
        if current.get("handoff_status") == "ACTIVE":
            return {"status": "active", "reason": "already_active", "service_status": "人工客服处理中", "message": "人工客服已接入当前会话，无需重复排队。您可以切换到人工客服继续补充信息。", "availability": {}}
        if current.get("handoff_status") == "PENDING":
            return {"status": "queued", "reason": "already_pending", "service_status": "人工客服排队中", "message": "当前会话已在人工客服队列中，无需重复提交。客服接入后会继续跟进。", "availability": {}}
        availability = self._load_human_availability()
        if not availability["in_service_time"]:
            self.chat_sessions.request_handoff(session_id, "off_hours")
            return {"status": "queued", "reason": "off_hours", "service_status": "已记录人工请求，等待工作时间处理", "message": f"当前人工客服不在服务时间内，已为您记录人工服务请求。人工服务时间为 {self.human_service_start}-{self.human_service_end}，工作人员上线后会优先处理。", "availability": availability}
        self.chat_sessions.request_handoff(session_id, "human_requested")
        if availability["available_staff_count"] <= 0:
            return {"status": "queued", "reason": "busy", "service_status": "人工客服繁忙，已进入排队", "message": "当前人工客服较忙，已为您进入人工排队。请您稍等，工作人员空闲后会接入处理。", "availability": availability}
        return {"status": "waiting", "reason": "available", "service_status": "等待人工客服接入", "message": "已为您提交人工请求，请稍候，工作人员会继续跟进本次会话。", "availability": availability}

    def _save_manual_handoff_message(self, session_id: str, payload: AgentReplyRequest, session: dict[str, Any]) -> dict[str, Any]:
        """保存客户发送给人工客服的补充内容，不触发 AI 生成。"""
        active = session.get("handoff_status") == "ACTIVE"
        self._ensure_handoff_exists(session_id, session, "manual_message")
        self.chat_messages.save(session_no=session_id, sender_type="customer", sender_id=str(payload.customer_id), content=payload.message, extra_data={"route_target": "human", "message_source": "manual_handoff_customer_message"})
        message = "您的补充内容已发送给当前人工客服。" if active else "您的补充内容已记录到人工服务请求中，客服接入后会一并查看。"
        return self._manual_session_ack(session_id, session, message)

    def _ensure_handoff_exists(self, session_id: str, session: dict[str, Any], reason: str) -> None:
        """保证人工请求幂等，已有挂起或接入状态时不重复创建。"""
        if session.get("handoff_status") not in {"PENDING", "ACTIVE"}:
            self.chat_sessions.request_handoff(session_id, reason)

    def _manual_session_ack(self, session_id: str, session: dict[str, Any], message: str) -> dict[str, Any]:
        """构造人工通道的兼容 AgentReply 响应。"""
        active = session.get("handoff_status") == "ACTIVE"
        reasons = ["manual_handoff_active" if active else "manual_handoff_pending"]
        return {"session_id": session_id, "answer": message, "customer_message": message, "internal_suggestion": None, "decision_type": "human_takeover", "service_status": "人工客服处理中" if active else "人工请求已挂起", "auto_send": False, "need_human": True, "analysis": {"intent": "consult", "user_goal": "human_request", "emotion": "normal", "order_related": False, "order_no": [], "product_name": None, "need_order_query": False, "need_ticket": False, "need_human": True, "priority": "medium", "confidence": 1.0, "summary": "人工会话补充消息", "risk_reasons": reasons, "action_type": None, "action_slots": {}, "missing_slots": [], "next_action": "transfer_human"}, "citations": [], "tool_results": [], "ticket_result": None, "risk_reasons": reasons, "pending_action_request": None}

    def _load_human_availability(self) -> dict[str, Any]:
        """汇总人工服务时间和坐席负载，只输出安全摘要。"""
        in_service_time = self._is_human_service_time()
        staff_members = self.staff_availability_loader() if in_service_time else []
        available = [
            staff
            for staff in staff_members
            if staff.get("online")
            and staff.get("acceptingTickets")
            and self.staff_presence_checker(str(staff.get("userId")))
            and int(staff.get("activeTickets") or 0)
            + self.chat_sessions.count_active_handoff_by_staff(str(staff.get("userId")))
            < int(staff.get("maxActiveTickets") or 0)
        ]
        return {"in_service_time": in_service_time, "service_start": self.human_service_start, "service_end": self.human_service_end, "staff_count": len(staff_members), "available_staff_count": len(available)}

    def _load_staff_members_internal(self) -> list[dict[str, Any]]:
        """读取 Java 内部坐席容量；不可用时按繁忙处理。"""
        try:
            response = self.staff_client.request_sync("GET", f"{self.business_service_url}/api/internal/staff/availability", headers={"X-Agent-Internal-Secret": self.internal_secret})
            data = response.json()
            return data.get("members") if isinstance(data, dict) and isinstance(data.get("members"), list) else []
        except (ResilienceError, ValueError):
            return []

    def _is_human_service_time(self) -> bool:
        """判断当前时间是否位于人工服务窗口，兼容跨午夜配置。"""
        now, start, end = self._time_to_minutes(datetime.now().strftime("%H:%M")), self._time_to_minutes(self.human_service_start), self._time_to_minutes(self.human_service_end)
        return start <= now < end if start <= end else now >= start or now < end

    @staticmethod
    def _time_to_minutes(value: str) -> int:
        """转换 HH:mm 配置，非法值降级为零点。"""
        try:
            hour, minute = value.split(":", 1)
            return int(hour) * 60 + int(minute)
        except (ValueError, AttributeError):
            return 0

    @staticmethod
    def _resolve_session_status(agent_result: dict[str, Any]) -> str:
        """将 Agent 决策映射为会话状态，保持原有状态流转。"""
        if agent_result.get("handoff_result"):
            return "AI_REPLIED"
        ticket_result = agent_result.get("ticket_result") or {}
        if ticket_result.get("status") == "success":
            return "CREATED_TICKET"
        if agent_result.get("decision_type") == "human_takeover":
            return "AI_REPLIED"
        if agent_result.get("decision_type") == "review_required":
            return "AI_REVIEW"
        if agent_result.get("decision_type") == "auto_reply":
            return "AI_REPLIED"
        return "AI_REPLIED" if agent_result.get("need_human") else "AI_ONLY"
