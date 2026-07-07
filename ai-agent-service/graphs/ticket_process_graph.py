import os
import re
from collections.abc import Callable
from typing import Any, TypedDict

from langchain_core.runnables import Runnable
from langgraph.graph import END, StateGraph

from agents.action_request import ACTION_SLOT_RULES
from rag.knowledge_taxonomy import infer_business_scope
from schemas.intent_schema import Citation, IntentResult


class TicketProcessState(TypedDict, total=False):
    """客服工单处理图的共享状态，贯穿识别、查单、检索、风控和回复节点。"""

    message: str
    session_id: str | None
    customer_id: int | None
    auth_token: str | None
    selected_order_no: str | None
    selected_ticket_no: str | None
    conversation_context: dict[str, Any] | None
    analysis: IntentResult
    citations: list[Citation]
    tool_results: list[dict[str, Any]]
    answer: str
    auto_send: bool
    need_human: bool
    ticket_result: dict[str, Any] | None
    risk_reasons: list[str]
    pending_action_request: dict[str, Any] | None


def build_ticket_process_graph(
    *,
    analyzer_chain: Runnable[str, IntentResult],
    retrieve_knowledge: Callable[[dict[str, Any]], list[Citation]],
    query_order: Callable[[str, str | None], dict[str, Any]],
    query_customer_orders: Callable[[int | str | None, str | None], dict[str, Any]],
    query_order_logistics: Callable[[str, str | None], dict[str, Any]],
    create_ticket: Callable[[dict[str, Any], str | None], dict[str, Any]],
    auto_assign_ticket: Callable[[str], dict[str, Any]],
    list_customer_tickets: Callable[[str | None], dict[str, Any]],
    query_ticket_status: Callable[[str, str | None], dict[str, Any]],
    urge_ticket: Callable[[str, str | None, str | None], dict[str, Any]],
    prepare_action: Callable[[TicketProcessState], TicketProcessState],
    compose_answer: Callable[[TicketProcessState], str],
    log_tool_call: Callable[[str, dict[str, Any], dict[str, Any]], None],
):
    """构建客服处理 LangGraph，将 PRD 主流程显式编排为可追踪节点。"""
    graph = StateGraph(TicketProcessState)

    def analyze_node(state: TicketProcessState) -> TicketProcessState:
        """调用 LangChain 识别链，生成后续节点依赖的结构化分析结果。"""
        analysis = analyzer_chain.invoke(
            {
                "message": state["message"],
                "conversation_context": state.get("conversation_context"),
            }
        )
        return {"analysis": analysis}

    def prepare_action_node(state: TicketProcessState) -> TicketProcessState:
        """合并通用业务动作槽位和 pending 状态，决定后续是否追问、查单或建单。"""
        return prepare_action(state)

    def query_order_node(state: TicketProcessState) -> TicketProcessState:
        """根据订单号或客户 ID 调用业务系统，查询结果进入后续回复依据。"""
        analysis = state["analysis"]
        selected_order_no = state.get("selected_order_no") or _context_value(state, "last_order")
        should_use_selected_order = bool(selected_order_no and analysis.order_related and analysis.user_goal in {"status_query", "action_request"})
        ticket_result = _query_or_urge_ticket_if_needed(state, list_customer_tickets, query_ticket_status, urge_ticket, log_tool_call)
        if ticket_result is not None:
            return {"tool_results": ticket_result}
        should_list_orders_for_slots = analysis.user_goal == "action_request" and analysis.next_action == "collect_slots" and "order_no" in analysis.missing_slots
        if not analysis.need_order_query and not should_use_selected_order:
            if should_list_orders_for_slots and state.get("customer_id") is not None:
                # 动作申请缺订单时查询客户订单列表，追问时给出可选订单，减少客户重复输入成本。
                result = query_customer_orders(state.get("customer_id"), state.get("auth_token"))
                log_tool_call("query_customer_orders", {"customer_id": state.get("customer_id")}, result)
                return {"tool_results": [result]}
            # 非订单相关问题不调用业务系统，避免无意义工具调用扩大影响面。
            return {"tool_results": []}

        tool_results: list[dict[str, Any]] = []
        should_query_logistics = _is_logistics_query(state["message"], analysis.intent)
        order_nos = list(analysis.order_no)
        if should_use_selected_order and selected_order_no not in order_nos:
            # 客户侧选中的订单只作为候选上下文，真实归属仍由 Java 订单接口按 Token 校验。
            order_nos.append(selected_order_no)

        for order_no in order_nos:
            # 用户给出明确订单号时，优先查询订单详情，避免返回过多无关订单。
            result = query_order(order_no, state.get("auth_token"))
            tool_results.append(result)
            log_tool_call("query_order", {"order_no": order_no}, result)
            if should_query_logistics:
                # 物流意图需要继续查询完整轨迹，订单归属由 Java 物流接口再次校验。
                logistics_result = query_order_logistics(order_no, state.get("auth_token"))
                tool_results.append(logistics_result)
                log_tool_call("query_order_logistics", {"order_no": order_no}, logistics_result)

        if not order_nos and state.get("customer_id") is not None:
            # 没有订单号但有客户上下文时，走客户订单列表接口支持“查询我的订单”。
            customer_id = state.get("customer_id")
            result = query_customer_orders(customer_id, state.get("auth_token"))
            tool_results.append(result)
            log_tool_call("query_customer_orders", {"customer_id": customer_id}, result)
            if should_query_logistics and result.get("status") == "success":
                orders = result.get("data") or []
                first_order_no = orders[0].get("orderNo") if orders else None
                if first_order_no:
                    # 客户未提供订单号时，按最近订单查询物流，避免要求客户重复补充信息。
                    logistics_result = query_order_logistics(first_order_no, state.get("auth_token"))
                    tool_results.append(logistics_result)
                    log_tool_call("query_order_logistics", {"order_no": first_order_no}, logistics_result)

        return {"tool_results": tool_results}

    def retrieve_knowledge_node(state: TicketProcessState) -> TicketProcessState:
        """检索已发布知识库片段，为回复生成和风险校验提供依据。"""
        if "model_analyze_failed" in state["analysis"].risk_reasons:
            # 模型识别失败时直接转人工，不再用 RAG 包装成自动回复依据。
            return {"citations": []}
        analysis = state["analysis"]
        if analysis.user_goal in {"human_request", "out_of_scope"}:
            # 明确转人工和非客服越界问题不依赖知识库检索，避免出现知识库缺失模板。
            return {"citations": []}
        business_scope = (
            "return_goods"
            if analysis.user_goal == "policy_consult" and analysis.action_type == "return_goods"
            else infer_business_scope(analysis.intent, analysis.user_goal)
        )
        return {
            "citations": retrieve_knowledge(
                {
                    "query": state["message"],
                    "intent": analysis.intent,
                    "user_goal": analysis.user_goal,
                    "business_scope": business_scope,
                    "conversation_context": state.get("conversation_context"),
                }
            )
        }

    def risk_check_node(state: TicketProcessState) -> TicketProcessState:
        """根据知识库命中和识别风险决定是否允许自动回复。"""
        analysis = state["analysis"].model_copy(deep=True)
        risk_reasons = list(analysis.risk_reasons)

        if "model_analyze_failed" in risk_reasons:
            # 模型识别失败已经明确转人工，不叠加知识库无命中原因。
            analysis.need_human = True
            analysis.need_ticket = True
            return {
                "analysis": analysis,
                "need_human": True,
                "auto_send": False,
                "risk_reasons": risk_reasons,
            }

        if analysis.user_goal == "action_request":
            if analysis.next_action == "cancel_pending":
                # 用户取消上一轮动作是安全终止，不需要知识库、人工或工单。
                analysis.need_human = False
                analysis.need_ticket = False
                return {
                    "analysis": analysis,
                    "need_human": False,
                    "auto_send": True,
                    "risk_reasons": risk_reasons,
                }
            if analysis.next_action == "collect_slots":
                # 信息未补齐时允许自动追问，但禁止创建工单。
                analysis.need_human = False
                analysis.need_ticket = False
                return {
                    "analysis": analysis,
                    "need_human": False,
                    "auto_send": True,
                    "risk_reasons": risk_reasons,
                }
            if _action_requires_order(analysis) and not _has_success_order_detail(state):
                # 订单归属或订单号未通过 Java 校验时不能建单，避免把他人订单写进工单。
                analysis.need_human = False
                analysis.need_ticket = False
                analysis.next_action = "collect_slots"
                analysis.missing_slots = ["order_no"]
                pending = state.get("pending_action_request") or {}
                if pending:
                    pending = dict(pending)
                    pending["status"] = "collecting"
                    pending["next_action"] = "collect_slots"
                    pending["missing_slots"] = ["order_no"]
                if "order_validation_failed" not in risk_reasons:
                    risk_reasons.append("order_validation_failed")
                return {
                    "analysis": analysis,
                    "need_human": False,
                    "auto_send": True,
                    "risk_reasons": risk_reasons,
                    "pending_action_request": pending or state.get("pending_action_request"),
                }

        has_tool_evidence = _has_order_tool_evidence(state)
        return_goods_policy_consult = analysis.user_goal == "policy_consult" and analysis.action_type == "return_goods"
        how_to_consult = analysis.user_goal == "how_to"
        out_of_scope = analysis.user_goal == "out_of_scope"
        human_request = analysis.user_goal == "human_request"
        basic_info_query = analysis.user_goal == "info_query" and analysis.intent in {"consult", "other"}
        high_risk_citations = [citation for citation in state.get("citations", []) if citation.risk_level in {"high", "critical"}]
        if high_risk_citations:
            # 投诉、赔付、法律等高风险知识只能支撑人工建议，不允许自动发送给客户。
            analysis.need_human = True
            if "high_risk_knowledge" not in risk_reasons:
                risk_reasons.append("high_risk_knowledge")
        if human_request:
            analysis.need_human = True
            analysis.need_ticket = False
            if "human_request" not in risk_reasons:
                risk_reasons.append("human_request")
        elif not state.get("citations") and not has_tool_evidence and not return_goods_policy_consult and not how_to_consult and not out_of_scope and not basic_info_query:
            # 知识库缺失只记录风险原因；低风险咨询由回复层澄清或提示边界，不再自动转人工。
            if "no_kb_hit" not in risk_reasons:
                risk_reasons.append("no_kb_hit")
            if analysis.user_goal in {"complaint", "dispute", "action_request"}:
                analysis.need_human = True

        analysis.risk_reasons = risk_reasons
        auto_send = not analysis.need_human and (
            bool(state.get("citations"))
            or has_tool_evidence
            or return_goods_policy_consult
            or how_to_consult
            or out_of_scope
            or basic_info_query
            or "no_kb_hit" in risk_reasons
        )
        return {
            "analysis": analysis,
            "need_human": analysis.need_human,
            "auto_send": auto_send,
            "risk_reasons": risk_reasons,
        }

    def create_ticket_node(state: TicketProcessState) -> TicketProcessState:
        """高风险或需建单场景通过 Java 业务系统创建工单，Python 不直接写工单主表。"""
        analysis = state["analysis"]
        if analysis.user_goal == "human_request":
            # 转人工是当前会话接管请求，不等同于创建售后/投诉等业务工单。
            return {"ticket_result": None}
        if not (analysis.need_ticket or analysis.need_human):
            return {"ticket_result": None}
        if analysis.user_goal == "action_request" and analysis.next_action != "create_ticket":
            # 动作闭环必须等槽位齐全且订单校验完成后才能建单。
            return {"ticket_result": None}

        duplicate = _find_duplicate_ticket(state, list_customer_tickets)
        if duplicate:
            # 同一客户、同一动作、同一订单已有未完成工单时直接复用，避免重复建单。
            tool_results = list(state.get("tool_results", []))
            duplicate_result = {"status": "success", "data": duplicate, "deduplicated": True}
            tool_results.append({"tool_name": "find_duplicate_ticket", **duplicate_result})
            pending = state.get("pending_action_request") or {}
            if pending:
                pending = dict(pending)
                pending["status"] = "completed"
                pending["completed"] = True
            return {"ticket_result": duplicate_result, "tool_results": tool_results, "pending_action_request": pending}

        payload = _build_ticket_payload(state)
        result = create_ticket(payload, state.get("auth_token"))
        log_tool_call("create_ticket", payload, result)

        tool_results = list(state.get("tool_results", []))
        tool_results.append({"tool_name": "create_ticket", **result})

        if result.get("status") == "success":
            ticket_data = result.get("data") or {}
            ticket_no = ticket_data.get("ticketNo")
            if ticket_no and should_auto_assign_ticket(state):
                # 只有低风险且配置允许的工单才由 Agent 触发自动派单；默认进入调度队列。
                try:
                    assign_result = auto_assign_ticket(ticket_no)
                except Exception as exc:
                    # 自动派单失败不能回滚已创建工单，保持 PENDING_ASSIGN 交由调度员处理。
                    assign_result = {"status": "failed", "error": str(exc)}
                log_tool_call("auto_assign_ticket", {"ticket_no": ticket_no}, assign_result)
                tool_results.append({"tool_name": "auto_assign_ticket", **assign_result})
                if assign_result.get("status") == "success":
                    result = assign_result
            pending = state.get("pending_action_request") or {}
            if pending:
                pending = dict(pending)
                pending["status"] = "completed"
                pending["completed"] = True
                pending["ticket_no"] = ticket_no
                return {"ticket_result": result, "tool_results": tool_results, "pending_action_request": pending}

        return {"ticket_result": result, "tool_results": tool_results}

    def generate_reply_node(state: TicketProcessState) -> TicketProcessState:
        """在风险校验之后生成最终候选回复，确保高风险场景只给人工建议。"""
        return {"answer": compose_answer(state)}

    # 节点顺序对应 PRD 主流程，避免 Agent 自由跳步造成不可控工具调用。
    graph.add_node("analyze", analyze_node)
    graph.add_node("prepare_action", prepare_action_node)
    graph.add_node("query_order", query_order_node)
    graph.add_node("retrieve_knowledge", retrieve_knowledge_node)
    graph.add_node("risk_check", risk_check_node)
    graph.add_node("create_ticket", create_ticket_node)
    graph.add_node("generate_reply", generate_reply_node)

    graph.set_entry_point("analyze")
    graph.add_edge("analyze", "prepare_action")
    graph.add_edge("prepare_action", "query_order")
    graph.add_edge("query_order", "retrieve_knowledge")
    graph.add_edge("retrieve_knowledge", "risk_check")
    graph.add_edge("risk_check", "create_ticket")
    graph.add_edge("create_ticket", "generate_reply")
    graph.add_edge("generate_reply", END)

    return graph.compile()


def _build_ticket_payload(state: TicketProcessState) -> dict[str, Any]:
    """按 Java business-service 的工单接口字段组装创建工单请求。"""
    analysis = state["analysis"]
    action_slots = analysis.action_slots or {}
    order_no = action_slots.get("order_no") or (analysis.order_no[0] if analysis.order_no else state.get("selected_order_no"))
    title = analysis.summary or state["message"][:80]
    ticket_type = _resolve_ticket_type(analysis)
    content = _build_ticket_content(state)

    return {
        "title": title[:128],
        "ticketType": ticket_type,
        "priority": analysis.priority,
        "customerId": state.get("customer_id"),
        "orderNo": order_no,
        # Java 旧字段 sessionId 是 Long，保留为空；externalSessionNo 保存 Python 会话编号用于强关联。
        "sessionId": None,
        "externalSessionNo": state.get("session_id"),
        "content": content,
        "aiSummary": content[:500],
        "assignedGroup": _resolve_assigned_group(ticket_type),
        "handlerId": None,
        "source": "AI_AGENT",
    }


def _has_order_tool_evidence(state: TicketProcessState) -> bool:
    """判断订单或物流工具是否返回了可信业务数据，成功查单可作为自动回复依据。"""
    return any(
        item.get("query_type") in {"order_detail", "customer_orders", "order_logistics", "ticket_status", "ticket_urge"}
        and item.get("status") in {"success", "empty"}
        for item in state.get("tool_results", [])
    )


def _query_or_urge_ticket_if_needed(
    state: TicketProcessState,
    list_customer_tickets: Callable[[str | None], dict[str, Any]],
    query_ticket_status: Callable[[str, str | None], dict[str, Any]],
    urge_ticket: Callable[[str, str | None, str | None], dict[str, Any]],
    log_tool_call: Callable[[str, dict[str, Any], dict[str, Any]], None],
) -> list[dict[str, Any]] | None:
    """识别客户查工单进度或催办表达，优先走工单 Tool 而不是创建新工单。"""
    message = state["message"]
    context_ticket_no = _context_value(state, "last_ticket")
    has_selected_ticket = bool(state.get("selected_ticket_no") or context_ticket_no)
    selected_ticket_action = has_selected_ticket and not _is_logistics_query(message, "") and not _is_how_to_query(message) and any(
        word in message for word in ["催", "加急", "进度", "处理", "状态", "太慢", "怎么还没"]
    )
    if not (_is_ticket_status_query(message) or _is_ticket_urge_query(message) or selected_ticket_action):
        return None

    ticket_no = _extract_ticket_no(message) or state.get("selected_ticket_no") or context_ticket_no
    tool_results: list[dict[str, Any]] = []
    if not ticket_no:
        list_result = list_customer_tickets(state.get("auth_token"))
        log_tool_call("list_customer_tickets", {}, list_result)
        tool_results.append({"tool_name": "list_customer_tickets", **list_result, "query_type": "customer_tickets"})
        tickets = list_result.get("data") if list_result.get("status") == "success" else []
        ticket_no = _latest_active_ticket_no(tickets or [])

    if not ticket_no:
        tool_results.append({
            "status": "empty",
            "query_type": "ticket_status",
            "error": "未找到可查询或催办的工单",
        })
        return tool_results

    if _is_ticket_urge_query(message) or (ticket_no and _has_urge_words(message)):
        result = urge_ticket(ticket_no, message, state.get("auth_token"))
        log_tool_call("urge_ticket", {"ticket_no": ticket_no}, result)
        tool_results.append({"tool_name": "urge_ticket", **result})
        return tool_results

    result = query_ticket_status(ticket_no, state.get("auth_token"))
    log_tool_call("query_ticket_status", {"ticket_no": ticket_no}, result)
    tool_results.append({"tool_name": "query_ticket_status", **result})
    return tool_results


def _extract_ticket_no(message: str) -> str | None:
    """从客户消息中提取工单号，支持 T 开头的 Demo 工单编号。"""
    match = re.search(r"T\d{12,24}", message, flags=re.IGNORECASE)
    return match.group(0).upper() if match else None


def _context_value(state: TicketProcessState, key: str) -> str | None:
    """从多轮上下文中读取已压缩实体，用于解析“刚才那个”等指代。"""
    item = (state.get("conversation_context") or {}).get(key) or {}
    value = item.get("value")
    return str(value) if value else None


def _latest_active_ticket_no(tickets: list[dict[str, Any]]) -> str | None:
    """未提供工单号时选择最近一张未关闭工单，符合客户口语里的“这个工单”。"""
    for ticket in tickets:
        if ticket.get("status") != "CLOSED" and ticket.get("ticketNo"):
            return str(ticket.get("ticketNo"))
    return None


def _is_ticket_status_query(message: str) -> bool:
    """识别客户查询工单处理进度的表达。"""
    return (_extract_ticket_no(message) is not None or "工单" in message) and any(
        word in message for word in ["工单", "进度", "处理", "状态", "到哪"]
    )


def _is_ticket_urge_query(message: str) -> bool:
    """识别客户希望催办工单的表达。"""
    return _has_urge_words(message) and ("工单" in message or _extract_ticket_no(message) is not None)


def _has_urge_words(message: str) -> bool:
    """识别催办关键词，供显式工单和上下文工单共用。"""
    urge_words = ["催", "催一下", "催催", "加急", "尽快", "太慢", "怎么还没", "帮我催", "推进"]
    return any(word in message for word in urge_words)


def _has_success_order_detail(state: TicketProcessState) -> bool:
    """判断订单详情是否已通过 Java Token 校验并成功返回。"""
    return any(
        item.get("query_type") == "order_detail" and item.get("status") == "success"
        for item in state.get("tool_results", [])
    )


def _action_requires_order(analysis: IntentResult) -> bool:
    """根据动作配置判断是否必须先完成订单归属校验。"""
    rule = ACTION_SLOT_RULES.get(str(analysis.action_type or ""))
    return bool(rule and rule.get("need_order_validation"))


def _resolve_ticket_type(analysis: IntentResult) -> str:
    """根据 action_type 映射 Java 工单类型，未配置时回退到 intent。"""
    rule = ACTION_SLOT_RULES.get(str(analysis.action_type or ""))
    return str((rule or {}).get("ticket_type") or analysis.intent)


def _build_ticket_content(state: TicketProcessState) -> str:
    """把动作槽位、原始问题和订单上下文写入工单内容，供坐席完整处理。"""
    analysis = state["analysis"]
    slots = analysis.action_slots or {}
    lines = [
        f"客户诉求：{state['message']}",
        f"业务动作：{analysis.action_type or '未识别'}",
        f"用户目的：{analysis.user_goal}",
    ]
    if slots:
        lines.append("已收集信息：")
        for key, value in slots.items():
            lines.append(f"- {key}: {value}")
    order = _first_success_order(state)
    if order:
        lines.append(
            f"订单上下文：订单 {order.get('orderNo')}，商品 {order.get('productName') or '未记录'}，"
            f"订单状态 {order.get('orderStatus') or order.get('status') or '未知'}，"
            f"售后状态 {order.get('afterSaleStatus') or 'NONE'}。"
        )
    return "\n".join(lines)


def _first_success_order(state: TicketProcessState) -> dict[str, Any] | None:
    """提取第一条成功订单详情，用于工单摘要和回复。"""
    for item in state.get("tool_results", []):
        if item.get("query_type") == "order_detail" and item.get("status") == "success":
            return item.get("data") or {}
    return None


def _find_duplicate_ticket(
    state: TicketProcessState,
    list_customer_tickets: Callable[[str | None], dict[str, Any]],
) -> dict[str, Any] | None:
    """建单前查找同客户、同动作、同订单的未完成工单，避免重复提交。"""
    analysis = state["analysis"]
    if analysis.user_goal != "action_request":
        return None
    slots = analysis.action_slots or {}
    order_no = slots.get("order_no")
    if not order_no:
        return None
    result = list_customer_tickets(state.get("auth_token"))
    tickets = result.get("data") if result.get("status") == "success" else []
    ticket_type = _resolve_ticket_type(analysis)
    active_statuses = {"PENDING_ASSIGN", "PENDING_PROCESS", "PROCESSING"}
    for ticket in tickets or []:
        if (
            ticket.get("ticketType") == ticket_type
            and str(ticket.get("orderNo") or "").lower() == str(order_no).lower()
            and ticket.get("status") in active_statuses
            and _ticket_has_same_action(ticket, str(analysis.action_type or ""))
        ):
            return ticket
    return None


def _ticket_has_same_action(ticket: dict[str, Any], action_type: str) -> bool:
    """只复用同一业务动作创建的新式工单，避免旧退款咨询工单拦截退货申请。"""
    if not action_type:
        return False
    content = f"{ticket.get('content') or ''}\n{ticket.get('aiSummary') or ''}"
    return f"业务动作：{action_type}" in content


def should_auto_assign_ticket(state: TicketProcessState) -> bool:
    """判断 Agent 建单后是否允许自动派单，默认关闭并保留给调度员确认。"""
    if os.getenv("AGENT_AUTO_ASSIGN_TICKET", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        return False

    analysis = state.get("analysis")
    if not analysis:
        return False
    if analysis.confidence < 0.85:
        return False

    risk_reasons = set(state.get("risk_reasons", [])) | set(analysis.risk_reasons)
    high_risk_actions = {
        "return_goods",
        "refund_request",
        "exchange_goods",
        "cancel_order",
        "complaint_submit",
    }
    blocked_risks = {
        "low_confidence",
        "complaint",
        "dispute",
        "legal_risk",
        "compensation_claim",
        "after_sale_dispute",
        "action_or_dispute_requires_human",
        "refund_commitment",
        "model_analyze_failed",
        "order_validation_failed",
    }

    if analysis.user_goal in {"action_request", "complaint", "dispute"}:
        return False
    if analysis.intent == "complaint" or analysis.emotion in {"dissatisfied", "strong_complaint"}:
        return False
    if str(analysis.action_type or "") in high_risk_actions:
        return False
    if risk_reasons & blocked_risks:
        return False

    allowed_intents = _auto_assign_allowed_intents()
    return analysis.intent in allowed_intents


def _auto_assign_allowed_intents() -> set[str]:
    """读取允许自动派单的低风险业务域，未配置时只允许明确低风险域。"""
    raw_value = os.getenv("AGENT_AUTO_ASSIGN_LOW_RISK_INTENTS", "logistics,invoice,member")
    return {item.strip() for item in raw_value.split(",") if item.strip()}


def _is_logistics_query(message: str, intent: str) -> bool:
    """识别需要查询物流轨迹的只读问题，供图节点决定是否调用物流工具。"""
    logistics_words = ["物流", "快递", "配送", "发货", "签收", "送达", "到达", "到哪", "什么时候到", "转运", "路线", "全流程"]
    return intent == "logistics" or any(word in message for word in logistics_words)


def _is_how_to_query(message: str) -> bool:
    """识别操作步骤咨询，避免选中工单抢占“怎么查询物流状态”等问题。"""
    if any(word in message for word in ["怎么还没", "为什么还没", "怎么不到账", "怎么没到"]):
        return False
    return any(word in message for word in ["怎么查询", "如何查询", "怎么查看", "如何查看", "在哪里看", "在哪看", "怎么申请", "如何申请", "怎么操作", "如何操作"])


def _resolve_assigned_group(intent: str) -> str:
    """根据意图给 Java 工单系统一个默认处理组，正式环境可替换为规则配置。"""
    if intent == "logistics":
        return "物流组"
    if intent in {"refund", "exchange"}:
        return "售后组"
    if intent == "repair":
        return "技术组"
    if intent == "complaint":
        return "投诉处理组"
    return "客服组"
