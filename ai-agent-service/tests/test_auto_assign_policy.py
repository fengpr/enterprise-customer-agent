"""验证 Agent 建单后的派单策略，防止高风险工单绕过调度队列。"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.runnables import RunnableLambda

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graphs.ticket_process_graph import build_ticket_process_graph, should_auto_assign_ticket
from schemas.intent_schema import IntentResult


def _analysis(**overrides):
    """构造测试用结构化意图，默认是可自动派单的低风险发票咨询。"""
    data = {
        "intent": "invoice",
        "user_goal": "info_query",
        "emotion": "normal",
        "order_related": False,
        "order_no": [],
        "product_name": None,
        "need_order_query": False,
        "need_ticket": True,
        "need_human": True,
        "priority": "medium",
        "confidence": 0.92,
        "summary": "发票问题",
        "risk_reasons": [],
        "action_type": None,
        "action_slots": {},
        "missing_slots": [],
        "next_action": "create_ticket",
    }
    data.update(overrides)
    return IntentResult(**data)


class AutoAssignPolicyTest(unittest.TestCase):
    """覆盖配置开关、低风险放行和高风险强制调度员确认。"""

    def test_unconfigured_auto_assign_is_disabled(self):
        """未配置开关时，Agent 默认不自动派单。"""
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(should_auto_assign_ticket({"analysis": _analysis()}))

    def test_false_auto_assign_is_disabled(self):
        """显式配置 false 时，Agent 不自动派单。"""
        with patch.dict(os.environ, {"AGENT_AUTO_ASSIGN_TICKET": "false"}, clear=True):
            self.assertFalse(should_auto_assign_ticket({"analysis": _analysis()}))

    def test_low_risk_can_auto_assign_when_enabled(self):
        """开启配置且命中低风险业务域时，允许自动派单。"""
        with patch.dict(os.environ, {"AGENT_AUTO_ASSIGN_TICKET": "true"}, clear=True):
            self.assertTrue(should_auto_assign_ticket({"analysis": _analysis()}))

    def test_high_risk_cases_never_auto_assign(self):
        """退货、退款、投诉、换货争议和赔付诉求即使开启配置也不得自动派单。"""
        high_risk_states = [
            _analysis(intent="refund", user_goal="action_request", action_type="return_goods", confidence=0.96),
            _analysis(intent="refund", user_goal="action_request", action_type="refund_request", confidence=0.96),
            _analysis(intent="complaint", user_goal="complaint", action_type="complaint_submit", confidence=0.96),
            _analysis(intent="exchange", user_goal="dispute", action_type="exchange_goods", confidence=0.96),
            _analysis(intent="refund", user_goal="complaint", risk_reasons=["compensation_claim"], confidence=0.96),
            _analysis(intent="invoice", emotion="dissatisfied", confidence=0.96),
            _analysis(intent="invoice", confidence=0.84),
        ]
        with patch.dict(os.environ, {"AGENT_AUTO_ASSIGN_TICKET": "true"}, clear=True):
            for analysis in high_risk_states:
                with self.subTest(intent=analysis.intent, goal=analysis.user_goal, action=analysis.action_type):
                    self.assertFalse(should_auto_assign_ticket({"analysis": analysis}))

    def test_create_ticket_keeps_pending_assign_when_auto_assign_disabled(self):
        """建单节点在默认配置下只创建 PENDING_ASSIGN 工单，不调用自动派单接口。"""
        assign_calls: list[str] = []

        def create_ticket(payload, auth_token):
            return {"status": "success", "data": {"ticketNo": "T1001", "status": "PENDING_ASSIGN"}}

        def auto_assign_ticket(ticket_no):
            assign_calls.append(ticket_no)
            return {"status": "success", "data": {"ticketNo": ticket_no, "status": "PENDING_PROCESS"}}

        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: _analysis(intent="complaint", user_goal="complaint", action_type="complaint_submit")),
            retrieve_knowledge=lambda _: [],
            query_order=lambda order_no, auth_token: {"status": "empty"},
            query_customer_orders=lambda customer_id, auth_token: {"status": "empty", "data": []},
            query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
            create_ticket=create_ticket,
            auto_assign_ticket=auto_assign_ticket,
            list_customer_tickets=lambda auth_token: {"status": "success", "data": []},
            query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
            urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
            prepare_action=lambda state: {"analysis": state["analysis"]},
            compose_answer=lambda state: "已提交",
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        with patch.dict(os.environ, {}, clear=True):
            result = graph.invoke({"message": "我要退货", "tool_results": [], "citations": []})

        self.assertEqual(assign_calls, [])
        self.assertEqual(result["ticket_result"]["data"]["status"], "PENDING_ASSIGN")

    def test_auto_assign_failure_keeps_created_ticket(self):
        """低风险自动派单接口异常时，不影响已创建工单继续留在待分派队列。"""

        def create_ticket(payload, auth_token):
            return {"status": "success", "data": {"ticketNo": "T1002", "status": "PENDING_ASSIGN"}}

        def auto_assign_ticket(ticket_no):
            raise RuntimeError("assign service unavailable")

        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: _analysis(intent="invoice", user_goal="info_query")),
            retrieve_knowledge=lambda _: [],
            query_order=lambda order_no, auth_token: {"status": "empty"},
            query_customer_orders=lambda customer_id, auth_token: {"status": "empty", "data": []},
            query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
            create_ticket=create_ticket,
            auto_assign_ticket=auto_assign_ticket,
            list_customer_tickets=lambda auth_token: {"status": "success", "data": []},
            query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
            urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
            prepare_action=lambda state: {"analysis": state["analysis"]},
            compose_answer=lambda state: "已提交",
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        with patch.dict(os.environ, {"AGENT_AUTO_ASSIGN_TICKET": "true"}, clear=True):
            result = graph.invoke({"message": "发票问题需要人工看一下", "tool_results": [], "citations": []})

        self.assertEqual(result["ticket_result"]["data"]["status"], "PENDING_ASSIGN")
        self.assertTrue(
            any(item.get("tool_name") == "auto_assign_ticket" and item.get("status") == "failed" for item in result["tool_results"])
        )


if __name__ == "__main__":
    unittest.main()
