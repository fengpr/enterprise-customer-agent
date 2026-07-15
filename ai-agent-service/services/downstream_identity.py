"""Worker 调用 Java 业务服务时使用的短期内部身份封装。"""

from __future__ import annotations

from services.observability import current_context


_EXECUTION_PREFIX = "agent-execution:"


def build_execution_identity(customer_id: int, request_id: str, credential: str) -> str:
    """把队列中的安全字段组合为仅在 Worker 内存中使用的执行身份。

    该值不是客户登录 Token；它只用于让下游工具构造签名凭证请求头，
    不会重新写入 Redis、Trace、日志或前端响应。
    """
    return f"{_EXECUTION_PREFIX}{customer_id}:{request_id}:{credential}"


def build_business_headers(identity: str | None) -> dict[str, str]:
    """根据客户 Token 或内部执行身份构造 Java 请求头。

    在线直连兼容路径继续转发客户 Bearer Token；独立 Worker 只发送短期签名凭证、
    客户 ID 和请求 ID，避免在可靠队列中保存原始 Authorization。
    """
    context = current_context()
    headers = {
        "X-Request-ID": context["request_id"],
        "X-Trace-ID": context["trace_id"],
    }
    if not identity:
        return headers
    if not identity.startswith(_EXECUTION_PREFIX):
        headers["Authorization"] = f"Bearer {identity}"
        return headers

    encoded = identity.removeprefix(_EXECUTION_PREFIX)
    try:
        customer_id, request_id, credential = encoded.split(":", 2)
    except ValueError:
        # 结构损坏时不回退为 Authorization，确保内部凭证不会被误当成客户 Token。
        return headers
    if not customer_id.isdigit() or not request_id or not credential:
        return headers
    headers.update(
        {
            "X-Agent-Customer-ID": customer_id,
            "X-Agent-Execution-Credential": credential,
            "X-Request-ID": request_id,
        }
    )
    return headers


def identity_cache_key(identity: str | None) -> str:
    """返回缓存隔离所需的稳定身份材料，不对外暴露具体凭证。"""
    return identity or "anonymous"
