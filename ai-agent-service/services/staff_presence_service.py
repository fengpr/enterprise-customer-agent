"""坐席工作台心跳与失联会话回收服务。"""

import os
from typing import Any


class StaffPresenceService:
    """使用 Redis TTL 记录真实工作台在线状态，不保存 Token 或客户数据。"""

    def __init__(self, client: Any | None = None, ttl_seconds: int = 30, grace_seconds: int = 60) -> None:
        self.ttl_seconds = ttl_seconds
        self.grace_seconds = max(grace_seconds, ttl_seconds)
        self.client = client if client is not None else self._create_client()

    @staticmethod
    def _create_client() -> Any | None:
        """创建短超时 Redis 客户端；不可用时按无人在线安全降级。"""
        url = os.getenv("REDIS_URL")
        if not url:
            return None
        try:
            import redis
            client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=0.3, socket_timeout=0.3)
            client.ping()
            return client
        except Exception:
            return None

    def heartbeat(self, staff_id: str) -> bool:
        """续期坐席在线状态。"""
        if not self.client:
            return False
        try:
            self.client.setex(self._key(staff_id), self.ttl_seconds, "1")
            # 宽限键用于失联回收，避免短暂网络抖动立即把活跃会话重新排队。
            self.client.setex(self._grace_key(staff_id), self.grace_seconds, "1")
            return True
        except Exception:
            return False

    def remove(self, staff_id: str) -> None:
        """坐席主动退出时立即清除心跳。"""
        if not self.client:
            return
        try:
            self.client.delete(self._key(staff_id), self._grace_key(staff_id))
        except Exception:
            return

    def is_online(self, staff_id: str) -> bool:
        """判断工作台心跳是否仍有效。"""
        if not self.client:
            return False
        try:
            return bool(self.client.exists(self._key(staff_id)))
        except Exception:
            return False

    def is_within_grace(self, staff_id: str) -> bool:
        """判断坐席是否仍处于失联宽限期，供后台回收任务使用。"""
        if not self.client:
            return False
        try:
            return bool(self.client.exists(self._grace_key(staff_id)))
        except Exception:
            return False

    @staticmethod
    def _key(staff_id: str) -> str:
        """Redis key 只包含内部坐席编号。"""
        return f"staff-presence:{staff_id}"

    @staticmethod
    def _grace_key(staff_id: str) -> str:
        """失联宽限键与在线判活键分离，保证接单判断和回收时限口径不同。"""
        return f"staff-presence-grace:{staff_id}"
