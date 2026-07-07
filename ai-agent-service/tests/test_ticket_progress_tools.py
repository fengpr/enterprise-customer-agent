"""验证客户查询和催办工单时会走工单 Tool，而不是创建新工单。"""

import sys
import unittest
from pathlib import Path

from langchain_core.runnables import RunnableLambda

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graphs.ticket_process_graph import build_ticket_process_graph
from schemas.intent_schema import IntentResult


def _status_analysis():
    """构造工单状态查询识别结果。"""
    return IntentResult(
        intent="consult",
        user_goal="status_query",
        emotion="normal",
        order_related=False,
        order_no=[],
        product_name=None,
        need_order_query=False,
        need_ticket=False,
        need_human=False,
        priority="medium",
        confidence=0.9,
        summary="客户查询工单进度",
        risk_reasons=[],
    )


class TicketProgressToolTest(unittest.TestCase):
    """覆盖工单进度查询和客户催办的 LangGraph 工具路由。"""

    def _build_graph(self, calls: list[str]):
        """创建只保留工具路由能力的测试图。"""

        def query_ticket_status(ticket_no, auth_token):
            calls.append(f"query:{ticket_no}")
            return {"status": "success", "query_type": "ticket_status", "data": {"ticketNo": ticket_no, "status": "PENDING_ASSIGN"}}

        def urge_ticket(ticket_no, reason, auth_token):
            calls.append(f"urge:{ticket_no}")
            return {
                "status": "success",
                "query_type": "ticket_urge",
                "data": {"ticketNo": ticket_no, "status": "PENDING_ASSIGN", "urgeCount": 1},
            }

        return build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: _status_analysis()),
            retrieve_knowledge=lambda _: [],
            query_order=lambda order_no, auth_token: {"status": "empty"},
            query_customer_orders=lambda customer_id, auth_token: {"status": "empty", "data": []},
            query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
            create_ticket=lambda payload, auth_token: {"status": "failed"},
            auto_assign_ticket=lambda ticket_no: {"status": "failed"},
            list_customer_tickets=lambda auth_token: {"status": "success", "data": []},
            query_ticket_status=query_ticket_status,
            urge_ticket=urge_ticket,
            prepare_action=lambda state: {"analysis": state["analysis"]},
            compose_answer=lambda state: "ok",
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

    def test_ticket_status_query_uses_status_tool(self):
        """客户询问工单进度时，调用 query_ticket_status。"""
        calls: list[str] = []
        graph = self._build_graph(calls)

        result = graph.invoke({"message": "帮我看一下工单 T20260625095309625900 的进度", "tool_results": [], "citations": []})

        self.assertEqual(calls, ["query:T20260625095309625900"])
        self.assertEqual(result["tool_results"][0]["query_type"], "ticket_status")

    def test_ticket_urge_uses_urge_tool(self):
        """客户催办工单时，调用 urge_ticket 并不创建新工单。"""
        calls: list[str] = []
        graph = self._build_graph(calls)

        result = graph.invoke({"message": "帮我催一下工单 T20260625095309625900", "tool_results": [], "citations": []})

        self.assertEqual(calls, ["urge:T20260625095309625900"])
        self.assertEqual(result["tool_results"][0]["query_type"], "ticket_urge")
        self.assertIsNone(result["ticket_result"])


if __name__ == "__main__":
    unittest.main()
