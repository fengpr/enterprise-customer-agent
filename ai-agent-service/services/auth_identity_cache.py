"""登录身份短期缓存，为在线 Agent 降低重复 current-user 校验带来的 Java 访问延迟。"""

import hashlib
import os
from typing import Any, Callable

from services.cache_service import CacheService


class AuthIdentityCache:
    """使用 Token 不可逆摘要作为缓存维度，仅保留接口鉴权所需的最小身份字段。"""

    _ALLOWED_FIELDS = ("user_id", "customer_id", "display_name", "role")

    def __init__(self, cache: CacheService | None = None, ttl_seconds: int | None = None) -> None:
        """初始化身份缓存；TTL 默认 30 分钟，且不允许配置超过 30 分钟。"""
        configured_ttl = ttl_seconds if ttl_seconds is not None else int(os.getenv("AUTH_IDENTITY_CACHE_TTL_SECONDS", "1800"))
        self.ttl_seconds = min(max(int(configured_ttl), 1), 1800)
        self.cache = cache or CacheService(namespace="auth")

    def get_or_load(self, bearer_token: str, loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """优先从 Redis 获取身份；未命中时才回源 Java 校验并缓存脱敏后字段。"""
        token_digest = hashlib.sha256(bearer_token.encode("utf-8")).hexdigest()
        # CacheService 会再对 parts 做一次摘要，Redis key 不会出现 Token 或它的完整摘要。
        key = self.cache.key("current-user", token_hash=token_digest)
        return self.cache.get_or_load(
            key,
            self.ttl_seconds,
            lambda: self._sanitize_identity(loader()),
            metric="identity_cache_hit",
        )

    @classmethod
    def _sanitize_identity(cls, identity: dict[str, Any]) -> dict[str, Any]:
        """只保留前端与路由鉴权需要的身份字段，拒绝缓存密码、令牌或其他扩展信息。"""
        if not isinstance(identity, dict):
            raise ValueError("Java current-user 返回格式无效")
        if identity.get("user_id") is None or identity.get("role") is None:
            raise ValueError("Java current-user 缺少必要身份字段")
        return {field: identity.get(field) for field in cls._ALLOWED_FIELDS}
