import os
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException

from agents.conversation_context import build_conversation_context
from agents.customer_service_agent import CustomerServiceAgent
from repositories.chat_repository import ChatMessageRepository, ChatSessionRepository
from schemas.intent_schema import AgentReplyRequest, AnalyzeRequest, ToolCallRequest
from tools.order_tools import OrderTools
from tools.ticket_tools import TicketTools

app = FastAPI(title="Enterprise Customer Agent Service", version="0.1.0")
agent = CustomerServiceAgent()
order_tools = OrderTools()
ticket_tools = TicketTools()
chat_sessions = ChatSessionRepository()
chat_messages = ChatMessageRepository(chat_sessions)
BUSINESS_SERVICE_URL = os.getenv("BUSINESS_SERVICE_URL", "http://localhost:8081")
AGENT_INTERNAL_SECRET = os.getenv("AGENT_INTERNAL_SECRET", "enterprise-customer-agent-demo-internal-secret")
HUMAN_SERVICE_START = os.getenv("HUMAN_SERVICE_START", "09:00")
HUMAN_SERVICE_END = os.getenv("HUMAN_SERVICE_END", "18:00")


@app.on_event("startup")
def startup_checks() -> None:
    """服务启动时只检查外部依赖状态，禁止自动建表、建索引或导入知识库。"""
    agent.rag.check_startup()


@app.get("/health")
def health() -> dict:
    """提供服务存活检查，便于前端、网关或部署平台判断 Agent 服务是否可用。"""
    return {"status": "ok"}


@app.get("/api/agent/status")
def status() -> dict:
    """返回 Agent 和 LLM 配置状态，用于排查前端无输出问题。"""
    return {"status": "ok", "llm": agent.llm_status()}


@app.post("/api/auth/login")
def login(payload: dict[str, Any]) -> dict:
    """代理 Java 业务系统登录接口，让前端只需要访问 Agent 服务。"""
    try:
        response = httpx.post(f"{BUSINESS_SERVICE_URL}/api/auth/login", json=payload, timeout=5.0)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=_response_detail(exc.response)) from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"业务登录服务不可用：{exc}") from exc


@app.get("/api/auth/current-user")
def current_user(authorization: str | None = Header(default=None)) -> dict:
    """通过 Java 业务系统校验 Token 并返回当前登录用户。"""
    return _current_login_user(authorization)


@app.post("/api/agent/analyze")
def analyze(payload: AnalyzeRequest) -> dict:
    """对用户输入做结构化意图识别，供客服工作台展示 AI 分析结果。"""
    return agent.analyze(payload.message).model_dump()


@app.post("/api/agent/reply")
def reply(payload: AgentReplyRequest, authorization: str | None = Header(default=None)) -> dict:
    """执行完整 Agent 回复流程，使用 Java Token 解析出的客户身份驱动查单和建单。"""
    current_user_data = _current_login_user(authorization)
    # 客户身份以后端认证结果为准，忽略前端伪造或过期的 customer_id。
    payload = payload.model_copy(
        update={
            "customer_id": current_user_data["customer_id"],
            "auth_token": _bearer_token(authorization),
        }
    )

    session = _get_or_create_session(payload)
    session_id = session["session_id"]
    route_target = payload.route_target or "ai"

    if route_target == "human":
        # 用户显式选择发给人工客服时，只补充到人工队列，不触发智能体回答。
        return _save_manual_handoff_message(session_id, payload, session)

    pending_action_request = _latest_pending_action_request(session_id)
    conversation_context = build_conversation_context(
        messages=chat_messages.list_by_session(session_id),
        pending_action_request=pending_action_request,
        selected_order_no=payload.selected_order_no,
        selected_ticket_no=payload.selected_ticket_no,
    )

    # 先保存用户原始问题，确保即使 Agent 失败也能追溯会话输入。
    chat_messages.save(
        session_no=session_id,
        sender_type="customer",
        sender_id=str(payload.customer_id) if payload.customer_id else None,
        content=payload.message,
        extra_data={"route_target": route_target},
    )

    if route_target == "both":
        # both 用于“请 AI 回答，同时把原文同步给人工”，坐席端可通过消息扩展字段识别。
        _ensure_handoff_exists(session_id, session, "synced_by_customer")

    agent_payload = payload.model_copy(
        update={
            "session_id": session_id,
            "pending_action_request": pending_action_request,
            "conversation_context": conversation_context,
        }
    )
    agent_reply = agent.reply(agent_payload)
    result = agent_reply.model_dump()
    result["session_id"] = session_id

    if (result.get("analysis") or {}).get("user_goal") == "human_request":
        # 用户明确要求人工时创建人工接管请求，而不是创建业务工单。
        handoff_result = _prepare_handoff_response(session_id)
        result["answer"] = handoff_result["message"]
        result["customer_message"] = handoff_result["message"]
        result["service_status"] = handoff_result["service_status"]
        result["decision_type"] = "human_takeover"
        result["need_human"] = True
        result["auto_send"] = False
        result["ticket_result"] = None
        result["handoff_result"] = handoff_result

    status_value = _resolve_session_status(result)
    chat_sessions.update_after_agent_reply(session_id, result["analysis"], status_value)
    chat_messages.save(
        session_no=session_id,
        sender_type="ai",
        sender_id="agent",
        content=result.get("customer_message") or result["answer"],
        extra_data={
            "customer_message": result.get("customer_message"),
            "internal_suggestion": result.get("internal_suggestion"),
            "decision_type": result.get("decision_type"),
            "service_status": result.get("service_status"),
            "analysis": result["analysis"],
            "citations": result["citations"],
            "tool_results": result["tool_results"],
            "ticket_result": result.get("ticket_result"),
            "risk_reasons": result["risk_reasons"],
            "auto_send": result["auto_send"],
            "need_human": result["need_human"],
            "handoff_result": result.get("handoff_result"),
            "pending_action_request": result.get("pending_action_request"),
            "conversation_context": conversation_context,
            "context_conflict": (conversation_context.get("debug_context") or {}).get("context_conflict"),
        },
    )
    return result


@app.post("/api/agent/tool/call")
def tool_call(payload: ToolCallRequest, authorization: str | None = Header(default=None)) -> dict:
    """提供受控工具调用入口，避免 Agent 绕过白名单访问业务系统。"""
    # 工具名称采用白名单判断，防止外部请求调用未授权工具。
    if payload.tool_name == "query_order":
        _current_login_user(authorization)
        return order_tools.query_order(payload.arguments.get("order_no", ""), _bearer_token(authorization))
    if payload.tool_name == "query_customer_orders":
        current_user_data = _current_login_user(authorization)
        return order_tools.query_customer_orders(current_user_data["customer_id"], _bearer_token(authorization))
    if payload.tool_name == "query_order_logistics":
        _current_login_user(authorization)
        return order_tools.query_order_logistics(payload.arguments.get("order_no", ""), _bearer_token(authorization))
    if payload.tool_name == "create_ticket":
        current_user_data = _current_login_user(authorization)
        arguments = dict(payload.arguments)
        # 工具入口也必须以 Token 中客户 ID 为准，防止外部伪造 customerId 建单。
        arguments["customerId"] = current_user_data["customer_id"]
        return ticket_tools.create_ticket(arguments, _bearer_token(authorization))
    if payload.tool_name == "query_ticket_status":
        _current_login_user(authorization)
        return ticket_tools.query_ticket_status(payload.arguments.get("ticket_no", ""), _bearer_token(authorization))
    if payload.tool_name == "urge_ticket":
        _current_login_user(authorization)
        return ticket_tools.urge_ticket(
            payload.arguments.get("ticket_no", ""),
            payload.arguments.get("reason"),
            _bearer_token(authorization),
        )
    return {"status": "failed", "error": f"Unsupported tool: {payload.tool_name}"}


@app.get("/api/agent/logs")
def logs(limit: int = 100) -> list[dict]:
    """返回 Agent 工具调用日志，用于客服主管追溯 AI 决策依据。"""
    return agent.list_call_logs(limit)


@app.get("/api/chat/session/list")
def list_chat_sessions(limit: int = 50, authorization: str | None = Header(default=None)) -> list[dict]:
    """查询最近客服会话列表，供前端左侧会话队列展示。"""
    current_user_data = _current_login_user(authorization)
    return chat_sessions.list_recent_for_customer(current_user_data["customer_id"], limit)


@app.post("/api/chat/session")
def create_chat_session(payload: dict[str, Any] | None = None, authorization: str | None = Header(default=None)) -> dict:
    """显式创建一个新的客户会话，便于客户从空白上下文开始咨询。"""
    current_user_data = _current_login_user(authorization)
    title = str((payload or {}).get("title") or "新会话").strip() or "新会话"
    return chat_sessions.create(current_user_data["customer_id"], title)


@app.get("/api/chat/session/{session_id}")
def get_chat_session(session_id: str, authorization: str | None = Header(default=None)) -> dict:
    """查询会话详情和消息历史，供客服工作台展示完整上下文。"""
    current_user_data = _current_login_user(authorization)
    session = chat_sessions.get_by_session_no_for_customer(session_id, current_user_data["customer_id"])
    if not session:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return {
        "session": session,
        "messages": chat_messages.list_by_session_for_customer(session_id, current_user_data["customer_id"]),
    }


@app.delete("/api/chat/session/{session_id}")
def delete_chat_session(session_id: str, authorization: str | None = Header(default=None)) -> dict:
    """软删除当前客户自己的会话，客户侧列表隐藏但保留工单审计上下文。"""
    current_user_data = _current_login_user(authorization)
    deleted = chat_sessions.soft_delete_for_customer(session_id, current_user_data["customer_id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在或已删除")
    return {"status": "success", "session_id": session_id}


@app.get("/api/customer/tickets/{ticket_no}")
def get_customer_ticket(ticket_no: str, authorization: str | None = Header(default=None)) -> dict:
    """代理查询客户自己的工单详情，让客户侧页面能刷新 Java 业务系统中的最新状态。"""
    _current_login_user(authorization)
    try:
        response = httpx.get(
            f"{BUSINESS_SERVICE_URL}/api/tickets/{ticket_no}",
            headers={"Authorization": authorization},
            timeout=5.0,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400:
            # 历史会话可能引用了已清理或旧 SQLite 文件中的工单；客户侧降级展示，不把控制台刷成错误。
            return {
                "ticketNo": ticket_no,
                "status": "状态暂不可同步",
                "source": "HISTORY_FALLBACK",
            }
        raise HTTPException(status_code=exc.response.status_code, detail=_response_detail(exc.response)) from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"业务工单服务不可用：{exc}") from exc


@app.get("/api/customer/tickets")
def list_customer_tickets(authorization: str | None = Header(default=None)) -> list[dict]:
    """代理查询当前客户自己的工单列表，供客户侧工单列表和进度面板使用。"""
    _current_login_user(authorization)
    try:
        response = httpx.get(
            f"{BUSINESS_SERVICE_URL}/api/tickets",
            headers={"Authorization": authorization},
            timeout=5.0,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=_response_detail(exc.response)) from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"业务工单列表服务不可用：{exc}") from exc


@app.post("/api/customer/tickets/{ticket_no}/urge")
def urge_customer_ticket(
    ticket_no: str,
    payload: dict[str, Any] | None = None,
    authorization: str | None = Header(default=None),
) -> dict:
    """代理客户催办自己的工单，催办由 Java 按 Token 校验归属并落库。"""
    _current_login_user(authorization)
    try:
        response = httpx.post(
            f"{BUSINESS_SERVICE_URL}/api/tickets/{ticket_no}/urge",
            json={"reason": str((payload or {}).get("reason") or "客户催办处理进度")},
            headers={"Authorization": authorization},
            timeout=5.0,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=_response_detail(exc.response)) from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"业务工单催办服务不可用：{exc}") from exc


@app.get("/api/customer/orders")
def list_customer_orders(authorization: str | None = Header(default=None)) -> list[dict]:
    """代理查询当前客户订单列表，供客户侧选择订单后发起咨询。"""
    current_user_data = _current_login_user(authorization)
    try:
        response = httpx.get(
            f"{BUSINESS_SERVICE_URL}/api/orders",
            params={"customerId": current_user_data["customer_id"]},
            headers={"Authorization": authorization},
            timeout=5.0,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=_response_detail(exc.response)) from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"业务订单服务不可用：{exc}") from exc


@app.post("/api/staff/tickets/{ticket_no}/reply/draft")
def draft_staff_ticket_reply(
    ticket_no: str,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict:
    """根据坐席填写的处理结果生成客户安全话术草稿，但不自动发送给客户。"""
    _require_staff_user(authorization)
    ticket = _get_staff_ticket(ticket_no, authorization)
    context = _find_ticket_chat_context(ticket_no, ticket)
    close_reason = str(payload.get("close_reason") or payload.get("processing_result") or "").strip()
    draft_message = _build_staff_reply_draft(ticket, close_reason)
    return {
        "ticket_no": ticket_no,
        "session_id": context["session"]["session_id"],
        "draft_message": draft_message,
    }


@app.post("/api/staff/tickets/{ticket_no}/reply/send")
def send_staff_ticket_reply(
    ticket_no: str,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict:
    """保存坐席确认后的客户可见回复，客户侧会话历史刷新后即可看到。"""
    staff_user = _require_staff_user(authorization)
    ticket = _get_staff_ticket(ticket_no, authorization)
    context = _find_ticket_chat_context(ticket_no, ticket)
    message = str(payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="发送内容不能为空")

    session_id = context["session"]["session_id"]
    saved_message = chat_messages.save(
        session_no=session_id,
        sender_type="staff",
        sender_id=str(staff_user.get("user_id")),
        content=message,
        extra_data={
            "ticket_no": ticket_no,
            "customer_visible": True,
            "message_source": "staff_confirmed_reply",
            "staff_user": {
                "user_id": staff_user.get("user_id"),
                "display_name": staff_user.get("display_name"),
            },
        },
    )
    # 坐席确认发送后，会话状态进入人工已回复，客户侧列表能看到进度变化。
    chat_sessions.update_status(session_id, "STAFF_REPLIED")
    return {"status": "success", "session_id": session_id, "message": saved_message}


@app.get("/api/staff/handoff/sessions")
def list_staff_handoff_sessions(
    limit: int = 50,
    authorization: str | None = Header(default=None),
) -> list[dict]:
    """查询人工接管会话队列，包含待接入会话和当前坐席已接入会话。"""
    staff_user = _require_staff_user(authorization)
    sessions = chat_sessions.list_handoff_sessions(str(staff_user.get("user_id")), limit)
    return [_handoff_session_payload(session) for session in sessions]


@app.get("/api/staff/handoff/sessions/{session_id}")
def get_staff_handoff_session(session_id: str, authorization: str | None = Header(default=None)) -> dict:
    """坐席查看人工会话详情和完整消息历史。"""
    staff_user = _require_staff_user(authorization)
    session = _get_staff_visible_handoff_session(session_id, staff_user)
    return {
        "session": _handoff_session_payload(session),
        "messages": chat_messages.list_by_session(session_id),
    }


@app.post("/api/staff/handoff/sessions/{session_id}/accept")
def accept_staff_handoff_session(session_id: str, authorization: str | None = Header(default=None)) -> dict:
    """坐席接入待处理人工会话，接入后客户消息不再由 Agent 自动回复。"""
    staff_user = _require_staff_user(authorization)
    accepted = chat_sessions.accept_handoff(
        session_id,
        str(staff_user.get("user_id")),
        str(staff_user.get("display_name") or "客服坐席"),
    )
    if not accepted:
        raise HTTPException(status_code=409, detail="该会话已被其他坐席接入或不在待接入状态")
    chat_messages.save(
        session_no=session_id,
        sender_type="system",
        sender_id="handoff",
        content=f"{staff_user.get('display_name') or '客服坐席'} 已接入人工服务。",
        extra_data={"message_source": "handoff_accepted", "customer_visible": True},
    )
    return {"status": "success", "session": _handoff_session_payload(accepted)}


@app.post("/api/staff/handoff/sessions/{session_id}/reply")
def send_staff_handoff_reply(
    session_id: str,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict:
    """坐席在人工会话中直接回复客户，不依赖业务工单。"""
    staff_user = _require_staff_user(authorization)
    session = _get_staff_owned_handoff_session(session_id, staff_user)
    message = str(payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="发送内容不能为空")
    saved_message = chat_messages.save(
        session_no=session["session_id"],
        sender_type="staff",
        sender_id=str(staff_user.get("user_id")),
        content=message,
        extra_data={
            "customer_visible": True,
            "message_source": "manual_handoff_reply",
            "staff_user": {
                "user_id": staff_user.get("user_id"),
                "display_name": staff_user.get("display_name"),
            },
        },
    )
    chat_sessions.update_status(session_id, "HUMAN_ACTIVE")
    return {"status": "success", "session_id": session_id, "message": saved_message}


@app.post("/api/staff/handoff/sessions/{session_id}/close")
def close_staff_handoff_session(
    session_id: str,
    payload: dict[str, Any] | None = None,
    authorization: str | None = Header(default=None),
) -> dict:
    """坐席结束人工接管，可选择回到 AI 协助或直接关闭会话。"""
    staff_user = _require_staff_user(authorization)
    target_status = str((payload or {}).get("status") or "HUMAN_CLOSED").strip() or "HUMAN_CLOSED"
    if target_status not in {"HUMAN_CLOSED", "AI_ONLY"}:
        raise HTTPException(status_code=400, detail="结束状态只能是 HUMAN_CLOSED 或 AI_ONLY")
    closed = chat_sessions.close_handoff(session_id, str(staff_user.get("user_id")), target_status)
    if not closed:
        raise HTTPException(status_code=403, detail="只能结束自己已接入的人工会话")
    reason = str((payload or {}).get("message") or "").strip()
    content = reason or "人工服务已结束，后续可继续由智能助手协助。"
    chat_messages.save(
        session_no=session_id,
        sender_type="system",
        sender_id="handoff",
        content=content,
        extra_data={"message_source": "handoff_closed", "customer_visible": True, "target_status": target_status},
    )
    return {"status": "success", "session": _handoff_session_payload(closed)}


def _prepare_handoff_response(session_id: str) -> dict[str, Any]:
    """创建人工接管请求，并根据工作时间和坐席容量生成客户可见话术。"""
    availability = _load_human_availability()
    if not availability["in_service_time"]:
        chat_sessions.request_handoff(session_id, "off_hours")
        return {
            "status": "queued",
            "reason": "off_hours",
            "service_status": "已记录人工请求，等待工作时间处理",
            "message": (
                f"当前人工客服不在服务时间内，已为您记录人工服务请求。"
                f"人工服务时间为 {HUMAN_SERVICE_START}-{HUMAN_SERVICE_END}，工作人员上线后会优先处理。"
                "等待期间仍可继续使用智能助手咨询其他问题，也可以选择“人工客服”补充资料。"
            ),
            "availability": availability,
        }

    chat_sessions.request_handoff(session_id, "human_requested")
    if availability["available_staff_count"] <= 0:
        return {
            "status": "queued",
            "reason": "busy",
            "service_status": "人工客服繁忙，已进入排队",
            "message": (
                "当前人工客服较忙，已为您进入人工排队。请您稍等，工作人员空闲后会接入处理。"
                "等待期间仍可继续使用智能助手咨询其他问题，也可以选择“人工客服”补充资料。"
            ),
            "availability": availability,
        }

    return {
        "status": "waiting",
        "reason": "available",
        "service_status": "等待人工客服接入",
        "message": "已为您提交人工请求，请稍候，工作人员会继续跟进本次会话。等待期间仍可继续使用智能助手咨询其他问题。",
        "availability": availability,
    }


def _save_manual_handoff_message(session_id: str, payload: AgentReplyRequest, session: dict[str, Any]) -> dict[str, Any]:
    """保存客户发给人工的补充消息，避免人工通道劫持整个 AI 会话。"""
    active = session.get("status") == "HUMAN_ACTIVE"
    _ensure_handoff_exists(session_id, session, "manual_message")
    chat_messages.save(
        session_no=session_id,
        sender_type="customer",
        sender_id=str(payload.customer_id) if payload.customer_id else None,
        content=payload.message,
        extra_data={"route_target": "human", "message_source": "manual_handoff_customer_message"},
    )
    message = "您的补充内容已发送给当前人工客服。" if active else "您的补充内容已记录到人工服务请求中，客服接入后会一并查看。"
    return _manual_session_ack(session_id, session, message)


def _ensure_handoff_exists(session_id: str, session: dict[str, Any], reason: str) -> None:
    """确保存在人工请求；已有挂起或接入中的人工服务时不重复创建。"""
    if session.get("status") in {"HUMAN_PENDING", "HUMAN_ACTIVE"}:
        return
    chat_sessions.request_handoff(session_id, reason)


def _load_human_availability() -> dict[str, Any]:
    """聚合人工服务时间和坐席负载，只返回 Agent 决策所需的安全摘要。"""
    in_service_time = _is_human_service_time()
    staff_members = _load_staff_members_internal() if in_service_time else []
    available_staff = []
    for staff in staff_members:
        staff_id = str(staff.get("userId"))
        active_tickets = int(staff.get("activeTickets") or 0)
        active_handoffs = chat_sessions.count_active_handoff_by_staff(staff_id)
        max_active = int(staff.get("maxActiveTickets") or 0)
        if staff.get("online") and staff.get("acceptingTickets") and active_tickets + active_handoffs < max_active:
            available_staff.append(staff)
    return {
        "in_service_time": in_service_time,
        "service_start": HUMAN_SERVICE_START,
        "service_end": HUMAN_SERVICE_END,
        "staff_count": len(staff_members),
        "available_staff_count": len(available_staff),
    }


def _load_staff_members_internal() -> list[dict[str, Any]]:
    """通过 Java 内部接口读取坐席聚合状态，避免客户 Token 越权访问坐席数据。"""
    try:
        response = httpx.get(
            f"{BUSINESS_SERVICE_URL}/api/internal/staff/availability",
            headers={"X-Agent-Internal-Secret": AGENT_INTERNAL_SECRET},
            timeout=5.0,
        )
        response.raise_for_status()
        data = response.json()
        members = data.get("members") if isinstance(data, dict) else data
        return members if isinstance(members, list) else []
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError):
        # 坐席状态服务不可用时按繁忙排队处理，避免误导客户已经有人接入。
        return []


def _is_human_service_time() -> bool:
    """判断当前是否处于人工服务时间，支持跨午夜时间段。"""
    now_minutes = _time_to_minutes(datetime.now().strftime("%H:%M"))
    start_minutes = _time_to_minutes(HUMAN_SERVICE_START)
    end_minutes = _time_to_minutes(HUMAN_SERVICE_END)
    if start_minutes <= end_minutes:
        return start_minutes <= now_minutes < end_minutes
    return now_minutes >= start_minutes or now_minutes < end_minutes


def _time_to_minutes(value: str) -> int:
    """把 HH:mm 配置转换为分钟，非法配置降级为 0 点。"""
    try:
        hour, minute = value.split(":", 1)
        return int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return 0


def _manual_session_ack(session_id: str, session: dict[str, Any], message: str | None = None) -> dict[str, Any]:
    """人工队列中的客户补充消息只返回轻量确认，不再触发 AI 自动答复。"""
    active = session.get("status") == "HUMAN_ACTIVE"
    message = message or ("您的补充内容已发送给当前人工客服。" if active else "您的补充内容已记录到人工排队会话中，客服接入后会一并查看。")
    return {
        "session_id": session_id,
        "answer": message,
        "customer_message": message,
        "internal_suggestion": None,
        "decision_type": "human_takeover",
        "service_status": "人工客服处理中" if active else "人工请求已挂起",
        "auto_send": False,
        "need_human": True,
        "analysis": {
            "intent": "consult",
            "user_goal": "human_request",
            "emotion": "normal",
            "order_related": False,
            "order_no": [],
            "product_name": None,
            "need_order_query": False,
            "need_ticket": False,
            "need_human": True,
            "priority": "medium",
            "confidence": 1.0,
            "summary": "人工会话补充消息",
            "risk_reasons": ["manual_handoff_active" if active else "manual_handoff_pending"],
            "action_type": None,
            "action_slots": {},
            "missing_slots": [],
            "next_action": "transfer_human",
        },
        "citations": [],
        "tool_results": [],
        "ticket_result": None,
        "risk_reasons": ["manual_handoff_active" if active else "manual_handoff_pending"],
        "pending_action_request": None,
    }


def _handoff_session_payload(session: dict[str, Any]) -> dict[str, Any]:
    """整理人工会话返回字段，避免坐席端依赖数据库内部列名。"""
    return {
        "session_id": session["session_id"],
        "customer_id": session["customer_id"],
        "status": session["status"],
        "title": session.get("title"),
        "intent": session.get("intent"),
        "emotion": session.get("emotion"),
        "priority": session.get("priority"),
        "ai_summary": session.get("ai_summary"),
        "handoff_reason": session.get("handoff_reason"),
        "human_requested_at": session.get("human_requested_at"),
        "human_assigned_staff_id": session.get("human_assigned_staff_id"),
        "human_assigned_staff_name": session.get("human_assigned_staff_name"),
        "human_accepted_at": session.get("human_accepted_at"),
        "updated_at": session.get("updated_at"),
    }


def _get_staff_visible_handoff_session(session_id: str, staff_user: dict[str, Any]) -> dict[str, Any]:
    """校验坐席只能查看待接入或自己已接入的人工会话。"""
    session = chat_sessions.get_by_session_no(session_id)
    staff_id = str(staff_user.get("user_id"))
    if not session or session.get("deleted_at"):
        raise HTTPException(status_code=404, detail="人工会话不存在")
    if session.get("status") == "HUMAN_PENDING":
        return session
    if session.get("status") == "HUMAN_ACTIVE" and session.get("human_assigned_staff_id") == staff_id:
        return session
    raise HTTPException(status_code=403, detail="无权访问该人工会话")


def _get_staff_owned_handoff_session(session_id: str, staff_user: dict[str, Any]) -> dict[str, Any]:
    """校验人工回复必须由当前接入坐席发送。"""
    session = chat_sessions.get_by_session_no(session_id)
    staff_id = str(staff_user.get("user_id"))
    if (
        not session
        or session.get("status") != "HUMAN_ACTIVE"
        or session.get("human_assigned_staff_id") != staff_id
        or session.get("deleted_at")
    ):
        raise HTTPException(status_code=403, detail="请先接入该人工会话")
    return session


def _current_login_user(authorization: str | None) -> dict:
    """把 Token 交给 Java 业务系统校验，Agent 不自行维护用户登录状态。"""
    if not authorization:
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        response = httpx.get(
            f"{BUSINESS_SERVICE_URL}/api/auth/current-user",
            headers={"Authorization": authorization},
            timeout=5.0,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=_response_detail(exc.response)) from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"业务认证服务不可用：{exc}") from exc


def _require_staff_user(authorization: str | None) -> dict:
    """校验当前用户是否为客服坐席，保护坐席回复草稿和发送接口。"""
    user = _current_login_user(authorization)
    if user.get("role") != "staff":
        raise HTTPException(status_code=403, detail="仅客服坐席可操作")
    return user


def _get_staff_ticket(ticket_no: str, authorization: str | None) -> dict:
    """代理读取坐席视角工单详情，用于生成客户回复草稿时带入最新状态。"""
    try:
        response = httpx.get(
            f"{BUSINESS_SERVICE_URL}/api/staff/tickets/{ticket_no}",
            headers={"Authorization": authorization},
            timeout=5.0,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=_response_detail(exc.response)) from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"业务工单服务不可用：{exc}") from exc


def _find_ticket_chat_context(ticket_no: str, ticket: dict | None = None) -> dict:
    """优先按工单 externalSessionNo 定位会话，旧数据再按消息扩展字段兼容查找。"""
    external_session_no = (ticket or {}).get("externalSessionNo")
    if external_session_no:
        session = chat_sessions.get_by_session_no(external_session_no)
        if session:
            # 新工单已强关联 Python 会话编号，坐席回复无需再扫描历史消息扩展字段。
            return {
                "session": session,
                "message": None,
                "ticket_result": {"status": "success", "data": ticket or {}},
            }

    context = chat_messages.find_ticket_context(ticket_no)
    if not context:
        raise HTTPException(status_code=404, detail="未找到该工单关联的客户会话")
    return context


def _build_staff_reply_draft(ticket: dict, close_reason: str) -> str:
    """根据工单状态和坐席处理结果生成客户可见草稿，避免暴露内部风控和工具细节。"""
    ticket_no = ticket.get("ticketNo")
    status = ticket.get("status")
    reason = close_reason or "您的问题已由客服处理完成。"
    if status == "CLOSED":
        return (
            f"您好，您的工单 {ticket_no} 已处理完成。\n\n"
            f"处理结果：{reason}\n\n"
            "如您对处理结果仍有疑问，可以继续在当前会话中反馈，我们会继续为您跟进。"
        )
    return (
        f"您好，您的工单 {ticket_no} 已有新的处理进展。\n\n"
        f"处理说明：{reason}\n\n"
        "我们会继续关注该问题的后续进度。"
    )


def _bearer_token(authorization: str | None) -> str | None:
    """从 Authorization 请求头中提取 Bearer Token，供工具转发给 Java 业务接口。"""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization.removeprefix("Bearer ").strip()


def _response_detail(response: httpx.Response) -> Any:
    """尽量保留 Java 服务返回的错误信息，便于前端和日志定位登录失败原因。"""
    try:
        return response.json()
    except ValueError:
        return response.text


def _get_or_create_session(payload: AgentReplyRequest) -> dict:
    """按请求中的会话编号续接会话；没有会话编号时自动创建新会话。"""
    if payload.session_id:
        # 续接历史会话必须校验归属，避免用户伪造 session_id 向他人会话追加消息。
        existing = chat_sessions.get_by_session_no_for_customer(payload.session_id, payload.customer_id)
        if existing:
            return existing
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return chat_sessions.create(payload.customer_id, payload.message)


def _latest_pending_action_request(session_id: str) -> dict[str, Any] | None:
    """读取同一会话最近未完成的业务动作 pending，供下一轮用户补槽位。"""
    messages = chat_messages.list_by_session(session_id)
    for message in reversed(messages):
        if message.get("sender_type") != "ai":
            continue
        pending = (message.get("extra_data") or {}).get("pending_action_request")
        if not pending or pending.get("completed") or pending.get("status") in {"completed", "cancelled"}:
            continue
        return pending
    return None


def _resolve_session_status(agent_result: dict) -> str:
    """根据 Agent 回复结果映射会话状态，保持前端列表状态可读。"""
    if agent_result.get("handoff_result"):
        return "HUMAN_PENDING"
    ticket_result = agent_result.get("ticket_result") or {}
    if ticket_result.get("status") == "success":
        return "CREATED_TICKET"
    decision_type = agent_result.get("decision_type")
    if decision_type == "human_takeover":
        return "HUMAN_PENDING"
    if decision_type == "review_required":
        return "AI_REVIEW"
    if decision_type == "auto_reply":
        return "AI_REPLIED"
    if agent_result.get("need_human"):
        return "HUMAN_PENDING"
    if agent_result.get("auto_send"):
        return "AI_ONLY"
    return "AI_ONLY"
