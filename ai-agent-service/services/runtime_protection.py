"""在线 Agent 的限流、并发保护与轻量运行指标。

生产环境使用 Redis 共享限流状态；未配置 Redis 时仅允许开发环境使用进程内实现。
"""

import os
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field


@dataclass
class RuntimeMetrics:
    """保存进程级运行指标，并输出 Prometheus 文本格式。"""

    requests: Counter = field(default_factory=Counter)
    failures: Counter = field(default_factory=Counter)
    degraded: int = 0
    latencies: deque[float] = field(default_factory=lambda: deque(maxlen=10_000))
    lock: threading.Lock = field(default_factory=threading.Lock)

    def observe(self, path: str, status: int, latency_ms: float) -> None:
        """记录 HTTP 请求次数、失败次数和延迟样本。"""
        with self.lock:
            self.requests[(path, str(status))] += 1
            if status >= 500:
                self.failures[path] += 1
            self.latencies.append(latency_ms)

    def mark_degraded(self) -> None:
        """记录一次安全降级，供告警判断模型或队列压力。"""
        with self.lock:
            self.degraded += 1

    def prometheus(self) -> str:
        """返回无需额外依赖的 Prometheus exposition 格式指标。"""
        with self.lock:
            lines = ["# TYPE agent_http_requests_total counter"]
            lines.extend(
                f'agent_http_requests_total{{path="{path}",status="{status}"}} {count}'
                for (path, status), count in self.requests.items()
            )
            lines.append("# TYPE agent_degraded_total counter")
            lines.append(f"agent_degraded_total {self.degraded}")
            if self.latencies:
                ordered = sorted(self.latencies)
                p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
                lines.append("# TYPE agent_request_latency_p95_ms gauge")
                lines.append(f"agent_request_latency_p95_ms {p95:.2f}")
            return "\n".join(lines) + "\n"


class AdmissionController:
    """按用户、IP 和全局并发限制在线 LLM 请求，避免下游被突发流量打穿。"""

    def __init__(self) -> None:
        self.global_limit = int(os.getenv("AGENT_MAX_CONCURRENCY", "20"))
        self.per_subject_limit = int(os.getenv("AGENT_PER_SUBJECT_CONCURRENCY", "2"))
        self._global = threading.BoundedSemaphore(self.global_limit)
        self._subjects: Counter = Counter()
        self._lock = threading.Lock()
        self._redis = self._create_redis_client()

    @staticmethod
    def _create_redis_client():
        """按环境变量连接 Redis；未配置时保留开发环境进程内降级实现。"""
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            return None
        try:
            import redis
            client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
            client.ping()
            return client
        except Exception:
            # Redis 故障不应阻塞演示环境启动；生产探针会通过 metrics 和告警发现该问题。
            return None

    def try_acquire(self, subject: str) -> bool:
        """非阻塞获取执行槽位；达到阈值时立即让调用方走安全降级。"""
        if self._redis:
            return self._try_acquire_redis(subject)
        with self._lock:
            if self._subjects[subject] >= self.per_subject_limit:
                return False
            if not self._global.acquire(blocking=False):
                return False
            self._subjects[subject] += 1
            return True

    def release(self, subject: str) -> None:
        """释放请求执行槽位，保证异常路径不会永久占满并发配额。"""
        if self._redis:
            self._release_redis(subject)
            return
        with self._lock:
            if self._subjects[subject] > 0:
                self._subjects[subject] -= 1
                self._global.release()

    def _try_acquire_redis(self, subject: str) -> bool:
        """使用 Redis 计数器跨 Pod 共享并发配额，并以短 TTL 防止进程崩溃泄漏槽位。"""
        ttl = int(os.getenv("AGENT_ADMISSION_TTL_SECONDS", "90"))
        global_key, subject_key = "agent:admission:global", f"agent:admission:subject:{subject}"
        try:
            subject_count = int(self._redis.incr(subject_key))
            self._redis.expire(subject_key, ttl)
            if subject_count > self.per_subject_limit:
                self._redis.decr(subject_key)
                return False
            global_count = int(self._redis.incr(global_key))
            self._redis.expire(global_key, ttl)
            if global_count > self.global_limit:
                self._redis.decr(global_key)
                self._redis.decr(subject_key)
                return False
            return True
        except Exception:
            # Redis 短暂故障时拒绝放大流量，调用方统一安全降级。
            return False

    def _release_redis(self, subject: str) -> None:
        """归还 Redis 并发槽位，计数异常时由 TTL 自动兜底清理。"""
        try:
            self._redis.decr("agent:admission:global")
            self._redis.decr(f"agent:admission:subject:{subject}")
        except Exception:
            pass


metrics = RuntimeMetrics()
admission_controller = AdmissionController()
