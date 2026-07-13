import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.runnables import RunnableLambda

from agents.action_request import enrich_action_analysis, infer_action_type
from agents.customer_service_agent import CustomerServiceAgent
from graphs.ticket_process_graph import build_ticket_process_graph
from schemas.intent_schema import Citation, IntentResult


class _FakeLogRepository:
    """测试用日志仓储，避免单测依赖真实数据库。"""

    def save(self, tool_name, input_data, output_data):
        """返回内存日志结构，便于断言调用链路。"""
        return {"tool_name": tool_name, "input_data": input_data, "output_data": output_data}


class _FakeReplyLLM:
    """测试用回复模型，记录 payload 并返回固定自然话术。"""

    def __init__(self, answer: str | None = None, exc: Exception | None = None):
        """配置模型返回内容或异常，用来覆盖成功和失败分支。"""
        self.answer = answer or "退货一般需要在规则允许的售后时效内提交申请，并保持商品完好、不影响二次销售；最终是否通过以售后审核结果为准。"
        self.exc = exc
        self.payloads: list[dict] = []

    def generate_customer_reply(self, payload):
        """模拟 LLM 生成客户侧回复。"""
        self.payloads.append(payload)
        if self.exc:
            raise self.exc
        return self.answer


class ReturnGoodsPolicyGuardrailTest(unittest.TestCase):
    """验证退货规则咨询不会被误判成真实售后动作或混入维修政策。"""

    def test_return_goods_policy_query_stays_policy_consult(self):
        """“查看退货规则”应稳定识别为退货规则咨询，不创建工单、不转人工。"""
        agent = object.__new__(CustomerServiceAgent)
        draft = IntentResult(
            intent="other",
            user_goal="other",
            emotion="normal",
            order_related=False,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=True,
            need_human=True,
            priority="high",
            confidence=0.55,
            summary="查看退货规则",
            risk_reasons=["low_confidence"],
        )

        result = agent._apply_business_guardrails("查看退货规则", draft)

        self.assertEqual(result.intent, "refund")
        self.assertEqual(result.user_goal, "policy_consult")
        self.assertEqual(result.action_type, "return_goods")
        self.assertFalse(result.need_human)
        self.assertFalse(result.need_ticket)
        self.assertFalse(result.need_order_query)
        self.assertFalse(result.order_related)
        self.assertNotIn("low_confidence", result.risk_reasons)

    def test_return_goods_policy_uses_rule_fast_path_before_intent_llm(self):
        """明确的退货规则问法应跳过意图 LLM，缩短首个流式 token 前的等待。"""
        class UnexpectedIntentLLM:
            """若快路径失效则立即让测试失败。"""

            def invoke(self, _message):
                raise AssertionError("退货规则不应调用意图 LLM")

        agent = object.__new__(CustomerServiceAgent)
        agent.llm_analyzer = UnexpectedIntentLLM()
        agent.log_repository = _FakeLogRepository()
        agent.call_logs = []

        result = agent._analyze_with_llm_fallback({"message": "查询退货规则", "conversation_context": None})

        self.assertEqual(result.intent, "refund")
        self.assertEqual(result.user_goal, "policy_consult")
        self.assertEqual(result.action_type, "return_goods")
        self.assertFalse(result.need_human)

    def test_order_status_query_can_use_order_context(self):
        """只有明确询问具体订单能否退货时，才进入订单状态查询。"""
        agent = object.__new__(CustomerServiceAgent)
        draft = IntentResult(
            intent="other",
            user_goal="other",
            emotion="normal",
            order_related=False,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="low",
            confidence=0.6,
            summary="我的订单能退吗",
            risk_reasons=[],
        )

        result = agent._apply_business_guardrails("我的订单能退吗", draft)

        self.assertEqual(result.intent, "refund")
        self.assertEqual(result.user_goal, "status_query")
        self.assertEqual(result.action_type, "return_goods")
        self.assertTrue(result.order_related)
        self.assertTrue(result.need_order_query)
        self.assertFalse(result.need_ticket)

    def test_policy_consult_does_not_enter_action_slots(self):
        """退货规则咨询即使带有 return_goods 标记，也不能进入退货申请槽位流程。"""
        analysis = IntentResult(
            intent="refund",
            user_goal="policy_consult",
            emotion="normal",
            order_related=False,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="medium",
            confidence=0.9,
            summary="查看退货规则",
            risk_reasons=[],
            action_type="return_goods",
        )

        normalized, pending = enrich_action_analysis(
            analysis,
            message="查看退货规则",
            selected_order_no="EC202606220001",
            pending_action_request=None,
            conversation_context=None,
        )

        self.assertEqual(normalized.user_goal, "policy_consult")
        self.assertEqual(normalized.action_type, "return_goods")
        self.assertEqual(normalized.action_slots, {})
        self.assertEqual(normalized.missing_slots, [])
        self.assertIsNone(normalized.next_action)
        self.assertIsNone(pending)

    def test_policy_consult_with_selected_order_does_not_query_order(self):
        """纯退货规则咨询即使前端选中订单，也不能调用订单查询接口。"""
        calls: list[str] = []
        analysis = IntentResult(
            intent="refund",
            user_goal="policy_consult",
            emotion="normal",
            order_related=True,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="medium",
            confidence=0.9,
            summary="查看退货规则",
            risk_reasons=[],
            action_type="return_goods",
        )

        def query_order(order_no, auth_token):
            calls.append(order_no)
            return {"status": "success", "data": {"orderNo": order_no}}

        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: analysis),
            retrieve_knowledge=lambda _: [
                Citation(
                    doc_name="Return Policy",
                    version="V1",
                    paragraph="签收后 7 天内且商品不影响二次销售时，可申请退货；是否通过以售后审核结果为准。",
                    score=0.9,
                    collection="return_policy",
                    business_scope="return_goods",
                    heading_path=["退货规则"],
                    risk_level="medium",
                    answerable_intents=["refund", "consult"],
                )
            ],
            query_order=query_order,
            query_customer_orders=lambda customer_id, auth_token: {"status": "empty", "data": []},
            query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
            create_ticket=lambda payload, auth_token: {"status": "failed"},
            auto_assign_ticket=lambda ticket_no: {"status": "failed"},
            list_customer_tickets=lambda auth_token: {"status": "success", "data": []},
            query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
            urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
            prepare_action=lambda state: {"analysis": state["analysis"]},
            compose_answer=lambda state: "ok",
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        graph.invoke(
            {
                "message": "查看退货规则",
                "selected_order_no": "EC202606220001",
                "tool_results": [],
                "citations": [],
            }
        )

        self.assertEqual(calls, [])

    def test_return_goods_policy_phrases_are_not_action_requests(self):
        """退货规则、条件和流程类问题不能被动作类型推断为退货申请。"""
        analysis = IntentResult(
            intent="refund",
            user_goal="policy_consult",
            emotion="normal",
            order_related=False,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="medium",
            confidence=0.9,
            summary="退货规则咨询",
            risk_reasons=[],
            action_type="return_goods",
        )

        for message in ["退货规则是什么", "退货条件", "七天无理由", "怎么申请退货"]:
            with self.subTest(message=message):
                self.assertIsNone(infer_action_type(message, analysis))

    def test_explicit_return_goods_request_still_collects_slots(self):
        """用户明确要求办理退货时，仍进入动作闭环并追问必要槽位。"""
        analysis = IntentResult(
            intent="refund",
            user_goal="action_request",
            emotion="normal",
            order_related=True,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="medium",
            confidence=0.9,
            summary="我要退货",
            risk_reasons=[],
            action_type="return_goods",
        )

        normalized, pending = enrich_action_analysis(
            analysis,
            message="我要退货",
            selected_order_no=None,
            pending_action_request=None,
            conversation_context=None,
        )

        self.assertEqual(normalized.user_goal, "action_request")
        self.assertEqual(normalized.action_type, "return_goods")
        self.assertEqual(normalized.next_action, "collect_slots")
        self.assertIn("order_no", normalized.missing_slots)
        self.assertIn("after_sale_reason", normalized.missing_slots)
        self.assertIsNotNone(pending)

    def test_policy_consult_cancels_old_return_goods_pending(self):
        """旧退货申请 pending 不能劫持新的退货规则咨询。"""
        pending = {
            "pending_id": "PA001",
            "status": "collecting",
            "action_type": "return_goods",
            "action_slots": {"order_no": "EC202606220001"},
            "missing_slots": ["after_sale_reason"],
            "next_action": "collect_slots",
            "completed": False,
        }
        analysis = IntentResult(
            intent="refund",
            user_goal="policy_consult",
            emotion="normal",
            order_related=False,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="medium",
            confidence=0.9,
            summary="查看退货规则",
            risk_reasons=[],
            action_type="return_goods",
        )

        normalized, new_pending = enrich_action_analysis(
            analysis,
            message="查看退货规则",
            selected_order_no=None,
            pending_action_request=pending,
            conversation_context=None,
        )

        self.assertEqual(normalized.user_goal, "policy_consult")
        self.assertEqual(normalized.missing_slots, [])
        self.assertIsNone(normalized.next_action)
        self.assertEqual(new_pending["status"], "cancelled")
        self.assertTrue(new_pending["completed"])
        self.assertEqual(new_pending["cancel_reason"], "non_action_intent")

    def test_return_goods_answer_removes_repair_sentences(self):
        """退货规则回复后校验会过滤保修、维修、质保等无关业务句子。"""
        answer = (
            "根据退货规则，签收后 7 天内且商品不影响二次销售时，可申请退货。"
            "如果涉及维修，请确认是否在保修期内。"
            "最终是否通过以售后审核结果为准。"
        )

        sanitized = CustomerServiceAgent._sanitize_return_goods_answer(answer)

        self.assertIn("可申请退货", sanitized)
        self.assertIn("售后审核结果", sanitized)
        self.assertNotIn("维修", sanitized)
        self.assertNotIn("保修期", sanitized)

    def test_return_goods_answer_skips_kb_metadata_chunks(self):
        """退货规则回复不能把适用范围、典型表达等知识库管理说明展示给客户。"""
        metadata_citation = Citation(
            doc_name="return_goods_policy",
            version="V1.1",
            paragraph=(
                "本文档适用于客户咨询退货条件、退货流程等问题。\n"
                "适合回答的典型表达包括：\n- “我想退货。”\n- “这个商品能退吗？”"
            ),
            score=0.9,
            collection="return_policy",
            business_scope="return_goods",
            heading_path=["退货条件政策", "适用范围"],
            risk_level="medium",
            answerable_intents=["refund", "consult"],
        )
        rule_citation = Citation(
            doc_name="return_goods_policy",
            version="V1.1",
            paragraph="客户申请退货通常需要存在有效订单，订单处于可售后范围内，商品类型支持退货，商品状态符合售后要求，并按平台流程提交申请。",
            score=0.86,
            collection="return_policy",
            business_scope="return_goods",
            heading_path=["退货条件政策", "客户可见规则"],
            risk_level="medium",
            answerable_intents=["refund", "consult"],
        )

        self.assertFalse(CustomerServiceAgent._is_return_goods_citation(metadata_citation))
        self.assertTrue(CustomerServiceAgent._is_return_goods_citation(rule_citation))
        policy_text = CustomerServiceAgent._return_goods_policy_text([metadata_citation, rule_citation])

        self.assertIn("有效订单", policy_text)
        self.assertNotIn("适合回答", policy_text)
        self.assertNotIn("我想退货", policy_text)

    def test_return_goods_policy_prefers_llm_with_clean_payload(self):
        """LLM 可用时，退货规则咨询应基于清洗后的事实生成自然话术，且不携带订单上下文。"""
        fake_llm = _FakeReplyLLM()
        agent = object.__new__(CustomerServiceAgent)
        agent.llm_analyzer = fake_llm
        agent.log_repository = _FakeLogRepository()
        agent.call_logs = []
        analysis = IntentResult(
            intent="refund",
            user_goal="policy_consult",
            emotion="normal",
            order_related=False,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="medium",
            confidence=0.9,
            summary="查看退货规则",
            risk_reasons=[],
            action_type="return_goods",
        )
        citations = [
            Citation(
                doc_name="return_goods_policy",
                version="V1.1",
                paragraph=(
                    "本文档适用于客户咨询退货条件、退货流程等问题。\n"
                    "适合回答的典型表达包括：\n- “我想退货。”\n- “这个商品能退吗？”"
                ),
                score=0.9,
                collection="return_policy",
                business_scope="return_goods",
                heading_path=["退货规则", "适用范围"],
                risk_level="medium",
                answerable_intents=["refund", "consult"],
            ),
            Citation(
                doc_name="return_goods_policy",
                version="V1.1",
                paragraph="签收后 7 天内且商品完好、不影响二次销售时，可在订单售后页面提交退货申请；最终是否通过以售后审核结果为准。",
                score=0.86,
                collection="return_policy",
                business_scope="return_goods",
                heading_path=["退货规则", "客户可见规则"],
                risk_level="medium",
                answerable_intents=["refund", "consult"],
            ),
        ]

        answer = agent._compose_return_goods_policy_answer(
            {
                "message": "查看退货规则",
                "analysis": analysis,
                "citations": citations,
                "tool_results": [
                    {"query_type": "order", "status": "success", "data": {"orderNo": "EC202606220001"}}
                ],
                "selected_order_no": "EC202606220001",
            },
            citations,
        )

        self.assertEqual(answer, fake_llm.answer)
        self.assertEqual(len(fake_llm.payloads), 1)
        payload = fake_llm.payloads[0]
        self.assertIsNone(payload["order"])
        self.assertIn("签收后 7 天内", payload["citations"][0]["paragraph"])
        self.assertIn("不影响二次销售", payload["citations"][0]["paragraph"])
        self.assertIn("售后审核结果为准", payload["citations"][0]["paragraph"])
        self.assertNotIn("适用范围", payload["citations"][0]["paragraph"])
        self.assertNotIn("典型表达", payload["citations"][0]["paragraph"])
        self.assertNotIn("我想退货", payload["citations"][0]["paragraph"])
        self.assertNotIn("EC202606220001", payload["service_instruction"])

    def test_return_goods_policy_falls_back_when_llm_fails(self):
        """LLM 失败时应回退模板，并继续避免输出知识库元信息和当前订单号。"""
        agent = object.__new__(CustomerServiceAgent)
        agent.llm_analyzer = _FakeReplyLLM(exc=RuntimeError("llm down"))
        agent.log_repository = _FakeLogRepository()
        agent.call_logs = []
        analysis = IntentResult(
            intent="refund",
            user_goal="policy_consult",
            emotion="normal",
            order_related=False,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="medium",
            confidence=0.9,
            summary="查看退货规则",
            risk_reasons=[],
            action_type="return_goods",
        )
        citations = [
            Citation(
                doc_name="return_goods_policy",
                version="V1.1",
                paragraph="签收后 7 天内且商品完好、不影响二次销售时，可在订单售后页面提交退货申请；最终是否通过以售后审核结果为准。",
                score=0.86,
                collection="return_policy",
                business_scope="return_goods",
                heading_path=["退货规则", "客户可见规则"],
                risk_level="medium",
                answerable_intents=["refund", "consult"],
            )
        ]

        answer = agent._compose_return_goods_policy_answer(
            {
                "message": "查看退货规则",
                "analysis": analysis,
                "citations": citations,
                "tool_results": [
                    {"query_type": "order", "status": "success", "data": {"orderNo": "EC202606220001"}}
                ],
                "selected_order_no": "EC202606220001",
            },
            citations,
        )

        self.assertIn("根据退货规则", answer)
        self.assertIn("最终是否通过以售后审核结果为准", answer)
        self.assertNotIn("适用范围", answer)
        self.assertNotIn("典型表达", answer)
        self.assertNotIn("EC202606220001", answer)

    def test_return_goods_policy_rejects_false_no_policy_llm_answer(self):
        """已召回明确退货规则时，模型不得误报“知识库未找到规则”。"""
        agent = object.__new__(CustomerServiceAgent)
        agent.llm_analyzer = _FakeReplyLLM(answer="当前知识库未找到明确退货规则。")
        agent.log_repository = _FakeLogRepository()
        agent.call_logs = []
        analysis = IntentResult(
            intent="refund",
            user_goal="policy_consult",
            emotion="normal",
            order_related=False,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="medium",
            confidence=0.9,
            summary="查询退货规则",
            risk_reasons=[],
            action_type="return_goods",
        )
        citations = [
            Citation(
                doc_name="return_goods_policy",
                version="V1.1",
                paragraph="签收后 7 天内且商品完好、不影响二次销售时，可在订单售后页面提交退货申请；最终是否通过以售后审核结果为准。",
                score=0.86,
                collection="return_policy",
                business_scope="return_goods",
                heading_path=["退货规则", "客户可见规则"],
                risk_level="medium",
                answerable_intents=["refund", "consult"],
            )
        ]

        answer = agent._compose_return_goods_policy_answer(
            {
                "message": "查询退货规则",
                "analysis": analysis,
                "citations": citations,
                "tool_results": [],
            },
            citations,
        )

        self.assertIn("签收后 7 天内", answer)
        self.assertIn("最终是否通过以售后审核结果为准", answer)
        self.assertNotIn("当前知识库未找到明确退货规则", answer)

    def test_return_goods_policy_rejects_order_followup_llm_answer(self):
        """纯退货规则咨询不能接受索要订单号或建议人工核实的 LLM 回复。"""
        llm_answer = (
            "您好，目前您咨询的是退货规则，系统中暂未找到具体的时效、流程等详细说明。"
            "不过根据通用规则，退货通常需要结合商品本身规则和订单实际状态来判断，"
            "建议您提供订单号，我帮您进一步核实后操作。"
        )
        agent = object.__new__(CustomerServiceAgent)
        agent.llm_analyzer = _FakeReplyLLM(answer=llm_answer)
        agent.log_repository = _FakeLogRepository()
        agent.call_logs = []
        analysis = IntentResult(
            intent="refund",
            user_goal="policy_consult",
            emotion="normal",
            order_related=False,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="medium",
            confidence=0.9,
            summary="查看退货规则",
            risk_reasons=[],
            action_type="return_goods",
        )
        citations = [
            Citation(
                doc_name="return_goods_policy",
                version="V1.1",
                paragraph="签收后 7 天内且商品不影响二次销售时，可申请退货；是否通过以售后审核结果为准。",
                score=0.86,
                collection="return_policy",
                business_scope="return_goods",
                heading_path=["退货规则", "客户可见规则"],
                risk_level="medium",
                answerable_intents=["refund", "consult"],
            )
        ]

        answer = agent._compose_return_goods_policy_answer(
            {
                "message": "查看退货规则",
                "analysis": analysis,
                "citations": citations,
                "tool_results": [],
            },
            citations,
        )

        self.assertIn("签收后 7 天内", answer)
        self.assertIn("售后审核结果为准", answer)
        self.assertNotIn("提供订单号", answer)
        self.assertNotIn("人工客服", answer)
        self.assertNotIn("进一步核实后操作", answer)

    def test_return_goods_policy_weak_policy_text_uses_generic_fallback(self):
        """只有内部约束而没有具体规则事实时，兜底话术不能复述“结合订单状态”等弱文本。"""
        weak_policy_text = "退货需要结合具体商品规则和订单状态来判断，无法直接承诺一定可以退货。"

        answer = CustomerServiceAgent._return_goods_policy_fallback_answer(weak_policy_text)

        self.assertIn("平台规定的售后时效", answer)
        self.assertIn("不影响二次销售", answer)
        self.assertNotIn("具体商品规则和订单状态", answer)
        self.assertNotIn("无法直接承诺", answer)


if __name__ == "__main__":
    unittest.main()
