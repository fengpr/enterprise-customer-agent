"""工单 Tool：通过统一韧性客户端访问 Java 业务系统。"""

import os
import uuid
import hashlib

from services.resilient_client import ResilienceError, ResilientClient
from services.cache_service import CacheService
from services.downstream_identity import build_business_headers, identity_cache_key


class TicketTools:
    """封装工单查询和受控写操作，写操作仅在携带幂等键时允许重试。"""

    def __init__(self, client: ResilientClient | None = None, cache: CacheService | None = None) -> None:
        self.base_url = os.getenv("BUSINESS_SERVICE_URL", "http://localhost:8081")
        self.internal_secret = os.getenv("AGENT_INTERNAL_SECRET", "enterprise-customer-agent-demo-internal-secret")
        self.client = client or ResilientClient(downstream="java_ticket")
        self.cache = cache or CacheService(namespace="business")

    def create_ticket(self, payload: dict, auth_token: str | None = None) -> dict:
        """创建工单，生成并透传幂等键后才允许网络重试。"""
        key = str(payload.get("idempotency_key") or payload.get("idempotencyKey") or uuid.uuid4())
        try:
            response = self.client.request_sync("POST", f"{self.base_url}/api/tickets", json=payload, headers=self._headers(auth_token, key), idempotency_key=key)
            data = response.json(); self._invalidate_ticket(auth_token, data.get("ticketNo")); return {"status": "success", "data": data}
        except ResilienceError as exc:
            return self._failure(exc)

    def append_ticket_information(
        self,
        ticket_no: str,
        payload: dict,
        auth_token: str | None = None,
    ) -> dict:
        """向既有工单追加客户补充信息；Java 根据工单和取件状态决定是否直接更新履约偏好。"""
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        if not idempotency_key:
            return {
                "status": "failed",
                "error": "missing_idempotency_key",
                "retryable": False,
            }
        body = {
            "content": payload.get("content"),
            "afterSaleReason": payload.get("afterSaleReason"),
            "returnMethod": payload.get("returnMethod"),
            "pickupTimeWindow": payload.get("pickupTimeWindow"),
        }
        try:
            response = self.client.request_sync(
                "POST",
                f"{self.base_url}/api/tickets/{ticket_no}/supplements",
                json=body,
                headers=self._headers(auth_token, idempotency_key),
                idempotency_key=idempotency_key,
            )
            data = response.json()
            self._invalidate_ticket(auth_token, ticket_no)
            return {"status": "success", "query_type": "ticket_supplement", "data": data}
        except ResilienceError as exc:
            return {
                **self._failure(exc),
                "query_type": "ticket_supplement",
                "ticket_no": ticket_no,
            }

    def list_customer_tickets(self, auth_token: str | None = None) -> dict:
        """查询当前客户工单列表，属于可重试只读操作。"""
        try:
            key = self.cache.key("ticket-list", customer=self._customer_key(auth_token))
            return {"status": "success", "data": self.cache.get_or_load(key, int(os.getenv("TICKET_CACHE_TTL_SECONDS", "60")), lambda: self.client.request_sync("GET", f"{self.base_url}/api/tickets", headers=self._headers(auth_token)).json(), metric="ticket_cache_hit")}
        except ResilienceError as exc:
            return self._failure(exc)

    def query_ticket_status(self, ticket_no: str, auth_token: str | None = None) -> dict:
        """查询工单状态，Java 负责客户归属权限校验。"""
        try:
            key = self._ticket_key(auth_token, ticket_no)
            data = self.cache.get_or_load(key, int(os.getenv("TICKET_CACHE_TTL_SECONDS", "60")), lambda: self.client.request_sync("GET", f"{self.base_url}/api/tickets/{ticket_no}", headers=self._headers(auth_token)).json(), metric="ticket_cache_hit")
            return {"status": "success", "query_type": "ticket_status", "data": data}
        except ResilienceError as exc:
            return {**self._failure(exc), "query_type": "ticket_status", "ticket_no": ticket_no}

    def urge_ticket(self, ticket_no: str, reason: str | None = None, auth_token: str | None = None, idempotency_key: str | None = None) -> dict:
        """催办为写操作，调用方未提供幂等键时禁止自动重试。"""
        try:
            response = self.client.request_sync("POST", f"{self.base_url}/api/tickets/{ticket_no}/urge", json={"reason": reason or "客户催办处理进度"}, headers=self._headers(auth_token, idempotency_key), idempotency_key=idempotency_key)
            data = response.json(); self._invalidate_ticket(auth_token, ticket_no); return {"status": "success", "query_type": "ticket_urge", "data": data}
        except ResilienceError as exc:
            return {**self._failure(exc), "query_type": "ticket_urge", "ticket_no": ticket_no}

    def auto_assign_ticket(self, ticket_no: str, idempotency_key: str | None = None) -> dict:
        """内部自动派单同样遵守幂等键重试约束。"""
        try:
            response = self.client.request_sync("POST", f"{self.base_url}/api/internal/tickets/{ticket_no}/auto-assign", headers={"X-Agent-Internal-Secret": self.internal_secret, **({"Idempotency-Key": idempotency_key} if idempotency_key else {})}, idempotency_key=idempotency_key)
            return {"status": "success", "data": response.json()}
        except ResilienceError as exc:
            return self._failure(exc)

    @staticmethod
    def _headers(auth_token: str | None, idempotency_key: str | None = None) -> dict[str, str]:
        """构造客户登录身份或 Worker 短期执行身份及可选幂等键请求头。"""
        headers = build_business_headers(auth_token)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    @staticmethod
    def _failure(error: ResilienceError) -> dict:
        """将统一异常投影为 Agent 可消费的工具失败结果。"""
        return {"status": "failed", "error": error.error_type, "downstream": error.downstream, "retryable": error.retryable}

    @staticmethod
    def _customer_key(auth_token: str | None) -> str:
        """使用 Token 摘要隔离客户工单缓存。"""
        return hashlib.sha256(identity_cache_key(auth_token).encode()).hexdigest()[:16]

    def _ticket_key(self, auth_token: str | None, ticket_no: str | None) -> str:
        """构造客户与工单共同参与的详情缓存 key。"""
        return self.cache.key("ticket", customer=self._customer_key(auth_token), ticket_no=ticket_no or "")

    def _invalidate_ticket(self, auth_token: str | None, ticket_no: str | None) -> None:
        """写成功后删除详情和列表缓存，读缓存不参与写路径。"""
        self.cache.delete(self._ticket_key(auth_token, ticket_no), self.cache.key("ticket-list", customer=self._customer_key(auth_token)))
