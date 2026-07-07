"""客户自助入口 Demo，用于联调登录、会话历史、Agent 客户话术和工单进度。"""

import os
from typing import Any

import requests
import streamlit as st

AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", "http://localhost:8000")
REQUEST_TIMEOUT = int(os.getenv("AGENT_REQUEST_TIMEOUT", "90"))
AUTH_TIMEOUT = int(os.getenv("AGENT_AUTH_TIMEOUT", "5"))
META_TIMEOUT = int(os.getenv("AGENT_META_TIMEOUT", "3"))

st.set_page_config(page_title="Enterprise Customer Agent", layout="wide")
st.title("Enterprise Customer Agent 自助服务")


def auth_headers() -> dict[str, str]:
    """组装带 Token 的请求头，所有客户数据接口都必须通过登录态访问。"""
    token = st.session_state.get("auth_token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def login_user(username: str, password: str) -> dict[str, Any]:
    """调用 Agent 代理登录接口，实际认证由 Java business-service 完成。"""
    response = requests.post(
        f"{AGENT_BASE_URL}/api/auth/login",
        json={"username": username, "password": password},
        timeout=AUTH_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def fetch_current_user() -> dict[str, Any] | None:
    """通过 Token 读取当前登录用户，Token 失效时清理本地登录态。"""
    try:
        response = requests.get(
            f"{AGENT_BASE_URL}/api/auth/current-user",
            headers=auth_headers(),
            timeout=AUTH_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        st.session_state.pop("auth_token", None)
        st.session_state.pop("current_user", None)
        st.warning(f"登录状态已失效，请重新登录：{exc}")
        return None


def fetch_agent_status() -> dict[str, Any]:
    """读取 Agent 状态并缓存，避免页面每次重跑都阻塞主流程。"""
    if st.session_state.get("agent_status"):
        return st.session_state["agent_status"]
    try:
        response = requests.get(f"{AGENT_BASE_URL}/api/agent/status", timeout=META_TIMEOUT)
        response.raise_for_status()
        st.session_state["agent_status"] = response.json()
    except requests.RequestException as exc:
        st.session_state["agent_status"] = {"status": "failed", "error": str(exc)}
    return st.session_state["agent_status"]


def fetch_sessions() -> list[dict[str, Any]]:
    """读取当前客户最近会话列表，后端会按登录客户隔离数据。"""
    try:
        response = requests.get(
            f"{AGENT_BASE_URL}/api/chat/session/list",
            headers=auth_headers(),
            timeout=META_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        st.sidebar.error(f"会话列表加载失败：{exc}")
        return []


def fetch_session_detail(session_id: str) -> dict[str, Any]:
    """读取会话详情和历史消息，客户侧只展示已脱敏后的消息正文。"""
    try:
        response = requests.get(
            f"{AGENT_BASE_URL}/api/chat/session/{session_id}",
            headers=auth_headers(),
            timeout=META_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        st.error(f"会话详情加载失败：{exc}")
        return {"session": None, "messages": []}


def fetch_ticket_detail(ticket_no: str) -> dict[str, Any] | None:
    """按工单号刷新业务系统最新状态，客户侧只展示可追踪字段。"""
    try:
        response = requests.get(
            f"{AGENT_BASE_URL}/api/customer/tickets/{ticket_no}",
            headers=auth_headers(),
            timeout=META_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def load_sessions(force: bool = False) -> list[dict[str, Any]]:
    """按需加载会话列表，减少按钮点击后的同步等待。"""
    if force or "sessions" not in st.session_state:
        st.session_state["sessions"] = fetch_sessions()
    return st.session_state.get("sessions", [])


def logout() -> None:
    """退出登录并清空本地状态，避免不同客户之间串会话。"""
    for key in [
        "auth_token",
        "current_user",
        "selected_session_id",
        "agent_reply",
        "agent_error",
        "sessions",
        "agent_status",
    ]:
        st.session_state.pop(key, None)


def render_login() -> None:
    """渲染登录页，未登录客户不能进入自助服务入口。"""
    st.subheader("登录")
    st.caption("Demo 账号：demo / 123456，buyer / 123456")
    with st.form("login_form"):
        username = st.text_input("用户名", value="demo")
        password = st.text_input("密码", value="123456", type="password")
        submitted = st.form_submit_button("登录", type="primary")
    if submitted:
        with st.spinner("正在登录..."):
            try:
                result = login_user(username, password)
                st.session_state["auth_token"] = result["token"]
                st.session_state["current_user"] = result["user"]
                st.session_state.pop("agent_error", None)
                st.session_state.pop("agent_reply", None)
                st.session_state.pop("sessions", None)
                st.rerun()
            except requests.RequestException as exc:
                st.error(f"登录失败：{exc}")


def render_ticket_status(ticket_result: dict[str, Any] | None) -> None:
    """展示客户可追踪的工单信息，不暴露工具调用原始响应。"""
    if not ticket_result:
        return
    if ticket_result.get("status") == "success":
        ticket_data = ticket_result.get("data", {})
        ticket_no = ticket_data.get("ticketNo")
        # 客户侧每次渲染时按工单号刷新 Java 最新状态，保证坐席处理后能看到进度变化。
        latest_ticket = fetch_ticket_detail(ticket_no) if ticket_no else None
        display_ticket = latest_ticket or ticket_data
        st.success(f"工单号：{display_ticket.get('ticketNo')} | 当前状态：{display_ticket.get('status')}")
    elif ticket_result.get("status") == "failed":
        st.info("问题已记录，客服会继续跟进处理。")


def render_service_status(reply: dict[str, Any]) -> None:
    """按客户可理解的处理状态展示 Agent 结果。"""
    status = reply.get("service_status") or "处理中"
    if status == "自动回复":
        st.success("处理进度：自动回复")
    elif "人工" in status:
        st.warning(f"处理进度：{status}")
    else:
        st.info(f"处理进度：{status}")


def render_message_history(session_id: str) -> None:
    """渲染历史会话，只展示客户消息、AI 客户话术和坐席确认后的客户可见回复。"""
    detail = fetch_session_detail(session_id)
    messages = detail.get("messages", [])
    if not messages:
        return

    st.markdown("#### 会话历史")
    for item in messages:
        sender_type = item["sender_type"]
        is_customer = sender_type == "customer"
        with st.chat_message("user" if is_customer else "assistant"):
            if sender_type == "staff":
                # 坐席消息是人工确认后发送给客户的正式回复，客户侧需要明确区分来源。
                st.caption("客服回复")
            elif sender_type == "ai":
                st.caption("智能客服")
            st.write(item["content"])
            if not is_customer:
                render_ticket_status(item.get("extra_data", {}).get("ticket_result"))


if not st.session_state.get("auth_token"):
    render_login()
    st.stop()

current_user = st.session_state.get("current_user")
if not current_user:
    current_user = fetch_current_user()
    if not current_user:
        render_login()
        st.stop()
    st.session_state["current_user"] = current_user

with st.sidebar:
    st.subheader("我的服务")
    st.caption(f"当前登录：{current_user.get('display_name')}")
    if st.button("退出登录"):
        logout()
        st.rerun()

    st.divider()
    refresh_sessions = st.button("刷新会话")
    sessions = load_sessions(force=refresh_sessions)
    session_options = {
        f"{item['title'] or item['session_id']} | {item['status']}": item["session_id"]
        for item in sessions
    }
    if session_options:
        selected_session_label = st.selectbox("历史会话", list(session_options.keys()))
        st.session_state["selected_session_id"] = session_options[selected_session_label]
    else:
        st.caption("暂无历史会话")

agent_status = fetch_agent_status()
if agent_status.get("status") == "failed":
    st.warning(f"Agent 服务状态异常：{agent_status.get('error')}")
else:
    llm_status = agent_status.get("llm", {})
    st.caption(f"Agent: {AGENT_BASE_URL} | LLM: {llm_status.get('provider') or '未启用'}")

selected_session_id = st.session_state.get("selected_session_id")
if selected_session_id:
    st.caption(f"当前会话：{selected_session_id}")
    if st.button("刷新最新回复和进度"):
        # Streamlit 不做消息推送，客户需要主动刷新来读取坐席确认发送的新回复。
        st.session_state.pop("sessions", None)
        st.rerun()

message = st.text_area("请描述您的问题", value="我买的鞋子质量不好，商家不给更换")
if st.button("提交问题", type="primary"):
    st.session_state.pop("agent_error", None)
    with st.spinner("已收到您的问题，正在为您处理..."):
        try:
            # 客户身份只通过 Token 透传，后端会用 Java 认证结果补齐 customer_id。
            response = requests.post(
                f"{AGENT_BASE_URL}/api/agent/reply",
                headers=auth_headers(),
                json={"message": message, "session_id": selected_session_id},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            reply = response.json()
            st.session_state["agent_reply"] = reply
            st.session_state["selected_session_id"] = reply.get("session_id")
            st.session_state.pop("sessions", None)
        except requests.Timeout:
            st.session_state["agent_error"] = f"请求处理时间较长，请稍后刷新查看结果。当前等待上限为 {REQUEST_TIMEOUT} 秒。"
        except requests.RequestException as exc:
            st.session_state["agent_error"] = f"问题提交失败：{exc}"

if st.session_state.get("agent_error"):
    st.error(st.session_state["agent_error"])

reply = st.session_state.get("agent_reply")
if reply:
    st.markdown("#### 处理结果")
    render_service_status(reply)
    st.info(reply.get("customer_message") or reply.get("answer"))
    render_ticket_status(reply.get("ticket_result"))

if st.session_state.get("selected_session_id"):
    render_message_history(st.session_state["selected_session_id"])
