"""客服坐席工作台 Demo，用于处理 Agent 创建的待办工单并更新客户可见进度。"""

import os
from typing import Any

import requests
import streamlit as st

AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", "http://localhost:8000")
BUSINESS_SERVICE_URL = os.getenv("BUSINESS_SERVICE_URL", "http://localhost:8081")
AUTH_TIMEOUT = int(os.getenv("STAFF_AUTH_TIMEOUT", "5"))
REQUEST_TIMEOUT = int(os.getenv("STAFF_REQUEST_TIMEOUT", "8"))

DEFAULT_QUEUE_STATUS = "PENDING_ASSIGN,PENDING_PROCESS,PROCESSING,TRANSFERRED,REOPENED"

st.set_page_config(page_title="Staff Console", layout="wide")
st.title("客服坐席工作台")


def auth_headers() -> dict[str, str]:
    """组装坐席 Token 请求头，所有工单处理动作都必须通过 Java 侧鉴权。"""
    token = st.session_state.get("staff_auth_token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def login_user(username: str, password: str) -> dict[str, Any]:
    """调用 Agent 登录代理，实际账号和角色由 Java business-service 校验。"""
    response = requests.post(
        f"{AGENT_BASE_URL}/api/auth/login",
        json={"username": username, "password": password},
        timeout=AUTH_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def fetch_current_user() -> dict[str, Any] | None:
    """读取当前登录坐席，角色不是 staff 时拒绝进入工作台。"""
    try:
        response = requests.get(
            f"{AGENT_BASE_URL}/api/auth/current-user",
            headers=auth_headers(),
            timeout=AUTH_TIMEOUT,
        )
        response.raise_for_status()
        user = response.json()
        if user.get("role") != "staff":
            st.error("当前账号不是客服坐席，请使用 staff / 123456 登录。")
            return None
        return user
    except requests.RequestException as exc:
        st.warning(f"登录状态已失效，请重新登录：{exc}")
        return None


def fetch_tickets(status_filter: str) -> list[dict[str, Any]]:
    """查询坐席工单队列，默认展示所有待处理相关状态。"""
    try:
        response = requests.get(
            f"{BUSINESS_SERVICE_URL}/api/staff/tickets",
            params={"status": status_filter},
            headers=auth_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        st.error(f"工单列表加载失败：{exc}")
        return []


def update_ticket(ticket_no: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """调用坐席工单动作接口，并把 Java 返回的最新工单状态交给页面刷新。"""
    response = requests.post(
        f"{BUSINESS_SERVICE_URL}/api/staff/tickets/{ticket_no}/{action}",
        json=payload,
        headers=auth_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def draft_customer_reply(ticket_no: str, close_reason: str) -> dict[str, Any]:
    """请求 Agent 根据坐席处理结果生成客户安全话术草稿，草稿不会自动发送。"""
    response = requests.post(
        f"{AGENT_BASE_URL}/api/staff/tickets/{ticket_no}/reply/draft",
        json={"close_reason": close_reason},
        headers=auth_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def send_customer_reply(ticket_no: str, message: str) -> dict[str, Any]:
    """发送坐席确认后的客户可见回复，消息会进入客户侧会话历史。"""
    response = requests.post(
        f"{AGENT_BASE_URL}/api/staff/tickets/{ticket_no}/reply/send",
        json={"message": message},
        headers=auth_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def logout() -> None:
    """退出坐席登录并清空本地缓存，避免后续操作复用旧 Token。"""
    for key in [
        "staff_auth_token",
        "staff_current_user",
        "selected_ticket_no",
        "last_action_result",
        "draft_ticket_no",
        "draft_message",
        "draft_error",
    ]:
        st.session_state.pop(key, None)


def render_login() -> None:
    """渲染坐席登录页，MVP 阶段使用 Demo staff 账号。"""
    st.subheader("坐席登录")
    st.caption("Demo 坐席账号：staff / 123456")
    with st.form("staff_login_form"):
        username = st.text_input("用户名", value="staff")
        password = st.text_input("密码", value="123456", type="password")
        submitted = st.form_submit_button("登录", type="primary")
    if submitted:
        try:
            result = login_user(username, password)
            user = result["user"]
            if user.get("role") != "staff":
                st.error("该账号没有坐席权限。")
                return
            st.session_state["staff_auth_token"] = result["token"]
            st.session_state["staff_current_user"] = user
            st.rerun()
        except requests.RequestException as exc:
            st.error(f"登录失败：{exc}")


if not st.session_state.get("staff_auth_token"):
    render_login()
    st.stop()

current_user = st.session_state.get("staff_current_user") or fetch_current_user()
if not current_user:
    logout()
    render_login()
    st.stop()
st.session_state["staff_current_user"] = current_user

with st.sidebar:
    st.subheader("工单队列")
    st.caption(f"当前坐席：{current_user.get('display_name')}")
    if st.button("退出登录"):
        logout()
        st.rerun()

    status_filter = st.multiselect(
        "状态筛选",
        ["PENDING_ASSIGN", "PENDING_PROCESS", "PROCESSING", "TRANSFERRED", "REOPENED", "CLOSED"],
        default=["PENDING_ASSIGN", "PENDING_PROCESS", "PROCESSING", "TRANSFERRED", "REOPENED"],
    )
    if st.button("刷新工单"):
        st.session_state.pop("staff_tickets", None)

status_value = ",".join(status_filter) if status_filter else DEFAULT_QUEUE_STATUS
if st.session_state.get("staff_status_value") != status_value:
    # 状态筛选变化时刷新队列，避免坐席看到上一组筛选条件下的旧数据。
    st.session_state["staff_status_value"] = status_value
    st.session_state.pop("staff_tickets", None)
if "staff_tickets" not in st.session_state:
    st.session_state["staff_tickets"] = fetch_tickets(status_value)
tickets = st.session_state["staff_tickets"]

ticket_options = {
    f"{item.get('ticketNo')} | {item.get('status')} | {item.get('priority')} | {item.get('title')}": item
    for item in tickets
}

if not ticket_options:
    st.info("当前没有待处理工单。")
    st.stop()

selected_label = st.selectbox("选择工单", list(ticket_options.keys()))
ticket = ticket_options[selected_label]
st.session_state["selected_ticket_no"] = ticket.get("ticketNo")
if st.session_state.get("draft_ticket_no") != ticket.get("ticketNo"):
    # 切换工单时清理上一张工单的草稿，避免坐席误发到错误客户会话。
    st.session_state.pop("draft_message", None)
    st.session_state.pop("draft_error", None)
    st.session_state["draft_ticket_no"] = ticket.get("ticketNo")

left, right = st.columns([2, 1])
with left:
    st.markdown("#### 工单详情")
    st.write(f"工单号：{ticket.get('ticketNo')}")
    st.write(f"状态：{ticket.get('status')}")
    st.write(f"优先级：{ticket.get('priority')}")
    st.write(f"处理组：{ticket.get('assignedGroup') or '-'}")
    st.write(f"处理人：{ticket.get('handlerId') or '-'}")
    st.write(f"关联订单：{ticket.get('orderNo') or '-'}")
    st.write(f"SLA 截止：{ticket.get('slaDeadline') or '-'}")
    st.markdown("#### 客户问题")
    st.info(ticket.get("content") or ticket.get("title") or "")
    st.markdown("#### AI 摘要")
    st.write(ticket.get("aiSummary") or "暂无 AI 摘要")

with right:
    st.markdown("#### 处理操作")
    handler_id = st.number_input("处理人 ID", min_value=1, value=int(current_user.get("user_id") or 10001))
    assigned_group = st.text_input("处理组", value=ticket.get("assignedGroup") or "客服组")

    if st.button("领取/分派", type="primary"):
        try:
            # 领取后进入待处理，客户侧仍可看到工单已被坐席承接。
            st.session_state["last_action_result"] = update_ticket(
                ticket["ticketNo"],
                "assign",
                {"handlerId": handler_id, "assignedGroup": assigned_group},
            )
            st.session_state.pop("staff_tickets", None)
            st.rerun()
        except requests.RequestException as exc:
            st.error(f"领取失败：{exc}")

    if st.button("开始处理"):
        try:
            # 开始处理会推进为 PROCESSING，客户刷新后能看到最新进度。
            st.session_state["last_action_result"] = update_ticket(
                ticket["ticketNo"],
                "status",
                {"status": "PROCESSING", "operatorId": handler_id, "reason": "坐席开始处理"},
            )
            st.session_state.pop("staff_tickets", None)
            st.rerun()
        except requests.RequestException as exc:
            st.error(f"状态更新失败：{exc}")

    close_reason = st.text_area("关闭说明", value="问题已处理完成")
    if st.button("关闭工单"):
        try:
            # 关闭工单代表人工处理闭环完成，客户侧最新状态会显示 CLOSED。
            st.session_state["last_action_result"] = update_ticket(
                ticket["ticketNo"],
                "close",
                {"operatorId": handler_id, "closeReason": close_reason},
            )
            st.session_state.pop("staff_tickets", None)
            st.rerun()
        except requests.RequestException as exc:
            st.error(f"关闭失败：{exc}")

    st.divider()
    st.markdown("#### 客户回复")
    if st.button("生成客户话术草稿"):
        try:
            # Agent 只生成草稿，最终发送必须由坐席确认。
            draft_result = draft_customer_reply(ticket["ticketNo"], close_reason)
            st.session_state["draft_message"] = draft_result["draft_message"]
            st.session_state.pop("draft_error", None)
        except requests.RequestException as exc:
            st.session_state["draft_error"] = f"草稿生成失败：{exc}"

    if st.session_state.get("draft_error"):
        st.error(st.session_state["draft_error"])

    draft_message = st.text_area(
        "客户可见内容",
        value=st.session_state.get("draft_message", ""),
        height=180,
        placeholder="请先生成草稿，或直接填写要发送给客户的处理说明。",
    )
    st.session_state["draft_message"] = draft_message
    if st.button("确认发送给客户", type="primary"):
        try:
            send_result = send_customer_reply(ticket["ticketNo"], draft_message)
            st.session_state["last_action_result"] = {
                "ticketNo": ticket["ticketNo"],
                "status": "已发送客户回复",
                "session_id": send_result.get("session_id"),
            }
            st.session_state.pop("draft_message", None)
            st.rerun()
        except requests.RequestException as exc:
            st.error(f"发送失败：{exc}")

if st.session_state.get("last_action_result"):
    latest = st.session_state["last_action_result"]
    st.success(f"最近操作成功：{latest.get('ticketNo')} 当前状态 {latest.get('status')}")
