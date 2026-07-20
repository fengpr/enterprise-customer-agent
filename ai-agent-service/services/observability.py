"""生产可观测性基础：Prometheus 指标与不含敏感数据的 Trace 上下文。"""

import contextvars
import hashlib
import os
import time
import uuid
from contextlib import contextmanager

from prometheus_client import Counter, Gauge, Histogram
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

def _configure_tracing() -> None:
    """仅在配置 Collector 地址时启用 OTLP 导出；本地开发仅创建 Span。"""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider = TracerProvider(resource=Resource.create({"service.name": "ai-agent-service"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
    except Exception:
        return


_configure_tracing()
tracer = trace.get_tracer("enterprise-customer-agent")

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")
HTTP_REQUESTS = Counter("agent_http_requests_total", "Agent HTTP 请求总数", ["path", "method", "status"])
HTTP_LATENCY = Histogram("agent_http_request_duration_seconds", "Agent HTTP 延迟", ["path", "method"])
SSE_CONNECTIONS = Gauge("agent_sse_active_connections", "当前活跃 SSE 连接数")
SSE_EVENTS = Counter("agent_sse_events_total", "SSE 事件总数", ["event_type"])
WORKER_JOBS = Counter("agent_worker_jobs_total", "Worker 任务总数", ["status"])
WORKER_DURATION = Histogram("agent_worker_duration_seconds", "Worker 任务耗时")
DOWNSTREAM_REQUESTS = Counter("agent_downstream_requests_total", "下游调用总数", ["downstream", "outcome"])
DOWNSTREAM_DURATION = Histogram("agent_downstream_duration_seconds", "下游调用耗时", ["downstream"])
DEGRADED = Counter("agent_degraded_total", "安全降级次数", ["reason"])
HANDOFF = Counter("agent_handoff_total", "转人工次数", ["reason"])
QUEUE_DEPTH = Gauge("agent_queue_depth", "Redis Stream 待消费任务数")
QUEUE_PENDING = Gauge("agent_queue_pending", "Redis Stream Pending 任务数")
DLQ_DEPTH = Gauge("agent_dead_letter_depth", "死信队列任务数")
CACHE = Counter("agent_cache_operations_total", "缓存操作次数", ["cache", "outcome"])
QUEUE_RETRIES = Gauge("agent_queue_retrying", "正在重试的队列任务数")
QUEUE_RUNNING = Gauge("agent_queue_running", "正在执行的队列任务数")
CANCELLATIONS = Counter("agent_execution_cancellations_total", "Agent 取消请求数", ["outcome"])
CANCELLATION_DURATION = Histogram("agent_execution_cancel_duration_seconds", "Agent 从请求取消到确认终止耗时")
CANCELLATION_PARTIAL_TOKENS = Histogram("agent_execution_cancel_partial_tokens", "取消前已输出的文本片段数")
# 查询重写单独统计，避免与客户回复模型、DeepEval Judge 的调用量混在一起。
RAG_QUERY_REWRITES = Counter("agent_rag_query_rewrite_total", "RAG 查询重写次数", ["outcome"])
RAG_QUERY_REWRITE_DURATION = Histogram("agent_rag_query_rewrite_duration_seconds", "RAG 查询重写耗时")


def set_request_context(request_id: str | None = None, trace_id: str | None = None) -> tuple[str, str]:
    """设置请求上下文；仅使用随机 ID，不把客户或业务字段写入 Trace。"""
    resolved_request_id = request_id or f"req-{uuid.uuid4().hex[:16]}"
    resolved_trace_id = trace_id or uuid.uuid4().hex
    request_id_var.set(resolved_request_id)
    trace_id_var.set(resolved_trace_id)
    return resolved_request_id, resolved_trace_id


def current_context() -> dict[str, str]:
    """返回可安全写入日志或下游 Header 的关联 ID。"""
    return {"request_id": request_id_var.get(), "trace_id": trace_id_var.get()}


def safe_identifier(value: object) -> str:
    """对业务标识做稳定哈希，避免订单号、手机号等进入指标标签。"""
    return hashlib.sha256(str(value).encode()).hexdigest()[:12]


@contextmanager
def observe_downstream(downstream: str):
    """记录下游调用耗时、成功和错误类型，不记录请求体或凭证。"""
    started = time.perf_counter()
    with tracer.start_as_current_span(f"downstream.{downstream}") as span:
        span.set_attribute("downstream.name", downstream)
        try:
            yield
            DOWNSTREAM_REQUESTS.labels(downstream, "success").inc()
        except Exception as exc:
            span.set_attribute("error.type", getattr(exc, "error_type", "error"))
            DOWNSTREAM_REQUESTS.labels(downstream, getattr(exc, "error_type", "error")).inc()
            raise
        finally:
            DOWNSTREAM_DURATION.labels(downstream).observe(time.perf_counter() - started)
