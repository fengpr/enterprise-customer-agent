"""验证客户订单统计意图、真实工具查询与确定性金额聚合。"""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from langchain_core.runnables import RunnableLambda

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.customer_service_agent import CustomerServiceAgent
from agents.llm_intent_analyzer import LLMIntentAnalyzer
from graphs.ticket_process_graph import build_ticket_process_graph
from schemas.intent_schema import IntentResult, LLMIntentDraft


def _draft_result(**overrides) -> IntentResult:
    """构造一个会把统计请求误判为操作指引的模型结果。"""
    data = {
        "intent": "consult",
        "user_goal": "how_to",
        "emotion": "normal",
        "order_related": False,
        "order_no": [],
        "product_name": None,
        "need_order_query": False,
        "need_ticket": False,
        "need_human": False,
        "priority": "low",
        "confidence": 0.55,
        "summary": "引导客户查看订单",
        "risk_reasons": ["low_confidence"],
    }
    data.update(overrides)
    return IntentResult(**data)


class OrderStatisticsTest(unittest.TestCase):
    """覆盖订单统计的识别、时间范围、聚合与失败保护。"""

    def setUp(self) -> None:
        self.agent = CustomerServiceAgent.__new__(CustomerServiceAgent)

    def test_business_guardrail_forces_real_order_query(self):
        """原始问题即使被模型判成 how_to，也必须切换为真实订单查询。"""
        result = self.agent._apply_business_guardrails(
            "我最近买了哪些东西，一共有几件，总计花费多少",
            _draft_result(),
        )

        self.assertEqual(result.intent, "consult")
        self.assertEqual(result.user_goal, "info_query")
        self.assertTrue(result.order_related)
        self.assertTrue(result.need_order_query)
        self.assertFalse(result.need_human)
        self.assertNotIn("low_confidence", result.risk_reasons)

    def test_statistics_ignores_selected_single_order_context(self):
        """完整购买汇总不能被上一轮选中的单笔订单缩小查询范围。"""
        result = self.agent._apply_business_guardrails(
            "统计本月消费和商品件数",
            _draft_result(order_no=["EC202607160001"]),
        )

        contextual = self.agent._apply_context_guardrails(
            "统计本月消费和商品件数",
            result,
            {"selected_order_no": "EC202607160001"},
        )

        self.assertEqual(contextual.order_no, [])
        self.assertTrue(contextual.need_order_query)
        self.assertFalse(result.need_human)
        self.assertFalse(result.need_ticket)
        self.assertNotIn("low_confidence", result.risk_reasons)

    def test_llm_completion_also_normalizes_statistics_intent(self):
        """LLM 结构化结果在进入图之前同样需要修正统计查询标记。"""
        draft = LLMIntentDraft.model_validate(_draft_result().model_dump())
        analyzer = object.__new__(LLMIntentAnalyzer)

        result = analyzer._complete_intent_result("最近购买了什么，总共花了多少", draft)

        self.assertEqual(result.user_goal, "info_query")
        self.assertTrue(result.order_related)
        self.assertTrue(result.need_order_query)

    def test_aggregate_uses_quantity_and_decimal_amount(self):
        """同名商品应合并真实数量和订单实付金额，不能用订单数冒充件数。"""
        now = datetime.now()
        answer = self.agent._compose_order_statistics_answer(
            {
                "message": "我最近买了哪些东西，一共有几件，总计花费多少",
                "tool_results": [
                    {
                        "query_type": "customer_orders",
                        "status": "success",
                        "data": [
                            {"productName": "路由器", "quantity": 2, "amount": "798.10", "payTime": (now - timedelta(days=2)).isoformat()},
                            {"productName": "路由器", "quantity": 1, "amount": "399.20", "payTime": (now - timedelta(days=5)).isoformat()},
                            {"productName": "摄像头", "quantity": 3, "amount": "897.30", "payTime": (now - timedelta(days=10)).isoformat()},
                            {"productName": "未支付商品", "quantity": 9, "amount": "999.00", "payTime": None},
                        ],
                    }
                ],
            }
        )

        self.assertIn("3 笔已支付订单", answer)
        self.assertIn("购买 6 件商品", answer)
        self.assertIn("¥2094.60", answer)
        self.assertIn("路由器 × 3，小计 ¥1197.30", answer)
        self.assertNotIn("未支付商品 ×", answer)

    def test_explicit_time_ranges_override_default(self):
        """近 N 天、本月、今年和全部历史应产生稳定的统计边界。"""
        now = datetime(2026, 7, 16, 12, 0, 0)

        label, start, _ = self.agent._resolve_order_statistics_range("统计过去30天订单", now)
        self.assertEqual(label, "近 30 天内")
        self.assertEqual(start, now - timedelta(days=30))
        self.assertEqual(self.agent._resolve_order_statistics_range("统计本月消费", now)[1], datetime(2026, 7, 1))
        self.assertEqual(self.agent._resolve_order_statistics_range("统计今年花费", now)[1], datetime(2026, 1, 1))
        self.assertIsNone(self.agent._resolve_order_statistics_range("统计全部历史订单", now)[1])
        self.assertEqual(self.agent._resolve_order_statistics_range("最近买了什么", now)[1], now - timedelta(days=90))

    def test_invalid_amount_does_not_return_partial_total(self):
        """已支付订单金额异常时必须拒绝生成看似准确的总额。"""
        answer = self.agent._compose_order_statistics_answer(
            {
                "message": "最近总计花费多少",
                "tool_results": [
                    {
                        "query_type": "customer_orders",
                        "status": "success",
                        "data": [{"productName": "路由器", "quantity": 1, "amount": None, "payTime": datetime.now().isoformat()}],
                    }
                ],
            }
        )

        self.assertIn("无法准确计算总花费", answer)
        self.assertNotIn("实付合计", answer)

    def test_empty_and_failed_tools_have_safe_messages(self):
        """无订单和下游失败应明确说明结果，不能引导客户自行统计。"""
        empty_answer = self.agent._compose_order_statistics_answer(
            {"message": "统计近期消费", "tool_results": [{"query_type": "customer_orders", "status": "empty", "data": []}]}
        )
        failed_answer = self.agent._compose_order_statistics_answer(
            {"message": "统计近期消费", "tool_results": [{"query_type": "customer_orders", "status": "failed", "error": "timeout"}]}
        )

        self.assertIn("没有找到订单记录", empty_answer)
        self.assertIn("订单服务暂时不可用", failed_answer)
        self.assertNotIn("订单页", empty_answer + failed_answer)

    def test_more_than_ten_products_keeps_full_totals(self):
        """商品种类较多时只截断明细，汇总数字仍覆盖全部订单。"""
        now = datetime.now()
        orders = [
            {
                "productName": f"商品{i}",
                "quantity": 1,
                "amount": "10.00",
                "payTime": (now - timedelta(hours=i)).isoformat(),
            }
            for i in range(12)
        ]

        answer = self.agent._compose_order_statistics_answer(
            {"message": "统计近期消费", "tool_results": [{"query_type": "customer_orders", "status": "success", "data": orders}]}
        )

        self.assertIn("12 笔已支付订单", answer)
        self.assertIn("购买 12 件商品", answer)
        self.assertIn("¥120.00", answer)
        self.assertIn("另有 2 种商品", answer)

    def test_graph_queries_customer_orders_for_statistics(self):
        """统计意图在处理图中必须实际调用当前客户订单工具。"""
        analysis = self.agent._apply_business_guardrails(
            "我最近买了哪些东西，一共有几件，总计花费多少",
            _draft_result(),
        )
        calls: list[tuple[object, object]] = []
        now = datetime.now().isoformat()

        def query_customer_orders(customer_id, auth_token):
            calls.append((customer_id, auth_token))
            return {
                "query_type": "customer_orders",
                "status": "success",
                "data": [{"productName": "路由器", "quantity": 1, "amount": "399.00", "payTime": now}],
            }

        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: analysis),
            retrieve_knowledge=lambda _: [],
            query_order=lambda order_no, auth_token: {"status": "empty"},
            query_customer_orders=query_customer_orders,
            query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
            create_ticket=lambda payload, auth_token: {"status": "failed"},
            auto_assign_ticket=lambda ticket_no: {"status": "failed"},
            list_customer_tickets=lambda auth_token: {"status": "success", "data": []},
            query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
            urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
            prepare_action=lambda state: {"analysis": state["analysis"]},
            compose_answer=lambda state: self.agent._compose_answer(state),
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        result = graph.invoke(
            {
                "message": "我最近买了哪些东西，一共有几件，总计花费多少",
                "customer_id": 1,
                "auth_token": "verified-token",
                "tool_results": [],
                "citations": [],
            }
        )

        self.assertEqual(calls, [(1, "verified-token")])
        self.assertIn("¥399.00", result["answer"])


if __name__ == "__main__":
    unittest.main()
