"""订单 Tool：通过统一韧性客户端访问 Java 业务系统。"""

import os
import hashlib

from services.resilient_client import ResilienceError, ResilientClient
from services.cache_service import CacheService
from services.downstream_identity import build_business_headers, identity_cache_key


class OrderTools:
    """封装 Agent 查询订单数据的受控入口，所有下游调用均经过韧性层。"""

    def __init__(self, client: ResilientClient | None = None, cache: CacheService | None = None) -> None:
        """初始化业务地址与可注入客户端，方便生产隔离和单元测试。"""
        self.base_url = os.getenv("BUSINESS_SERVICE_URL", "http://localhost:8081")
        self.client = client or ResilientClient(downstream="java_order")
        self.cache = cache or CacheService(namespace="business")

    def query_order(self, order_no: str, auth_token: str | None = None) -> dict:
        """查询订单详情；查询类请求在网络和服务端错误时可安全重试。"""
        if not order_no:
            return {"status": "failed", "error": "order_no is required"}
        try:
            key = self.cache.key("order", customer=self._customer_key(auth_token), order_no=order_no)
            data = self.cache.get_or_load(key, int(os.getenv("ORDER_CACHE_TTL_SECONDS", "60")), lambda: self.client.request_sync("GET", f"{self.base_url}/api/orders/{order_no}", headers=self._headers(auth_token)).json(), metric="order_cache_hit")
            return {"status": "empty", "order_no": order_no} if not data else {"status": "success", "query_type": "order_detail", "order_no": order_no, "data": data}
        except ResilienceError as exc:
            return self._failure("order_detail", exc, order_no=order_no)

    def query_customer_orders(self, customer_id: int | str | None, auth_token: str | None = None) -> dict:
        """查询当前客户订单列表，不允许 Agent 直连业务数据库。"""
        if customer_id is None or str(customer_id).strip() == "":
            return {"status": "failed", "query_type": "customer_orders", "error": "customer_id is required"}
        try:
            key = self.cache.key("customer-orders", customer_id=customer_id)
            data = self.cache.get_or_load(key, int(os.getenv("ORDER_CACHE_TTL_SECONDS", "60")), lambda: self.client.request_sync("GET", f"{self.base_url}/api/orders", params={"customerId": customer_id}, headers=self._headers(auth_token)).json(), metric="order_cache_hit")
            return {"status": "empty", "query_type": "customer_orders", "customer_id": customer_id, "data": []} if not data else {"status": "success", "query_type": "customer_orders", "customer_id": customer_id, "data": data}
        except ResilienceError as exc:
            return self._failure("customer_orders", exc, customer_id=customer_id)

    def query_order_logistics(self, order_no: str, auth_token: str | None = None) -> dict:
        """查询物流轨迹；失败时返回标准错误而非让 Agent 编造物流事实。"""
        if not order_no:
            return {"status": "failed", "query_type": "order_logistics", "error": "order_no is required"}
        try:
            key = self.cache.key("logistics", customer=self._customer_key(auth_token), order_no=order_no)
            data = self.cache.get_or_load(key, int(os.getenv("LOGISTICS_CACHE_TTL_SECONDS", "45")), lambda: self.client.request_sync("GET", f"{self.base_url}/api/orders/{order_no}/logistics", headers=self._headers(auth_token)).json(), metric="order_cache_hit")
            return {"status": "empty", "query_type": "order_logistics", "order_no": order_no, "data": None} if not data else {"status": "success", "query_type": "order_logistics", "order_no": order_no, "data": data}
        except ResilienceError as exc:
            return self._failure("order_logistics", exc, order_no=order_no)

    @staticmethod
    def _headers(auth_token: str | None) -> dict[str, str]:
        """构造客户登录身份或 Worker 短期执行身份请求头。"""
        return build_business_headers(auth_token)

    @staticmethod
    def _customer_key(auth_token: str | None) -> str:
        """以不可逆 Token 摘要隔离缓存，避免直接把凭证写入 key。"""
        return hashlib.sha256(identity_cache_key(auth_token).encode()).hexdigest()[:16]

    @staticmethod
    def _failure(query_type: str, error: ResilienceError, **fields) -> dict:
        """向 Agent 返回可判定的下游失败摘要，不暴露原始网络异常。"""
        return {"status": "failed", "query_type": query_type, **fields, "error": error.error_type, "downstream": error.downstream, "retryable": error.retryable}
