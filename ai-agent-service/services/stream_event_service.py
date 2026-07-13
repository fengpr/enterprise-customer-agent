"""Agent SSE 事件缓冲服务，负责 Worker 与 API 间的安全事件传递。"""

import json
import os
import time
import uuid
from typing import Any


class StreamEventService:
    """使用 Redis Stream 保存可重放的客户可见 SSE 事件。"""

    _memory_events: dict[str, list[dict[str, Any]]] = {}

    def __init__(self, redis_client: Any | None = None) -> None:
        """初始化 Redis；不可用时仅保留当前进程内缓冲，主任务仍可继续。"""
        self._redis = redis_client
        self.ttl_seconds = int(os.getenv("AGENT_STREAM_EVENT_TTL_SECONDS", "1200"))
        if self._redis is None:
            redis_url = os.getenv("REDIS_URL")
            if redis_url:
                try:
                    import redis
                    self._redis = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=2)
                    self._redis.ping()
                except Exception:
                    self._redis = None

    def publish(self, request_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """写入一个客户可见事件，禁止把异常、凭证或工具原始结果带入缓冲。"""
        event = {
            "request_id": request_id,
            "event_type": event_type,
            "timestamp": int(time.time() * 1000),
            "payload": self._safe_payload(payload or {}),
        }
        if self._redis:
            try:
                event_id = self._redis.xadd(self._key(request_id), {"event": json.dumps(event, ensure_ascii=False)})
                event["event_id"] = str(event_id)
                self._redis.expire(self._key(request_id), self.ttl_seconds)
                return event
            except Exception:
                # 事件缓存失败不能影响已在 Worker 中执行的客户任务。
                pass
        event["event_id"] = f"local-{uuid.uuid4().hex}"
        self._memory_events.setdefault(request_id, []).append(event)
        return event

    def replay(self, request_id: str, last_event_id: str | None = None) -> list[dict[str, Any]]:
        """按 Last-Event-ID 返回严格后续事件，避免重连时重复拼接 token。"""
        if self._redis:
            try:
                # Redis 5 不支持 XRANGE 的开区间起点语法，先包含读取后再在客户端过滤已收到事件。
                start = last_event_id if last_event_id and not last_event_id.startswith("local-") else "-"
                items = self._redis.xrange(self._key(request_id), min=start, max="+")
                result: list[dict[str, Any]] = []
                for event_id, fields in items:
                    if last_event_id and str(event_id) == last_event_id:
                        continue
                    event = json.loads(fields["event"])
                    event["event_id"] = str(event_id)
                    result.append(event)
                return result
            except Exception:
                pass
        events = list(self._memory_events.get(request_id, []))
        if not last_event_id:
            return events
        # 本地降级 ID 是随机值，不能按字符串大小比较；按缓冲写入顺序补发即可。
        for index, event in enumerate(events):
            if event["event_id"] == last_event_id:
                return events[index + 1:]
        return events

    @staticmethod
    def to_sse(event: dict[str, Any]) -> str:
        """编码标准 SSE 帧，事件 ID 供浏览器断线续传使用。"""
        return f"id: {event['event_id']}\nevent: {event['event_type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    @staticmethod
    def _key(request_id: str) -> str:
        """生成按请求隔离的 Redis Stream Key。"""
        return f"agent:stream:events:{request_id}"

    @staticmethod
    def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """删除可能泄露内部链路、凭证和异常栈的字段。"""
        forbidden = {"authorization", "auth_token", "execution_credential", "tool_results", "internal_suggestion", "traceback", "exception", "prompt"}
        return {key: value for key, value in payload.items() if key.lower() not in forbidden}
