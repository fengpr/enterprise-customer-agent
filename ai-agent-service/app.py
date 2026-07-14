import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from agents.customer_service_agent import CustomerServiceAgent
from rag.evaluate import evaluate as evaluate_rag
from repositories.chat_repository import ChatMessageRepository, ChatSessionRepository
from repositories.evaluation_repository import EvaluationRepository
from schemas.intent_schema import AgentExecutionJob, AgentReplyRequest, AnalyzeRequest, ToolCallRequest
from services.agent_execution_queue import AgentExecutionQueue
from services.agent_execution_service import AgentExecutionAccessDenied, AgentExecutionService
from services.auth_identity_cache import AuthIdentityCache
from services.resilient_client import ResilienceError, ResilientClient
from services.runtime_protection import admission_controller, metrics
from services.observability import HTTP_LATENCY, HTTP_REQUESTS, set_request_context
from services.observability import current_context, tracer
from services.stream_event_service import StreamEventService
from tools.order_tools import OrderTools
from tools.ticket_tools import TicketTools

app = FastAPI(title="Enterprise Customer Agent Service", version="0.1.0")
agent = CustomerServiceAgent()
order_tools = OrderTools()
ticket_tools = TicketTools()
chat_sessions = ChatSessionRepository()
chat_messages = ChatMessageRepository(chat_sessions)
evaluation_repository = EvaluationRepository()
agent_execution_service = AgentExecutionService(
    agent=agent,
    chat_sessions=chat_sessions,
    chat_messages=chat_messages,
    evaluation_repository=evaluation_repository,
)
agent_execution_queue = AgentExecutionQueue()
stream_event_service = StreamEventService()
business_client = ResilientClient(downstream="java_business")
identity_cache = AuthIdentityCache()
BUSINESS_SERVICE_URL = os.getenv("BUSINESS_SERVICE_URL", "http://localhost:8081")
AGENT_INTERNAL_SECRET = os.getenv("AGENT_INTERNAL_SECRET", "enterprise-customer-agent-demo-internal-secret")
HUMAN_SERVICE_START = os.getenv("HUMAN_SERVICE_START", "09:00")
HUMAN_SERVICE_END = os.getenv("HUMAN_SERVICE_END", "18:00")


@app.on_event("startup")
def startup_checks() -> None:
    """服务启动时只检查外部依赖状态，禁止在线 API 进程承载评测 Worker。"""
    agent.rag.check_startup()


@app.middleware("http")
async def observe_request(request: Request, call_next):
    """为每个请求注入 Trace ID 并记录延迟，方便跨 API、Worker 与下游服务排障。"""
    request_id, trace_id = set_request_context(request.headers.get("X-Request-ID"), request.headers.get("X-Trace-ID"))
    started = time.perf_counter()
    try:
        with tracer.start_as_current_span("agent.api.request") as span:
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("url.path", request.url.path)
            span.set_attribute("agent.request_id", request_id)
            response = await call_next(request)
    except Exception:
        metrics.observe(request.url.path, 500, (time.perf_counter() - started) * 1000)
        HTTP_REQUESTS.labels(request.url.path, request.method, "500").inc()
        raise
    metrics.observe(request.url.path, response.status_code, (time.perf_counter() - started) * 1000)
    HTTP_REQUESTS.labels(request.url.path, request.method, str(response.status_code)).inc()
    HTTP_LATENCY.labels(request.url.path, request.method).observe(time.perf_counter() - started)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Trace-ID"] = trace_id
    return response


@app.get("/health")
def health() -> dict:
    """提供服务存活检查，便于前端、网关或部署平台判断 Agent 服务是否可用。"""
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> PlainTextResponse:
    """暴露 Prometheus 抓取端点，避免监控采集依赖业务接口。"""
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/agent/status")
def status() -> dict:
    """返回 Agent 和 LLM 配置状态，用于排查前端无输出问题。"""
    return {
        "status": "ok",
        "llm": agent.llm_status(),
        "queue": {
            "enabled": agent_execution_queue.enabled,
            "active_worker": agent_execution_queue.has_active_worker() if agent_execution_queue.enabled else False,
        },
    }


@app.get("/api/staff/system/monitor")
def staff_system_monitor(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """返回坐席端轻量系统监控快照；局部指标读取失败不能影响页面整体可用性。"""
    _require_staff_user(authorization)
    queue_snapshot = agent_execution_queue.snapshot()
    metric_snapshot = _system_metric_snapshot()
    return {
        "agent_status": {
            "status": "ok",
            "llm": agent.llm_status(),
        },
        "queue": {
            "available": queue_snapshot.get("available", False),
            "enabled": queue_snapshot.get("enabled", False),
            "stream_depth": queue_snapshot.get("stream_depth"),
            "pending": queue_snapshot.get("pending"),
            "running": queue_snapshot.get("running"),
            "retrying": queue_snapshot.get("retrying"),
            "error": queue_snapshot.get("error"),
        },
        "worker": {
            "active": queue_snapshot.get("active_worker", False),
        },
        "dlq": {
            "count": queue_snapshot.get("dead_letter"),
        },
        "llm": metric_snapshot["llm"],
        "cache": metric_snapshot["cache"],
        "degraded": metric_snapshot["degraded"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/auth/login")
def login(payload: dict[str, Any]) -> dict:
    """代理 Java 业务系统登录接口，让前端只需要访问 Agent 服务。"""
    try:
        response = business_client.request_sync("POST", f"{BUSINESS_SERVICE_URL}/api/auth/login", json=payload)
        return response.json()
    except ResilienceError as exc:
        raise HTTPException(status_code=exc.status_code or 503, detail=exc.safe_message) from exc


@app.get("/api/auth/current-user")
def current_user(authorization: str | None = Header(default=None)) -> dict:
    """通过 Java 业务系统校验 Token 并返回当前登录用户。"""
    return _current_login_user(authorization)


@app.post("/api/agent/analyze")
def analyze(payload: AnalyzeRequest) -> dict:
    """对用户输入做结构化意图识别，供客服工作台展示 AI 分析结果。"""
    return agent.analyze(payload.message).model_dump()


def _execute_reply(payload: AgentReplyRequest, authorization: str | None = None) -> dict:
    """兼容内部调用的执行入口；核心业务逻辑已迁移到 AgentExecutionService。"""
    current_user_data = _current_login_user(authorization)
    payload = payload.model_copy(
        update={
            "customer_id": current_user_data["customer_id"],
            "auth_token": _bearer_token(authorization),
        }
    )
    try:
        return agent_execution_service.execute(payload)
    except AgentExecutionAccessDenied as exc:
        # 保持历史接口的会话归属鉴权语义。
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _overload_response() -> dict[str, Any]:
    """在并发槽位耗尽时返回客户安全话术，不等待模型排队造成请求雪崩。"""
    metrics.mark_degraded()
    return {
        "answer": "当前咨询量较大，您的问题已进入人工客服处理队列，请稍后查看处理进度。",
        "customer_message": "当前咨询量较大，您的问题已进入人工客服处理队列，请稍后查看处理进度。",
        "decision_type": "human_takeover",
        "service_status": "排队等待人工处理",
        "auto_send": False,
        "need_human": True,
        "degraded": True,
        "retry_after": int(os.getenv("AGENT_OVERLOAD_RETRY_AFTER_SECONDS", "10")),
    }


def _worker_unavailable_response() -> dict[str, Any]:
    """后台 Worker 未就绪时返回明确终态，避免客户侧长时间停留在排队中。"""
    metrics.mark_degraded()
    return {
        "answer": "当前智能客服服务暂时不可用，请稍后重试，或转人工客服处理。",
        "customer_message": "当前智能客服服务暂时不可用，请稍后重试，或转人工客服处理。",
        "decision_type": "human_takeover",
        "service_status": "智能客服暂不可用",
        "auto_send": False,
        "need_human": True,
        "degraded": True,
        "retry_after": int(os.getenv("AGENT_WORKER_RETRY_AFTER_SECONDS", "10")),
        "error_code": "AGENT_WORKER_UNAVAILABLE",
    }


@app.post("/api/agent/reply", response_model=None)
def reply(payload: AgentReplyRequest, request: Request, authorization: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict | JSONResponse:
    """同步兼容入口只创建/复用可靠任务并短等待，绝不在 API 进程直接执行 Agent。"""
    current_user = _current_login_user(authorization)
    if not agent_execution_queue.enabled:
        return JSONResponse(status_code=503, content=_overload_response() | {"status": "degraded", "queued": False, "error_code": "QUEUE_UNAVAILABLE"})
    if not agent_execution_queue.has_active_worker():
        return JSONResponse(status_code=503, content=_worker_unavailable_response() | {"status": "degraded", "queued": False})
    execution_payload = payload.model_copy(update={"customer_id": current_user["customer_id"], "auth_token": _bearer_token(authorization)})
    request_id = _enqueue_execution_job(execution_payload, authorization, idempotency_key, "sync")
    deadline = time.monotonic() + float(os.getenv("AGENT_SYNC_WAIT_SECONDS", "3"))
    while time.monotonic() < deadline:
        state = agent_execution_queue.get(request_id) or {}
        if state.get("status") == "SUCCESS":
            result = dict(state.get("result") or {})
            result.setdefault("request_id", request_id)
            result.setdefault("execution_status", "success")
            return result
        if state.get("status") in {"DEGRADED", "FAILED", "DEAD_LETTER"}:
            return JSONResponse(status_code=202, content=_overload_response() | {"request_id": request_id, "status": "degraded", "queued": False, "error_code": state.get("error_code", "AGENT_UPSTREAM_UNAVAILABLE")})
        time.sleep(0.05)
    return JSONResponse(status_code=202, content={"request_id": request_id, "status": "queued", "queued": True, "degraded": False, "retry_after": int(os.getenv("AGENT_QUEUE_RETRY_AFTER_SECONDS", "3")), "customer_message": "您的问题正在为您处理，请稍后查询处理进度。", "service_status": "排队处理中"})


@app.post("/api/agent/reply/stream/legacy", include_in_schema=False)
async def reply_stream(payload: AgentReplyRequest, request: Request, authorization: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> StreamingResponse:
    """SSE 回复接口：立即建立连接，在线 Agent 在受控执行槽位中运行。"""
    import asyncio
    import json

    subject = _request_subject(request, authorization)
    # 在进入队列前固定可信身份，Worker 无需也不应依赖 FastAPI 的鉴权函数。
    current_user_data = _current_login_user(authorization)
    execution_payload = payload.model_copy(
        update={
            "customer_id": current_user_data["customer_id"],
            "auth_token": _bearer_token(authorization),
        }
    )

    async def event_stream():
        """分阶段推送状态；当前模型不支持 token stream 时仍可避免 HTTP 长时间无响应。"""
        yield "event: accepted\ndata: {\"status\": \"accepted\"}\n\n"
        if agent_execution_queue.enabled:
            # 生产模式由独立 Agent Worker 执行模型和工具调用，API Pod 只保持 SSE 连接。
            request_id = _enqueue_execution_job(execution_payload, authorization, idempotency_key, "sse")
            yield f"event: queued\ndata: {json.dumps({'request_id': request_id}, ensure_ascii=False)}\n\n"
            deadline = time.monotonic() + float(os.getenv("AGENT_QUEUE_MAX_WAIT_SECONDS", "30"))
            while time.monotonic() < deadline:
                state = agent_execution_queue.get(request_id) or {}
                if state.get("status") == "SUCCESS":
                    yield f"event: completed\ndata: {json.dumps(state['result'], ensure_ascii=False)}\n\n"
                    return
                if state.get("status") in {"DEGRADED", "FAILED", "DEAD_LETTER"}:
                    yield f"event: degraded\ndata: {json.dumps(_overload_response() | {'request_id': request_id}, ensure_ascii=False)}\n\n"
                    return
                await asyncio.sleep(0.25)
            yield f"event: degraded\ndata: {json.dumps(_overload_response() | {'request_id': request_id}, ensure_ascii=False)}\n\n"
            return
        if not admission_controller.try_acquire(subject):
            yield f"event: degraded\ndata: {json.dumps(_overload_response(), ensure_ascii=False)}\n\n"
            return
        try:
            yield "event: generating\ndata: {\"status\": \"generating\"}\n\n"
            result = await asyncio.to_thread(agent_execution_service.execute, execution_payload)
            yield f"event: completed\ndata: {json.dumps(result, ensure_ascii=False)}\n\n"
        except Exception as exc:
            # 外部模型或业务工具异常不向客户暴露内部堆栈。
            metrics.mark_degraded()
            payload_data = _overload_response() | {"error_code": "AGENT_UPSTREAM_UNAVAILABLE"}
            yield f"event: degraded\ndata: {json.dumps(payload_data, ensure_ascii=False)}\n\n"
        finally:
            admission_controller.release(subject)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/agent/reply/stream")
async def reply_stream_v2(payload: AgentReplyRequest, request: Request, authorization: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> StreamingResponse:
    """建立可重放的 SSE 订阅；API 只转发事件，不在本进程执行模型。"""
    import asyncio

    current_user = _current_login_user(authorization)
    if not agent_execution_queue.enabled:
        async def unavailable():
            event = stream_event_service.publish("unavailable", "degraded", _overload_response())
            yield stream_event_service.to_sse(event)
        return StreamingResponse(unavailable(), media_type="text/event-stream")
    if not agent_execution_queue.has_active_worker():
        async def worker_unavailable():
            event = stream_event_service.publish("unavailable", "degraded", _worker_unavailable_response())
            yield stream_event_service.to_sse(event)
        return StreamingResponse(worker_unavailable(), media_type="text/event-stream")

    execution_payload = payload.model_copy(update={"customer_id": current_user["customer_id"], "auth_token": _bearer_token(authorization)})
    request_id = _enqueue_execution_job(execution_payload, authorization, idempotency_key, "sse")
    last_event_id = request.headers.get("Last-Event-ID")

    async def event_stream():
        """先补发 Last-Event-ID 之后的事件，再持续读取 Worker 产生的 token。"""
        if not last_event_id:
            accepted = stream_event_service.publish(request_id, "accepted", {"status": "accepted"})
            queued = stream_event_service.publish(request_id, "queued", {"status": "queued"})
            yield stream_event_service.to_sse(accepted)
            yield stream_event_service.to_sse(queued)
            cursor = queued["event_id"]
        else:
            cursor = last_event_id
        deadline = time.monotonic() + float(os.getenv("AGENT_QUEUE_MAX_WAIT_SECONDS", "30"))
        next_keepalive = time.monotonic() + float(os.getenv("AGENT_SSE_KEEPALIVE_SECONDS", "5"))
        while time.monotonic() < deadline:
            for event in stream_event_service.replay(request_id, cursor):
                cursor = event["event_id"]
                yield stream_event_service.to_sse(event)
                if event["event_type"] in {"completed", "degraded", "error"}:
                    return
            state = agent_execution_queue.get(request_id) or {}
            if state.get("status") in {"SUCCESS", "DEGRADED", "FAILED", "DEAD_LETTER"}:
                result = state.get("result") or {}
                event_type = "completed" if state.get("status") == "SUCCESS" else "degraded"
                event = stream_event_service.publish(request_id, event_type, {"answer": result.get("customer_message") or result.get("answer", ""), "status": state.get("status")})
                yield stream_event_service.to_sse(event)
                return
            # 定期发送 SSE 注释帧，避免浏览器、开发代理或网关把无 token 的检索阶段判为闲置连接。
            if time.monotonic() >= next_keepalive:
                yield ": keepalive\n\n"
                next_keepalive = time.monotonic() + float(os.getenv("AGENT_SSE_KEEPALIVE_SECONDS", "5"))
            await asyncio.sleep(0.15)
        # 单次订阅到期只发布非终态 queued；客户端携带相同幂等键和 Last-Event-ID 自动续传。
        waiting = stream_event_service.publish(request_id, "queued", {
            "status": (agent_execution_queue.get(request_id) or {}).get("status", "PENDING"),
            "retry_after": int(os.getenv("AGENT_QUEUE_RETRY_AFTER_SECONDS", "3")),
            "customer_message": "请求仍在后台处理中，正在继续等待结果。",
        })
        yield stream_event_service.to_sse(waiting)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/agent/replies/{request_id}")
def get_queued_reply(request_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """查询 SSE 断线后的排队结果；仅暴露短 TTL 内的客户可见响应。"""
    result = agent_execution_queue.get(request_id)
    if not result:
        raise HTTPException(status_code=404, detail="请求不存在或已过期")
    if result.get("owner") != _queue_owner(authorization):
        raise HTTPException(status_code=403, detail="无权读取该请求结果")
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


@app.get("/api/staff/rag/evaluation")
def rag_evaluation(
    mode: str = "baseline",
    authorization: str | None = Header(default=None),
) -> dict:
    """运行离线 RAG 质量评测，仅向内部坐席开放，避免客户读取内部评测与失败样本。"""
    _require_staff_user(authorization)
    data_dir = Path(__file__).resolve().parent / "data"
    if mode != "baseline":
        raise HTTPException(status_code=400, detail="真实 Agent 全量评测请通过后台任务接口提交")
    # 基线报告用于页面首屏和快速回归，不会触发模型或业务工具调用。
    return evaluate_rag(
        eval_dir=str(data_dir / "rag_eval"),
        kb_dir=str(data_dir / "kb_sources"),
        generation_mode="baseline",
    )


@app.post("/api/staff/rag/evaluation/jobs")
def create_rag_evaluation_job(payload: dict[str, Any] | None = None, authorization: str | None = Header(default=None)) -> dict:
    """提交全量真实 Agent 评测任务，接口立即返回，前端通过状态接口轮询结果。"""
    _require_staff_user(authorization)
    requested = (payload or {}).get("max_samples")
    try:
        max_samples = int(requested) if requested is not None else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="样本数必须是正整数") from exc
    if max_samples is not None and not 1 <= max_samples <= 500:
        raise HTTPException(status_code=400, detail="样本数必须在 1 到 500 之间")
    return evaluation_repository.create_job("GOLDEN", {"max_samples": max_samples})


@app.get("/api/staff/rag/evaluation/jobs/{job_id}")
def get_rag_evaluation_job(job_id: str, authorization: str | None = Header(default=None)) -> dict:
    """查询评测后台任务状态与完成报告，仅允许内部坐席访问。"""
    _require_staff_user(authorization)
    job = evaluation_repository.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="评测任务不存在或已过期")
    return job


@app.get("/api/staff/evaluation/online/report")
def online_evaluation_report(days: int = 7, limit: int = 50, authorization: str | None = Header(default=None)) -> dict:
    """读取线上采样评测监控报告；该接口不触发任何模型调用。"""
    _require_staff_user(authorization)
    return evaluation_repository.online_report(days=days, limit=limit)


@app.get("/api/staff/evaluation/queue")
def online_evaluation_queue(authorization: str | None = Header(default=None)) -> dict:
    """查询评测队列积压与每日预算消耗，供坐席端监控 Worker 健康度。"""
    _require_staff_user(authorization)
    return evaluation_repository.queue_status()


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
    result = ticket_tools.query_ticket_status(ticket_no, _bearer_token(authorization))
    if result.get("status") == "success":
        return result["data"]
    if result.get("error") == "4xx":
        return {"ticketNo": ticket_no, "status": "状态暂不可同步", "source": "HISTORY_FALLBACK"}
    raise HTTPException(status_code=503, detail="业务工单服务暂时不可用")


@app.get("/api/customer/tickets")
def list_customer_tickets(authorization: str | None = Header(default=None)) -> list[dict]:
    """代理查询当前客户自己的工单列表，供客户侧工单列表和进度面板使用。"""
    _current_login_user(authorization)
    result = ticket_tools.list_customer_tickets(_bearer_token(authorization))
    if result.get("status") == "success":
        return result["data"]
    raise HTTPException(status_code=503, detail="业务工单列表服务暂时不可用")


@app.post("/api/customer/tickets/{ticket_no}/urge")
def urge_customer_ticket(
    ticket_no: str,
    payload: dict[str, Any] | None = None,
    authorization: str | None = Header(default=None),
) -> dict:
    """代理客户催办自己的工单，催办由 Java 按 Token 校验归属并落库。"""
    _current_login_user(authorization)
    key = str((payload or {}).get("idempotency_key") or uuid.uuid4())
    result = ticket_tools.urge_ticket(ticket_no, str((payload or {}).get("reason") or "客户催办处理进度"), _bearer_token(authorization), key)
    if result.get("status") == "success":
        return result["data"]
    raise HTTPException(status_code=503, detail="业务工单催办服务暂时不可用")


@app.get("/api/customer/orders")
def list_customer_orders(authorization: str | None = Header(default=None)) -> list[dict]:
    """代理查询当前客户订单列表，供客户侧选择订单后发起咨询。"""
    current_user_data = _current_login_user(authorization)
    result = order_tools.query_customer_orders(current_user_data["customer_id"], _bearer_token(authorization))
    if result.get("status") in {"success", "empty"}:
        return result.get("data", [])
    raise HTTPException(status_code=503, detail="业务订单服务暂时不可用")


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
        response = business_client.request_sync(
            "GET",
            f"{BUSINESS_SERVICE_URL}/api/internal/staff/availability",
            headers={"X-Agent-Internal-Secret": AGENT_INTERNAL_SECRET},
        )
        data = response.json()
        members = data.get("members") if isinstance(data, dict) else data
        return members if isinstance(members, list) else []
    except (ResilienceError, ValueError):
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
    """优先使用短期身份缓存，未命中时仍交给 Java 业务系统做权威 Token 校验。"""
    if not authorization:
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        def load_identity() -> dict[str, Any]:
            """缓存缺失时请求 Java；Authorization 仅用于本次下游校验，不写入 Redis。"""
            response = business_client.request_sync(
                "GET",
                f"{BUSINESS_SERVICE_URL}/api/auth/current-user",
                headers={"Authorization": authorization},
            )
            return response.json()

        token = _bearer_token(authorization)
        if not token:
            # 非 Bearer 格式仍交给 Java 校验并保持既有 401 行为，不可共享空 Token 缓存键。
            return load_identity()
        return identity_cache.get_or_load(token, load_identity)
    except ResilienceError as exc:
        raise HTTPException(status_code=exc.status_code or 503, detail=exc.safe_message) from exc
    except ValueError as exc:
        # 身份格式不完整时不信任缓存值，避免发生客户或角色越权。
        raise HTTPException(status_code=502, detail="身份服务返回异常") from exc


def _require_staff_user(authorization: str | None) -> dict:
    """校验当前用户是否为客服坐席，保护坐席回复草稿和发送接口。"""
    user = _current_login_user(authorization)
    if user.get("role") != "staff":
        raise HTTPException(status_code=403, detail="仅客服坐席可操作")
    return user


def _get_staff_ticket(ticket_no: str, authorization: str | None) -> dict:
    """代理读取坐席视角工单详情，用于生成客户回复草稿时带入最新状态。"""
    try:
        response = business_client.request_sync(
            "GET",
            f"{BUSINESS_SERVICE_URL}/api/staff/tickets/{ticket_no}",
            headers={"Authorization": authorization},
        )
        return response.json()
    except ResilienceError as exc:
        raise HTTPException(status_code=exc.status_code or 503, detail=exc.safe_message) from exc


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


def _request_subject(request: Request, authorization: str | None) -> str:
    """生成限流主体；优先使用已认证 Token，匿名请求回退到来源 IP。"""
    token = _bearer_token(authorization)
    if token:
        return f"token:{token[:24]}"
    client = request.client.host if request.client else "unknown"
    return f"ip:{client}"


def _queue_owner(authorization: str | None) -> str:
    """保存不可逆的 Token 摘要作为短期队列结果归属校验，不在结果中暴露完整凭证。"""
    import hashlib

    token = _bearer_token(authorization) or "anonymous"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def _enqueue_execution_job(payload: AgentReplyRequest, authorization: str | None, idempotency_key: str | None, route_source: str) -> str:
    """统一创建安全队列任务，使同步接口和 SSE 复用同一幂等与状态协议。"""
    request_id = f"agent-job-{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)
    job = AgentExecutionJob(
        request_id=request_id,
        customer_id=int(payload.customer_id or 0),
        message=payload.message,
        session_id=payload.session_id,
        selected_order_no=payload.selected_order_no,
        selected_ticket_no=payload.selected_ticket_no,
        route_target=payload.route_target,
        idempotency_key=idempotency_key or f"{route_source}:{request_id}",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=int(os.getenv("AGENT_JOB_TTL_SECONDS", "600")))).isoformat(),
        route_source=route_source,
        risk_level="high" if payload.route_target in {"human", "both"} else "normal",
        trace_id=current_context()["trace_id"],
        execution_credential=_queue_execution_credential(int(payload.customer_id or 0), request_id),
    )
    return agent_execution_queue.enqueue(job, _queue_owner(authorization))


def _queue_execution_credential(customer_id: int, request_id: str) -> str:
    """签发短期内部执行凭证；队列不保存客户 Authorization 原始 Token。"""
    import hashlib
    import hmac

    expires_at = int(time.time()) + int(os.getenv("AGENT_JOB_TTL_SECONDS", "600"))
    content = f"{customer_id}:{request_id}:{expires_at}"
    secret = os.getenv("AGENT_EXECUTION_SECRET", AGENT_INTERNAL_SECRET)
    signature = hmac.new(secret.encode("utf-8"), content.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    return f"v1.{expires_at}.{signature}"


def _response_detail(response: httpx.Response) -> Any:
    """尽量保留 Java 服务返回的错误信息，便于前端和日志定位登录失败原因。"""
    try:
        return response.json()
    except ValueError:
        return response.text


def _system_metric_snapshot() -> dict[str, Any]:
    """聚合当前进程 Prometheus 指标，供内部监控页以 JSON 方式展示关键异常与缓存命中率。"""
    try:
        llm_errors = {
            outcome: int(_sum_metric("agent_downstream_requests_total", {"outcome": outcome}, downstream_contains="llm"))
            for outcome in ("timeout", "rate_limit_429", "circuit_open")
        }
        return {
            "llm": llm_errors,
            "cache": {
                "rag": _cache_metric("rag_cache_hit"),
                "order": _cache_metric("order_cache_hit"),
                "ticket": _cache_metric("ticket_cache_hit"),
                "identity": _cache_metric("identity_cache_hit"),
                "session": _cache_metric("session_cache_hit"),
            },
            # 兼容早期 RuntimeMetrics 的降级计数；新 Prometheus Counter 有 reason 标签。
            "degraded": {
                "total": int(metrics.degraded + _sum_metric("agent_degraded_total")),
                "by_reason": _labeled_counter("agent_degraded_total", "reason"),
            },
        }
    except Exception:
        return {
            "llm": {"timeout": None, "rate_limit_429": None, "circuit_open": None},
            "cache": {},
            "degraded": {"total": None, "by_reason": {}},
        }


def _metric_samples(metric_name: str) -> list[Any]:
    """从默认 Registry 读取指定样本；只读指标名与标签，不接触请求体或鉴权信息。"""
    samples: list[Any] = []
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name == metric_name:
                samples.append(sample)
    return samples


def _sum_metric(metric_name: str, labels: dict[str, str] | None = None, downstream_contains: str | None = None) -> float:
    """按标签聚合 Prometheus Counter/Gauge，用于把文本指标转换为页面需要的数字。"""
    total = 0.0
    labels = labels or {}
    for sample in _metric_samples(metric_name):
        sample_labels = sample.labels or {}
        if any(sample_labels.get(key) != value for key, value in labels.items()):
            continue
        if downstream_contains and downstream_contains not in sample_labels.get("downstream", "").lower():
            continue
        total += float(sample.value)
    return total


def _labeled_counter(metric_name: str, label_name: str) -> dict[str, int]:
    """按单个标签拆分 Counter，便于页面显示降级原因分布。"""
    values: dict[str, int] = {}
    for sample in _metric_samples(metric_name):
        label = (sample.labels or {}).get(label_name, "unknown")
        values[label] = values.get(label, 0) + int(sample.value)
    return values


def _cache_metric(cache_name: str) -> dict[str, Any]:
    """计算缓存命中率；没有 hit/miss 分母时返回 null，避免展示伪精确百分比。"""
    hit = int(_sum_metric("agent_cache_operations_total", {"cache": cache_name, "outcome": "hit"}))
    miss = int(_sum_metric("agent_cache_operations_total", {"cache": cache_name, "outcome": "miss"}))
    error = int(_sum_metric("agent_cache_operations_total", {"cache": cache_name, "outcome": "error"}))
    denominator = hit + miss
    return {
        "hit": hit,
        "miss": miss,
        "error": error,
        "hit_rate": round(hit / denominator, 4) if denominator else None,
    }


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
