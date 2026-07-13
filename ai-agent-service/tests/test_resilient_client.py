"""覆盖统一下游韧性层的重试、熔断、舱壁与错误分类。"""

import asyncio
import time

import httpx
import pytest

from services.resilient_client import ResilienceError, ResilientClient, ResilientInvoker


@pytest.fixture(autouse=True)
def reset_resilience_state():
    """每个用例隔离进程内熔断器和舱壁状态。"""
    ResilientClient.reset_state()


def _response(status_code: int) -> httpx.Response:
    """构造带请求上下文的 HTTP 响应。"""
    return httpx.Response(status_code, request=httpx.Request("GET", "http://downstream.test"))


def test_successful_async_operation_returns_result():
    """成功调用应直接返回结果。"""
    client = ResilientClient(downstream="test_success", backoff_base=0)

    async def operation():
        return _response(200)

    assert asyncio.run(client.invoke(operation)).status_code == 200


def test_timeout_is_retried_and_classified():
    """超时应按重试次数执行后以 timeout 标准错误返回。"""
    client = ResilientClient(downstream="test_timeout", max_retries=1, backoff_base=0)
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ResilienceError, match="timeout") as exc_info:
        asyncio.run(client.invoke(operation))
    assert calls == 2
    assert exc_info.value.error_type == "timeout"


@pytest.mark.parametrize("status_code", [429, 503])
def test_rate_limit_and_5xx_are_retried(status_code: int):
    """429 与 5xx 均属于可恢复失败，后续成功时应返回成功响应。"""
    client = ResilientClient(downstream=f"test_retry_{status_code}", max_retries=2, backoff_base=0)
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        return _response(status_code if calls == 1 else 200)

    assert asyncio.run(client.invoke(operation)).status_code == 200
    assert calls == 2


def test_4xx_is_not_retried():
    """业务 4xx 不应因网络策略重复提交请求。"""
    client = ResilientClient(downstream="test_4xx", max_retries=3, backoff_base=0)
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        return _response(400)

    with pytest.raises(ResilienceError) as exc_info:
        asyncio.run(client.invoke(operation))
    assert calls == 1
    assert exc_info.value.error_type == "4xx"


def test_circuit_opens_after_configured_failures():
    """连续可恢复失败达到阈值后，后续调用应被熔断器直接拒绝。"""
    client = ResilientClient(downstream="test_circuit", max_retries=0, failure_threshold=2, recovery_seconds=30)

    async def failure():
        return _response(503)

    for _ in range(2):
        with pytest.raises(ResilienceError):
            asyncio.run(client.invoke(failure))
    with pytest.raises(ResilienceError) as exc_info:
        asyncio.run(client.invoke(failure))
    assert exc_info.value.error_type == "circuit_open"


def test_bulkhead_rejects_when_downstream_is_full():
    """占满同一下游的舱壁后，新调用不能无限等待。"""
    client = ResilientClient(downstream="test_bulkhead", max_concurrency=1)
    semaphore = client._get_bulkhead()
    assert semaphore.acquire(blocking=False)

    async def success():
        return _response(200)

    try:
        with pytest.raises(ResilienceError) as exc_info:
            asyncio.run(client.invoke(success))
        assert exc_info.value.error_type == "circuit_open"
    finally:
        semaphore.release()


def test_non_idempotent_write_does_not_retry_without_key():
    """没有 Idempotency-Key 的写操作即便 5xx 也必须只尝试一次。"""
    client = ResilientClient(downstream="test_write", max_retries=3, backoff_base=0)
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        return _response(503)

    with pytest.raises(ResilienceError):
        client._invoke_sync(operation, method="POST", idempotency_key=None)
    assert calls == 1


def test_stream_returns_chunks_without_waiting_for_complete_answer():
    """原生流式生成应逐块透传，而不是等完整回答后一次性返回。"""
    invoker = ResilientInvoker(ResilientClient(downstream="test_stream_success", max_retries=0))

    assert list(invoker.stream(lambda: iter(["退货", "规则"])) ) == ["退货", "规则"]


def test_stream_first_token_timeout_releases_worker(monkeypatch):
    """模型长期不返回首个 token 时必须标准化超时，防止 Worker 被永久占用。"""
    monkeypatch.setenv("LLM_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", "0.02")
    monkeypatch.setenv("LLM_STREAM_TOTAL_TIMEOUT_SECONDS", "0.2")
    invoker = ResilientInvoker(ResilientClient(downstream="test_stream_first_timeout", max_retries=0))

    def stalled_stream():
        time.sleep(0.2)
        yield "不应返回"

    with pytest.raises(ResilienceError) as exc_info:
        list(invoker.stream(stalled_stream))
    assert exc_info.value.error_type == "timeout"


def test_stream_total_timeout_after_first_chunk(monkeypatch):
    """已收到首个 token 后仍须受总时限保护，避免生成中途永久卡住。"""
    monkeypatch.setenv("LLM_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("LLM_STREAM_TOTAL_TIMEOUT_SECONDS", "0.03")
    invoker = ResilientInvoker(ResilientClient(downstream="test_stream_total_timeout", max_retries=0))

    def slow_stream():
        yield "首段"
        time.sleep(0.2)
        yield "不应返回"

    generator = invoker.stream(slow_stream)
    assert next(generator) == "首段"
    with pytest.raises(ResilienceError) as exc_info:
        next(generator)
    assert exc_info.value.error_type == "timeout"
