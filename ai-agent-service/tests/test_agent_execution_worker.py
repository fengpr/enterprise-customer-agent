"""验证 Worker 通过 AgentExecutionService 执行，且不导入 FastAPI 应用。"""

from pathlib import Path

from rag.agent_execution_worker import run_once
from services.resilient_client import ResilienceError


class FakeQueue:
    """模拟领取与完成一个队列任务。"""

    def __init__(self) -> None:
        self.completed = []
        self.retries = []
        self.max_attempts = 3

    def claim(self):
        from schemas.intent_schema import AgentExecutionJob
        return "1-0", "request-1", AgentExecutionJob(request_id="request-1", customer_id=9, message="查询订单", idempotency_key="test-1"), 0

    def recover_pending(self):
        return None

    def ack_success(self, stream_id: str, request_id: str, result: dict) -> None:
        self.completed.append((stream_id, request_id, result))

    def retry_or_dead_letter(self, *args) -> None:
        self.retries.append(args)


class FakeExecutionService:
    """验证 Worker 仅依赖共享执行服务。"""

    def __init__(self) -> None:
        self.request = None

    def execute(self, request):
        self.request = request
        return {"answer": "订单处理中"}


class FailingExecutionService:
    """模拟 Worker 执行异常，验证任务不被静默丢弃。"""

    def execute(self, request):
        raise RuntimeError("upstream unavailable")


class TimeoutExecutionService:
    """模拟模型首 token 超时，验证任务会回到队列而非把 SSE 直接结束。"""

    def execute(self, request):
        raise ResilienceError("online_llm", "timeout", True, safe_message="模型响应超时")


class FakeEventService:
    """记录 Worker 面向客户发布的流式事件。"""

    def __init__(self) -> None:
        self.events = []

    def publish(self, request_id: str, event_type: str, payload: dict | None = None) -> None:
        self.events.append((request_id, event_type, payload))


def test_worker_uses_execution_service_and_shared_job_contract() -> None:
    """Worker 应把队列载荷转换为 AgentExecutionJob 后委托执行服务。"""
    queue = FakeQueue()
    service = FakeExecutionService()

    assert run_once(queue, service) is True
    assert service.request.customer_id == 9
    assert queue.completed == [("1-0", "request-1", {"answer": "订单处理中", "request_id": "request-1"})]


def test_worker_failure_is_retried_or_sent_to_dead_letter() -> None:
    """执行失败时 Worker 应委托队列记录重试或死信，而不是直接丢弃消息。"""
    queue = FakeQueue()

    assert run_once(queue, FailingExecutionService()) is True
    assert queue.retries[0][1] == "request-1"
    assert queue.retries[0][-1] == "AGENT_UPSTREAM_UNAVAILABLE"


def test_worker_stream_timeout_is_requeued_without_terminal_error() -> None:
    """流式模型首次超时应发布 queued 重试状态，不能误导前端停止接收事件。"""
    queue = FakeQueue()
    events = FakeEventService()

    assert run_once(queue, TimeoutExecutionService(), events) is True
    assert queue.retries[0][-1] == "ONLINE_LLM_TIMEOUT"
    assert events.events[-1][1] == "queued"


def test_worker_does_not_import_fastapi_app() -> None:
    """防止 Worker 再次反向依赖 app.py，保持进程边界清晰。"""
    source = (Path(__file__).parents[1] / "rag" / "agent_execution_worker.py").read_text(encoding="utf-8")
    app_source = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
    assert "from app import" not in source
    assert "AgentExecutionService" in source
    assert "from rag.agent_execution_worker import" not in app_source
