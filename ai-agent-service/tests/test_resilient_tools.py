"""验证订单与工单 Tool 均经由统一韧性客户端。"""

import httpx

from tools.order_tools import OrderTools
from tools.ticket_tools import TicketTools


class FakeResilientClient:
    """记录 Tool 传给韧性层的调用参数。"""

    def __init__(self) -> None:
        self.calls = []

    def request_sync(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return httpx.Response(200, json={"ticketNo": "T1"}, request=httpx.Request(method, url))


def test_order_tool_uses_resilient_client_for_query():
    """订单查询应通过注入的 ResilientClient，而非直接 HTTP 调用。"""
    client = FakeResilientClient()
    tool = OrderTools(client=client)

    assert tool.query_order("EC202607130001", "token")["status"] == "success"
    assert client.calls[0][0] == "GET"
    assert client.calls[0][2]["headers"]["Authorization"] == "Bearer token"


def test_ticket_create_passes_idempotency_key_to_resilient_client():
    """建单写操作必须把稳定幂等键同时传给请求头和重试策略。"""
    client = FakeResilientClient()
    tool = TicketTools(client=client)

    assert tool.create_ticket({"title": "投诉", "idempotency_key": "create-1"}, "token")["status"] == "success"
    _, _, kwargs = client.calls[0]
    assert kwargs["idempotency_key"] == "create-1"
    assert kwargs["headers"]["Idempotency-Key"] == "create-1"
