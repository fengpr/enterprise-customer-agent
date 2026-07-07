import os

import httpx


class OrderTools:
    """订单工具，封装 Agent 查询 Java 业务系统订单数据的受控入口。"""

    def __init__(self) -> None:
        """读取业务服务地址，默认连接本地 Java 模拟系统。"""
        self.base_url = os.getenv("BUSINESS_SERVICE_URL", "http://localhost:8081")

    def query_order(self, order_no: str, auth_token: str | None = None) -> dict:
        """根据订单号查询订单详情，失败时返回结构化错误供 Agent 转人工。"""
        if not order_no:
            return {"status": "failed", "error": "order_no is required"}
        try:
            # 工具层设置短超时，防止业务系统异常拖慢客服回复链路。
            headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
            response = httpx.get(f"{self.base_url}/api/orders/{order_no}", headers=headers, timeout=3.0)
            response.raise_for_status()
            data = response.json()
            if not data:
                return {"status": "empty", "order_no": order_no}
            return {"status": "success", "query_type": "order_detail", "order_no": order_no, "data": data}
        except Exception as exc:
            return {"status": "failed", "query_type": "order_detail", "order_no": order_no, "error": str(exc)}

    def query_customer_orders(self, customer_id: int | str | None, auth_token: str | None = None) -> dict:
        """根据客户 ID 查询订单列表，用于用户只说“查询我的订单”但没有提供订单号的场景。"""
        if customer_id is None or str(customer_id).strip() == "":
            return {"status": "failed", "query_type": "customer_orders", "error": "customer_id is required"}
        try:
            # 客户订单列表只能通过受控业务接口读取，Agent 不直接访问业务数据库。
            headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
            response = httpx.get(
                f"{self.base_url}/api/orders",
                params={"customerId": customer_id},
                headers=headers,
                timeout=3.0,
            )
            response.raise_for_status()
            data = response.json()
            if not data:
                return {"status": "empty", "query_type": "customer_orders", "customer_id": customer_id, "data": []}
            return {"status": "success", "query_type": "customer_orders", "customer_id": customer_id, "data": data}
        except Exception as exc:
            return {
                "status": "failed",
                "query_type": "customer_orders",
                "customer_id": customer_id,
                "error": str(exc),
            }

    def query_order_logistics(self, order_no: str, auth_token: str | None = None) -> dict:
        """根据订单号查询完整物流轨迹，失败时返回结构化错误并禁止 Agent 编造路线。"""
        if not order_no:
            return {"status": "failed", "query_type": "order_logistics", "error": "order_no is required"}
        try:
            # 物流轨迹属于客户订单敏感信息，必须通过 Java 业务接口携带 Token 查询。
            headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
            response = httpx.get(f"{self.base_url}/api/orders/{order_no}/logistics", headers=headers, timeout=3.0)
            response.raise_for_status()
            data = response.json()
            if not data:
                return {"status": "empty", "query_type": "order_logistics", "order_no": order_no, "data": None}
            return {"status": "success", "query_type": "order_logistics", "order_no": order_no, "data": data}
        except Exception as exc:
            return {
                "status": "failed",
                "query_type": "order_logistics",
                "order_no": order_no,
                "error": str(exc),
            }
