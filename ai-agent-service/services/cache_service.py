"""Redis 容错缓存服务：统一 key、JSON、TTL 抖动、空值缓存和 singleflight。"""

import hashlib
import json
import os
import random
import threading
from collections import Counter
from typing import Any, Callable

from services.observability import CACHE


_EMPTY = {"__cache_empty__": True}


class CacheService:
    """缓存仅作加速层，Redis 故障始终回源且不影响在线请求。"""

    def __init__(self, client: Any | None = None, namespace: str = "agent-cache") -> None:
        self.namespace = namespace
        self.client = client if client is not None else self._create_client()
        self.metrics: Counter[str] = Counter()
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    @staticmethod
    def _create_client() -> Any | None:
        """按环境变量创建短超时 Redis 客户端；连接失败时禁用缓存。"""
        url = os.getenv("REDIS_URL")
        if not url:
            return None
        try:
            import redis
            client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=0.2, socket_timeout=0.2)
            client.ping()
            return client
        except Exception:
            return None

    def key(self, namespace: str, **parts: Any) -> str:
        """使用稳定 JSON 摘要规范化复杂 key，避免客户数据直接暴露在 Redis key 中。"""
        serialized = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode()).hexdigest()[:24]
        return f"{self.namespace}:{namespace}:{digest}"

    def get(self, key: str, metric: str = "cache_hit") -> Any | None:
        """读取并反序列化缓存；空值标记同样计为命中。"""
        if not self.client:
            return None
        try:
            raw = self.client.get(key)
            if raw is None:
                self.metrics[f"{metric}_miss"] += 1
                CACHE.labels(metric, "miss").inc()
                return None
            value = json.loads(raw)
            self.metrics[metric] += 1
            CACHE.labels(metric, "hit").inc()
            return None if value == _EMPTY else value
        except Exception:
            self.metrics["cache_error"] += 1
            CACHE.labels(metric, "error").inc()
            return None

    def set(self, key: str, value: Any, ttl_seconds: int, *, empty: bool = False) -> None:
        """写入 JSON 缓存并为雪崩保护增加小幅 TTL 抖动。"""
        if not self.client:
            return
        try:
            ttl = max(1, int(ttl_seconds + random.uniform(0, max(1, ttl_seconds * 0.1))))
            self.client.setex(key, ttl, json.dumps(_EMPTY if empty else value, ensure_ascii=False, default=str))
        except Exception:
            self.metrics["cache_error"] += 1

    def delete(self, *keys: str) -> None:
        """删除写操作影响的缓存；失败不影响业务写入结果。"""
        if not self.client or not keys:
            return
        try:
            self.client.delete(*keys)
        except Exception:
            self.metrics["cache_error"] += 1

    def get_or_load(self, key: str, ttl_seconds: int, loader: Callable[[], Any], *, metric: str, empty_ttl_seconds: int = 15) -> Any:
        """缓存未命中时用进程内 singleflight 抑制热点 key 同时回源。"""
        cached = self.get(key, metric)
        if cached is not None:
            return cached
        with self._lock_for(key):
            cached = self.get(key, metric)
            if cached is not None:
                return cached
            value = loader()
            self.set(key, value, empty_ttl_seconds if value in (None, {}, []) else ttl_seconds, empty=value in (None, {}, []))
            return value

    def snapshot_metrics(self) -> dict[str, int]:
        """返回轻量命中统计，供现有状态接口或日志读取。"""
        return dict(self.metrics)

    def _lock_for(self, key: str) -> threading.Lock:
        """按 key 创建短期 singleflight 锁。"""
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())
