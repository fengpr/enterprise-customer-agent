"""独立 Agent Worker：执行可靠队列任务并把客户可见流式事件写入 Redis。"""

import time
import inspect
from services.observability import WORKER_DURATION, WORKER_JOBS, set_request_context
from dotenv import load_dotenv
from typing import Any

from services.agent_execution_queue import AgentExecutionQueue
from services.agent_execution_service import AgentExecutionService
from services.resilient_client import ResilienceError
from services.stream_event_service import StreamEventService


def run_once(
    queue: AgentExecutionQueue,
    execution_service: AgentExecutionService,
    event_service: StreamEventService | None = None,
) -> bool:
    """消费一个任务；客户端断开不会中止模型执行或最终结果持久化。"""
    claimed = queue.recover_pending() or queue.claim()
    if not claimed:
        return False
    stream_id, request_id, job, attempt = claimed
    # 队列 payload 只保存随机 Trace ID；恢复 Pending 任务仍可关联原链路。
    set_request_context(request_id, getattr(job, "trace_id", None))
    events = event_service or StreamEventService()

    def publish(event_type: str, payload: dict[str, Any] | None = None) -> None:
        """只记录客户可见内容，禁止将凭证、Prompt 和工具原始返回写入事件流。"""
        events.publish(request_id, event_type, payload)

    try:
        with WORKER_DURATION.time():
            # 兼容既有测试替身与扩展实现；真实 AgentExecutionService 接收事件发布器。
            if "event_publisher" in inspect.signature(execution_service.execute).parameters:
                result = execution_service.execute(job.to_request(), event_publisher=publish)
            else:
                result = execution_service.execute(job.to_request())
        result["request_id"] = request_id
        queue.ack_success(stream_id, request_id, result)
        WORKER_JOBS.labels("degraded" if result.get("degraded") else "success").inc()
        publish("degraded" if result.get("degraded") else "completed", {
            "answer": result.get("customer_message") or result.get("answer", ""),
            "service_status": result.get("service_status"),
            "degraded": bool(result.get("degraded")),
        })
    except ResilienceError as exc:
        # 流式模型超时、429 或熔断属于可恢复下游故障，不能把一次重试误报成终态错误。
        error_code = f"{exc.downstream.upper()}_{exc.error_type.upper()}"
        _retry_or_publish_terminal(queue, events, stream_id, request_id, job, attempt, error_code)
        WORKER_JOBS.labels("failed").inc()
    except Exception:
        # 客户侧只得到安全错误码，任务本身仍由 Stream 重试和死信策略接管。
        _retry_or_publish_terminal(queue, events, stream_id, request_id, job, attempt, "AGENT_UPSTREAM_UNAVAILABLE")
        WORKER_JOBS.labels("failed").inc()
    return True


def _retry_or_publish_terminal(
    queue: AgentExecutionQueue,
    events: StreamEventService,
    stream_id: str,
    request_id: str,
    job: Any,
    attempt: int,
    error_code: str,
) -> None:
    """按队列最大尝试次数发布“重试中”或最终安全降级事件。

    Redis Stream 会负责将可恢复任务重新投递。SSE 仅在任务真正进入死信时发送
    终态 degraded，避免客户端在第一次模型抖动时就停止等待后续成功结果。
    """
    is_terminal = attempt + 1 >= int(getattr(queue, "max_attempts", 3))
    queue.retry_or_dead_letter(stream_id, request_id, job, attempt, error_code)
    if is_terminal:
        events.publish(request_id, "degraded", {
            "error_code": error_code,
            "customer_message": "当前咨询量较大，已为您转入人工处理队列，请稍后查看处理进度。",
            "degraded": True,
        })
        return
    events.publish(request_id, "queued", {
        "error_code": error_code,
        "retry_after": 3,
        "customer_message": "服务正在重试处理中，请稍后查看回复。",
    })


def main() -> None:
    """常驻消费可靠队列，API 进程只维护 SSE 连接而不执行模型。"""
    # 支持从命令行单独启动 Worker；必须在创建队列前加载本地运行配置。
    load_dotenv()
    queue = AgentExecutionQueue()
    if not queue.enabled:
        raise RuntimeError("Agent Worker 无法连接 Redis 队列：请检查 REDIS_URL、AGENT_EXECUTION_QUEUE_ENABLED=true，以及 Redis 服务是否返回 PONG")
    execution_service = AgentExecutionService()
    event_service = StreamEventService()
    while True:
        # 每轮刷新一次心跳；若模型调用卡住，心跳会自然过期，便于启动脚本拉起替代 Worker。
        queue.heartbeat()
        run_once(queue, execution_service, event_service)
        time.sleep(0.01)


if __name__ == "__main__":
    main()
