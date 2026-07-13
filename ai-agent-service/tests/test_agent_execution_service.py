"""验证在线 Agent 执行服务与 FastAPI 路由解耦。"""

from types import SimpleNamespace

from schemas.intent_schema import AgentReplyRequest
from services.agent_execution_service import AgentExecutionAccessDenied, AgentExecutionService


class FakeSessions:
    """提供执行服务测试所需的最小会话仓储行为。"""

    def __init__(self) -> None:
        self.created = []
        self.updated = []

    def create(self, customer_id: int, title: str) -> dict:
        self.created.append((customer_id, title))
        return {"session_id": "session-1", "status": "AI_ONLY"}

    def get_by_session_no_for_customer(self, session_id: str, customer_id: int) -> dict | None:
        return None

    def update_after_agent_reply(self, session_id: str, analysis: dict, status: str) -> None:
        self.updated.append((session_id, analysis, status))


class FakeMessages:
    """记录执行服务写入的客户和 AI 消息。"""

    def __init__(self) -> None:
        self.saved = []

    def list_by_session(self, session_id: str) -> list[dict]:
        return []

    def save(self, **kwargs) -> None:
        self.saved.append(kwargs)


class FakeEvaluationRepository:
    """收集 Trace，避免测试依赖实际数据库。"""

    def __init__(self) -> None:
        self.traces = []

    def capture_online_trace(self, trace: dict) -> None:
        self.traces.append(trace)


class FakeAgent:
    """模拟真实 Agent 的结构化回复。"""

    def __init__(self) -> None:
        self.payload = None

    def reply(self, payload: AgentReplyRequest) -> SimpleNamespace:
        self.payload = payload
        return SimpleNamespace(
            model_dump=lambda: {
                "answer": "退款政策说明",
                "customer_message": "退款政策说明",
                "internal_suggestion": None,
                "decision_type": "auto_reply",
                "service_status": "自动回复",
                "auto_send": True,
                "need_human": False,
                "analysis": {"intent": "refund", "user_goal": "policy_consult"},
                "citations": [],
                "citation_validation": {},
                "tool_results": [],
                "ticket_result": None,
                "risk_reasons": [],
                "pending_action_request": None,
            }
        )


def test_execution_service_runs_session_agent_persistence_and_trace() -> None:
    """执行服务应独立完成会话、Agent、消息持久化和 Trace 采集。"""
    sessions = FakeSessions()
    messages = FakeMessages()
    traces = FakeEvaluationRepository()
    agent = FakeAgent()
    service = AgentExecutionService(
        agent=agent,
        chat_sessions=sessions,
        chat_messages=messages,
        evaluation_repository=traces,
        staff_availability_loader=lambda: [],
    )

    result = service.execute(AgentReplyRequest(message="我想退款", customer_id=7, auth_token="token"))

    assert result["session_id"] == "session-1"
    assert agent.payload.session_id == "session-1"
    assert sessions.created == [(7, "我想退款")]
    assert [message["sender_type"] for message in messages.saved] == ["customer", "ai"]
    assert sessions.updated[0][2] == "AI_REPLIED"
    assert traces.traces[0]["customer_id"] == 7


def test_execution_service_rejects_other_customer_session() -> None:
    """会话不属于当前客户时，服务必须返回可由接口层映射的鉴权异常。"""
    service = AgentExecutionService(
        agent=FakeAgent(),
        chat_sessions=FakeSessions(),
        chat_messages=FakeMessages(),
        evaluation_repository=FakeEvaluationRepository(),
        staff_availability_loader=lambda: [],
    )

    try:
        service.execute(AgentReplyRequest(message="继续咨询", session_id="other-session", customer_id=7))
    except AgentExecutionAccessDenied:
        pass
    else:
        raise AssertionError("应拒绝访问其他客户会话")
