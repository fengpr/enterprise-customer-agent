"""在线 Agent 下游调用的统一韧性封装。"""

import asyncio
import os
import queue
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx
from services.observability import observe_downstream


@dataclass
class ResilienceError(Exception):
    """下游调用失败的标准化错误，供上层按类型做安全降级。"""

    downstream: str
    error_type: str
    retryable: bool
    status_code: int | None = None
    safe_message: str = "下游服务暂时不可用，请稍后重试。"

    def __str__(self) -> str:
        """输出不含凭证与响应体的安全错误摘要。"""
        return f"{self.downstream}:{self.error_type}"


@dataclass
class _CircuitState:
    """维护单个下游服务的熔断状态。"""

    failures: int = 0
    opened_until: float = 0.0


class ResilientClient:
    """为 HTTP 与 LLM 调用提供超时、重试、熔断和并发舱壁。"""

    _circuits: dict[str, _CircuitState] = {}
    _circuit_lock = threading.Lock()
    _bulkheads: dict[str, threading.BoundedSemaphore] = {}
    _bulkhead_lock = threading.Lock()

    def __init__(
        self,
        *,
        downstream: str,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        total_timeout: float | None = None,
        max_retries: int | None = None,
        failure_threshold: int | None = None,
        recovery_seconds: float | None = None,
        max_concurrency: int | None = None,
        backoff_base: float | None = None,
    ) -> None:
        """读取可覆盖的环境配置；不同 downstream 共享各自熔断与舱壁。"""
        prefix = downstream.upper().replace("-", "_")
        self.downstream = downstream
        self.connect_timeout = connect_timeout if connect_timeout is not None else float(os.getenv(f"{prefix}_CONNECT_TIMEOUT", "2"))
        self.read_timeout = read_timeout if read_timeout is not None else float(os.getenv(f"{prefix}_READ_TIMEOUT", "8"))
        self.total_timeout = total_timeout if total_timeout is not None else float(os.getenv(f"{prefix}_TOTAL_TIMEOUT", "10"))
        self.max_retries = max_retries if max_retries is not None else int(os.getenv(f"{prefix}_MAX_RETRIES", "2"))
        self.failure_threshold = failure_threshold if failure_threshold is not None else int(os.getenv(f"{prefix}_CIRCUIT_FAILURE_THRESHOLD", "5"))
        self.recovery_seconds = recovery_seconds if recovery_seconds is not None else float(os.getenv(f"{prefix}_CIRCUIT_RECOVERY_SECONDS", "20"))
        self.max_concurrency = max_concurrency if max_concurrency is not None else int(os.getenv(f"{prefix}_MAX_CONCURRENCY", "12"))
        self.backoff_base = backoff_base if backoff_base is not None else float(os.getenv(f"{prefix}_BACKOFF_BASE", "0.15"))

    @classmethod
    def reset_state(cls) -> None:
        """重置进程内韧性状态，仅用于测试或受控运维恢复。"""
        with cls._circuit_lock, cls._bulkhead_lock:
            cls._circuits.clear()
            cls._bulkheads.clear()

    async def request(self, method: str, url: str, *, idempotency_key: str | None = None, **kwargs: Any) -> httpx.Response:
        """异步 HTTP 调用入口；非幂等写请求无幂等键时只执行一次。"""
        async def operation() -> httpx.Response:
            timeout = httpx.Timeout(self.total_timeout, connect=self.connect_timeout, read=self.read_timeout)
            async with httpx.AsyncClient(timeout=timeout) as client:
                return await client.request(method, url, **kwargs)

        return await self.invoke(operation, method=method, idempotency_key=idempotency_key)

    def request_sync(self, method: str, url: str, *, idempotency_key: str | None = None, **kwargs: Any) -> httpx.Response:
        """为现有同步 Agent 图提供适配入口，仍复用相同错误分类、熔断与舱壁。"""
        def operation() -> httpx.Response:
            timeout = httpx.Timeout(self.total_timeout, connect=self.connect_timeout, read=self.read_timeout)
            with httpx.Client(timeout=timeout) as client:
                return client.request(method, url, **kwargs)

        return self._invoke_sync(operation, method=method, idempotency_key=idempotency_key)

    async def invoke(self, operation: Callable[[], Awaitable[Any]], *, method: str = "GET", idempotency_key: str | None = None) -> Any:
        """包装任意异步下游调用，例如 LLM 的 ainvoke。"""
        semaphore = self._get_bulkhead()
        if not semaphore.acquire(blocking=False):
            raise self._error("circuit_open", retryable=True, safe_message="当前服务繁忙，请稍后重试。")
        try:
            self._check_circuit()
            attempts = self._attempts(method, idempotency_key)
            for attempt in range(attempts):
                try:
                    with observe_downstream(self.downstream):
                        result = await asyncio.wait_for(operation(), timeout=self.total_timeout)
                    self._raise_for_response(result)
                    self._record_success()
                    return result
                except ResilienceError as exc:
                    if not exc.retryable or attempt == attempts - 1:
                        self._record_failure(exc)
                        raise
                    await asyncio.sleep(self._backoff(attempt))
                except Exception as exc:
                    error = self._classify_exception(exc)
                    if not error.retryable or attempt == attempts - 1:
                        self._record_failure(error)
                        raise error from exc
                    await asyncio.sleep(self._backoff(attempt))
        finally:
            semaphore.release()

    def _invoke_sync(self, operation: Callable[[], Any], *, method: str, idempotency_key: str | None) -> Any:
        """同步适配器，供当前同步 Tool 和 FastAPI 同步路由调用。"""
        semaphore = self._get_bulkhead()
        if not semaphore.acquire(blocking=False):
            raise self._error("circuit_open", retryable=True, safe_message="当前服务繁忙，请稍后重试。")
        try:
            self._check_circuit()
            attempts = self._attempts(method, idempotency_key)
            for attempt in range(attempts):
                try:
                    with observe_downstream(self.downstream):
                        result = operation()
                    self._raise_for_response(result)
                    self._record_success()
                    return result
                except ResilienceError as exc:
                    if not exc.retryable or attempt == attempts - 1:
                        self._record_failure(exc)
                        raise
                    time.sleep(self._backoff(attempt))
                except Exception as exc:
                    error = self._classify_exception(exc)
                    if not error.retryable or attempt == attempts - 1:
                        self._record_failure(error)
                        raise error from exc
                    time.sleep(self._backoff(attempt))
        finally:
            semaphore.release()

    def _attempts(self, method: str, idempotency_key: str | None) -> int:
        """限制非幂等写操作重试，防止网络抖动造成重复建单或重复催办。"""
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and not idempotency_key:
            return 1
        return self.max_retries + 1

    def _check_circuit(self) -> None:
        """熔断窗口未结束时立即失败，保护已异常的下游。"""
        with self._circuit_lock:
            state = self._circuits.setdefault(self.downstream, _CircuitState())
            if state.opened_until > time.monotonic():
                raise self._error("circuit_open", retryable=True, safe_message="下游服务正在恢复，请稍后重试。")
            if state.opened_until:
                state.opened_until = 0.0
                state.failures = 0

    def _record_success(self) -> None:
        """成功响应关闭失败计数。"""
        with self._circuit_lock:
            self._circuits.setdefault(self.downstream, _CircuitState()).failures = 0

    def _record_failure(self, error: ResilienceError) -> None:
        """仅把可恢复的下游故障计入熔断，业务 4xx 不污染服务健康度。"""
        if not error.retryable:
            return
        with self._circuit_lock:
            state = self._circuits.setdefault(self.downstream, _CircuitState())
            state.failures += 1
            if state.failures >= self.failure_threshold:
                state.opened_until = time.monotonic() + self.recovery_seconds

    def _get_bulkhead(self) -> threading.BoundedSemaphore:
        """取得按下游隔离的并发舱壁。"""
        with self._bulkhead_lock:
            return self._bulkheads.setdefault(self.downstream, threading.BoundedSemaphore(self.max_concurrency))

    def _raise_for_response(self, result: Any) -> None:
        """将 HTTP 非成功状态转换为标准错误。"""
        if not isinstance(result, httpx.Response) or result.status_code < 400:
            return
        if result.status_code == 429:
            raise self._error("rate_limit_429", retryable=True, status_code=429)
        if result.status_code >= 500:
            raise self._error("5xx", retryable=True, status_code=result.status_code)
        raise self._error("4xx", retryable=False, status_code=result.status_code, safe_message="请求未被业务服务接受。")

    def _classify_exception(self, exc: Exception) -> ResilienceError:
        """将 HTTP 和 LLM SDK 异常归类为有限集合，避免上层依赖供应商异常。"""
        if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
            return self._error("timeout", retryable=True)
        if isinstance(exc, httpx.NetworkError):
            return self._error("network_error", retryable=True)
        status_code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
        if status_code == 429:
            return self._error("rate_limit_429", retryable=True, status_code=429)
        if isinstance(status_code, int) and status_code >= 500:
            return self._error("5xx", retryable=True, status_code=status_code)
        if isinstance(status_code, int) and status_code >= 400:
            return self._error("4xx", retryable=False, status_code=status_code)
        return self._error("network_error", retryable=True)

    def _error(self, error_type: str, *, retryable: bool, status_code: int | None = None, safe_message: str | None = None) -> ResilienceError:
        """构造不暴露凭证和原始响应体的标准错误。"""
        return ResilienceError(self.downstream, error_type, retryable, status_code, safe_message or "下游服务暂时不可用，请稍后重试。")

    def _backoff(self, attempt: int) -> float:
        """计算带极小随机扰动的指数退避，降低同批重试的拥塞放大。"""
        return self.backoff_base * (2 ** attempt) + random.uniform(0, self.backoff_base / 5)


class ResilientInvoker:
    """LLM 调用适配器，使用独立的 online_llm 下游隔离组。"""

    def __init__(self, client: ResilientClient | None = None) -> None:
        self.client = client or ResilientClient(downstream="online_llm")

    def invoke(self, operation: Callable[[], Any]) -> Any:
        """包装同步 LangChain invoke，统一纳入在线 LLM 韧性策略。"""
        return self.client._invoke_sync(operation, method="POST", idempotency_key="llm-inference")

    async def ainvoke(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        """包装异步 LangChain ainvoke，供未来异步 Agent 节点复用。"""
        return await self.client.invoke(operation, method="POST", idempotency_key="llm-inference")

    def stream(self, operation: Callable[[], Any]):
        """包装 LangChain 原生 stream，并为首个 token 与总生成时间设置看门狗。

        模型供应商或 SDK 可能在建立流式连接后永久不返回任何 chunk。若直接在
        Worker 线程中迭代该生成器，单个卡住的请求会占住唯一消费者，后续任务只能
        一直停在队列中。因此用守护线程承接阻塞迭代，并在主执行线程按时限失败，
        让上层队列能够安全重试或降级。
        """
        semaphore = self.client._get_bulkhead()
        if not semaphore.acquire(blocking=False):
            raise self.client._error("circuit_open", retryable=True, safe_message="当前服务繁忙，请稍后重试。")
        # 默认首 token 最多等待 15 秒，整次生成最多 60 秒；部署时可通过环境变量收紧。
        first_token_timeout = float(os.getenv("LLM_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", "15"))
        total_timeout = float(os.getenv("LLM_STREAM_TOTAL_TIMEOUT_SECONDS", "60"))
        messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        cancelled = threading.Event()

        def pump() -> None:
            """在守护线程中读取供应商生成器，避免阻塞 Agent Worker 主循环。"""
            iterator: Any | None = None
            try:
                iterator = iter(operation())
                while not cancelled.is_set():
                    chunk = next(iterator)
                    if cancelled.is_set():
                        return
                    messages.put(("chunk", chunk))
            except StopIteration:
                messages.put(("done", None))
            except Exception as exc:  # 供应商 SDK 异常交回主线程统一分类。
                messages.put(("error", exc))
            finally:
                # 部分 SDK 的生成器支持 close，可尽量释放超时后的网络资源。
                close = getattr(iterator, "close", None)
                if cancelled.is_set() and callable(close):
                    try:
                        close()
                    except Exception:
                        pass

        worker = threading.Thread(target=pump, name=f"llm-stream-{self.client.downstream}", daemon=True)
        try:
            self.client._check_circuit()
            worker.start()
            started_at = time.monotonic()
            received_first_chunk = False
            with observe_downstream(self.client.downstream):
                while True:
                    elapsed = time.monotonic() - started_at
                    remaining_total = total_timeout - elapsed
                    if remaining_total <= 0:
                        raise self.client._error(
                            "timeout",
                            retryable=True,
                            safe_message="模型响应超时，请稍后重试。",
                        )
                    # 首 token 前使用更短的等待窗口；之后仍以总时限防止长时间卡住。
                    wait_timeout = min(remaining_total, first_token_timeout if not received_first_chunk else remaining_total)
                    try:
                        event_type, payload = messages.get(timeout=wait_timeout)
                    except queue.Empty as exc:
                        raise self.client._error(
                            "timeout",
                            retryable=True,
                            safe_message="模型响应超时，请稍后重试。",
                        ) from exc
                    if event_type == "chunk":
                        received_first_chunk = True
                        yield payload
                        continue
                    if event_type == "done":
                        self.client._record_success()
                        return
                    raise payload
        except ResilienceError as exc:
            self.client._record_failure(exc)
            raise
        except Exception as exc:
            error = self.client._classify_exception(exc)
            self.client._record_failure(error)
            raise error from exc
        finally:
            cancelled.set()
            semaphore.release()
