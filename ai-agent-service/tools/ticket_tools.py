import os

import httpx


class TicketTools:
    """工单工具，封装 Agent 对 Java 业务系统的受控调用。"""

    def __init__(self) -> None:
        """读取业务服务地址和 Agent 内部密钥，便于不同环境切换。"""
        self.base_url = os.getenv("BUSINESS_SERVICE_URL", "http://localhost:8081")
        self.internal_secret = os.getenv("AGENT_INTERNAL_SECRET", "enterprise-customer-agent-demo-internal-secret")

    def create_ticket(self, payload: dict, auth_token: str | None = None) -> dict:
        """创建客服工单，写入动作必须携带客户 Token 让 Java 校验身份。"""
        try:
            # 工单创建是受控写入动作，客户身份以 Java Token 为准。
            headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
            response = httpx.post(f"{self.base_url}/api/tickets", json=payload, headers=headers, timeout=5.0)
            response.raise_for_status()
            return {"status": "success", "data": response.json()}
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}

    def list_customer_tickets(self, auth_token: str | None = None) -> dict:
        """查询当前客户工单列表，用于动作建单前做幂等检查。"""
        try:
            # 工单列表由 Java 根据 Token 限定当前客户，Agent 不信任前端 customer_id。
            headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
            response = httpx.get(f"{self.base_url}/api/tickets", headers=headers, timeout=5.0)
            response.raise_for_status()
            return {"status": "success", "data": response.json()}
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}

    def query_ticket_status(self, ticket_no: str, auth_token: str | None = None) -> dict:
        """查询当前客户自己的工单状态，供客户询问处理进度时使用。"""
        try:
            # 工单详情由 Java 校验客户归属，避免客户查询他人工单。
            headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
            response = httpx.get(f"{self.base_url}/api/tickets/{ticket_no}", headers=headers, timeout=5.0)
            response.raise_for_status()
            return {"status": "success", "query_type": "ticket_status", "data": response.json()}
        except Exception as exc:
            return {"status": "failed", "query_type": "ticket_status", "ticket_no": ticket_no, "error": str(exc)}

    def urge_ticket(self, ticket_no: str, reason: str | None = None, auth_token: str | None = None) -> dict:
        """客户催办自己的工单，Java 会落库催办记录并更新最近催办摘要。"""
        try:
            # 催办属于客户侧低风险写入动作，但仍必须由 Java 按 Token 校验工单归属。
            headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
            response = httpx.post(
                f"{self.base_url}/api/tickets/{ticket_no}/urge",
                json={"reason": reason or "客户催办处理进度"},
                headers=headers,
                timeout=5.0,
            )
            response.raise_for_status()
            return {"status": "success", "query_type": "ticket_urge", "data": response.json()}
        except Exception as exc:
            return {"status": "failed", "query_type": "ticket_urge", "ticket_no": ticket_no, "error": str(exc)}

    def auto_assign_ticket(self, ticket_no: str) -> dict:
        """Agent 建单成功后触发 Java 内部自动派单，最终处理人仍由业务系统规则决定。"""
        try:
            # 自动派单是内部受控动作，使用共享密钥而不是客户 Token 调用坐席接口。
            headers = {"X-Agent-Internal-Secret": self.internal_secret}
            response = httpx.post(
                f"{self.base_url}/api/internal/tickets/{ticket_no}/auto-assign",
                headers=headers,
                timeout=5.0,
            )
            response.raise_for_status()
            return {"status": "success", "data": response.json()}
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}
