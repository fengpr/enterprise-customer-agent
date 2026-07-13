"""身份缓存测试，验证令牌不落盘、命中时不重复调用 Java，缓存异常时可回源。"""

from services.auth_identity_cache import AuthIdentityCache
from services.cache_service import CacheService


class FakeRedis:
    """最小 Redis 替身，存储写入内容以验证不含敏感令牌。"""

    def __init__(self):
        self.values: dict[str, str] = {}

    def get(self, key):
        """返回模拟 Redis 字符串值。"""
        return self.values.get(key)

    def setex(self, key, _ttl, value):
        """记录缓存写入。"""
        self.values[key] = value


def test_identity_cache_uses_token_hash_and_minimal_identity_payload():
    """同一 Token 应命中缓存，且 Redis 不得包含令牌或不必要字段。"""
    redis = FakeRedis()
    cache = AuthIdentityCache(CacheService(redis, namespace="auth"), ttl_seconds=30)
    calls: list[int] = []

    def loader():
        calls.append(1)
        return {
            "user_id": 7,
            "customer_id": 8,
            "display_name": "测试客户",
            "role": "customer",
            "password": "must-not-cache",
            "access_token": "must-not-cache",
        }

    token = "customer-token-secret"
    first = cache.get_or_load(token, loader)
    second = cache.get_or_load(token, loader)

    assert first == second == {"user_id": 7, "customer_id": 8, "display_name": "测试客户", "role": "customer"}
    assert calls == [1]
    persisted = " ".join([*redis.values.keys(), *redis.values.values()])
    assert token not in persisted
    assert "must-not-cache" not in persisted


def test_identity_cache_uses_thirty_minute_ttl_cap_and_falls_back_when_redis_is_unavailable():
    """身份 TTL 不得超过 30 分钟；Redis 不可用时仍可回源校验。"""
    class BrokenRedis:
        """模拟缓存访问失败。"""

        def get(self, _key):
            raise RuntimeError("redis down")

        def setex(self, *_args):
            raise RuntimeError("redis down")

    cache = AuthIdentityCache(CacheService(BrokenRedis(), namespace="auth"), ttl_seconds=3600)

    assert cache.ttl_seconds == 1800
    assert cache.get_or_load("token", lambda: {"user_id": 1, "customer_id": 2, "display_name": "客户", "role": "customer"})["customer_id"] == 2


def test_current_login_user_routes_bearer_token_through_identity_cache(monkeypatch):
    """接口鉴权入口必须实际使用身份缓存，不能继续每次直连 Java。"""
    import app

    calls: list[str] = []

    class FakeIdentityCache:
        """记录 app 传入缓存组件的 Bearer Token；组件内部会对它做不可逆摘要。"""

        def get_or_load(self, token, loader):
            calls.append(token)
            return loader()

    class FakeResponse:
        """模拟 Java current-user 响应。"""

        @staticmethod
        def json():
            return {"user_id": 1, "customer_id": 2, "display_name": "客户", "role": "customer"}

    monkeypatch.setattr(app, "identity_cache", FakeIdentityCache())
    monkeypatch.setattr(app.business_client, "request_sync", lambda *_args, **_kwargs: FakeResponse())

    user = app._current_login_user("Bearer cache-test-token")

    assert calls == ["cache-test-token"]
    assert user["customer_id"] == 2
