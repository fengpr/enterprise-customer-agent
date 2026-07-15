import sys
import unittest
from datetime import datetime, timedelta
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

    def test_selected_order_bare_return_goods_is_action_request(self):
        """已选订单上的单发“退货”应发起售后动作，不能退化为规则咨询。"""
        policy_draft = IntentResult(
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
            summary="退货",
            risk_reasons=[],
            action_type="return_goods",
        )

        normalized, pending = enrich_action_analysis(
            policy_draft,
            message="退货",
            selected_order_no="EC202606220001",
            pending_action_request=None,
        )

        self.assertEqual(normalized.user_goal, "action_request")
        self.assertEqual(normalized.action_type, "return_goods")
        self.assertEqual(normalized.action_slots["order_no"], "EC202606220001")
        self.assertEqual(normalized.next_action, "collect_slots")
        self.assertEqual(normalized.missing_slots, ["after_sale_reason", "return_method"])
        self.assertEqual(pending["status"], "waiting_for_user_input")

    def test_unselected_bare_return_goods_remains_policy_consult(self):
        """没有当前订单时，单发“退货”不能擅自绑定历史订单或直接建单。"""
        policy_draft = IntentResult(
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
            summary="退货",
            risk_reasons=[],
            action_type="return_goods",
        )

        normalized, pending = enrich_action_analysis(
            policy_draft,
            message="退货",
            selected_order_no=None,
            pending_action_request=None,
        )

        self.assertEqual(normalized.user_goal, "policy_consult")
        self.assertIsNone(pending)

    def test_quality_reason_fills_return_goods_pending_not_complaint(self):
        """退货申请追问原因后，商品质量问题应作为退货原因，而不是改判成投诉。"""
        pending = {
            "pending_id": "PA-RETURN-REASON",
            "status": "collecting",
            "action_type": "return_goods",
            "action_slots": {"order_no": "EC202606220001"},
            "missing_slots": ["after_sale_reason"],
            "next_action": "collect_slots",
            "completed": False,
        }
        analysis = IntentResult(
            intent="complaint",
            user_goal="complaint",
            emotion="normal",
            order_related=True,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=True,
            need_human=True,
            priority="high",
            confidence=0.9,
            summary="商品质量问题",
            risk_reasons=["complaint"],
            action_type="complaint_submit",
        )

        normalized, new_pending = enrich_action_analysis(
            analysis,
            message="商品质量问题",
            selected_order_no=None,
            pending_action_request=pending,
            conversation_context=None,
        )

        self.assertEqual(normalized.user_goal, "action_request")
        self.assertEqual(normalized.intent, "refund")
        self.assertEqual(normalized.action_type, "return_goods")
        self.assertEqual(normalized.action_slots.get("order_no"), "EC202606220001")
        self.assertEqual(normalized.action_slots.get("after_sale_reason"), "商品质量问题")
        self.assertEqual(normalized.next_action, "collect_slots")
        self.assertEqual(normalized.missing_slots, ["return_method"])
        self.assertNotIn("complaint", normalized.risk_reasons)
        self.assertEqual((new_pending or {}).get("status"), "waiting_for_user_input")

    def test_new_return_pending_contains_session_slot_state(self):
        """新退货流程应保存可审计的待完成动作字段，并用 return_reason 表示客户待补槽位。"""
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
            confidence=0.95,
            summary="我要退货",
            risk_reasons=[],
            action_type="return_goods",
        )

        normalized, pending = enrich_action_analysis(
            analysis,
            message="我要退货",
            selected_order_no="EC202606220001",
            pending_action_request=None,
        )

        self.assertEqual(normalized.missing_slots, ["after_sale_reason", "return_method"])
        self.assertEqual(pending["status"], "waiting_for_user_input")
        self.assertEqual(pending["issue_type"], "return")
        self.assertEqual(pending["order_no"], "EC202606220001")
        self.assertEqual(pending["missing_slots"], ["return_reason", "return_method"])
        self.assertEqual(pending["collected_slots"]["order_no"], "EC202606220001")
        self.assertTrue(pending["created_at"])
        self.assertTrue(pending["updated_at"])
        self.assertTrue(pending["expires_at"])

    def test_short_wrong_topic_draft_fills_return_reason(self):
        """“拍错了”即使被模型判成越界，也必须优先填充当前退货槽位，不能取消流程。"""
        now = datetime.utcnow()
        pending = {
            "pending_id": "PA-SHORT-REASON",
            "status": "waiting_for_user_input",
            "action_type": "return_goods",
            "action_slots": {"order_no": "EC202606220001"},
            "missing_slots": ["return_reason"],
            "next_action": "collect_slots",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=30)).isoformat(),
            "completed": False,
        }
        analysis = IntentResult(
            intent="other",
            user_goal="out_of_scope",
            emotion="normal",
            order_related=False,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="low",
            confidence=0.55,
            summary="拍错了",
            risk_reasons=[],
        )

        normalized, new_pending = enrich_action_analysis(
            analysis,
            message="拍错了",
            selected_order_no=None,
            pending_action_request=pending,
        )

        self.assertEqual(normalized.user_goal, "action_request")
        self.assertEqual(normalized.action_type, "return_goods")
        self.assertEqual(normalized.action_slots["after_sale_reason"], "拍错了")
        self.assertEqual(normalized.next_action, "collect_slots")
        self.assertEqual(normalized.missing_slots, ["return_method"])
        self.assertEqual(new_pending["collected_slots"]["return_reason"], "拍错了")
        self.assertNotEqual(new_pending["status"], "cancelled")

    def test_current_selected_order_overrides_old_pending_order(self):
        """前端本轮明确选中的订单优先于旧 pending，补充原因时不得继续绑定旧订单。"""
        pending = {
            "pending_id": "PA-OLD-ORDER",
            "status": "waiting_for_user_input",
            "action_type": "return_goods",
            "action_slots": {"order_no": "EC202606220001"},
            "missing_slots": ["return_reason"],
            "updated_at": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(minutes=30)).isoformat(),
            "completed": False,
        }
        analysis = IntentResult(
            intent="complaint",
            user_goal="complaint",
            emotion="normal",
            order_related=True,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=True,
            need_human=True,
            priority="high",
            confidence=0.9,
            summary="商品质量问题",
            risk_reasons=["complaint"],
            action_type="complaint_submit",
        )

        normalized, new_pending = enrich_action_analysis(
            analysis,
            message="商品质量问题",
            selected_order_no="EC202606220002",
            pending_action_request=pending,
            conversation_context={"debug_context": {}},
        )

        self.assertEqual(normalized.action_type, "return_goods")
        self.assertEqual(normalized.action_slots["order_no"], "EC202606220002")
        self.assertEqual(new_pending["order_no"], "EC202606220002")
        self.assertNotEqual(new_pending["pending_id"], pending["pending_id"])

    def test_stale_pending_reason_requires_confirmation_before_execution(self):
        """5-30 分钟的退货流程先确认续办，确认前不能查单或创建工单。"""
        now = datetime.utcnow()
        pending = {
            "pending_id": "PA-STALE-RETURN",
            "status": "waiting_for_user_input",
            "action_type": "return_goods",
            "action_slots": {"order_no": "EC202606220001"},
            "missing_slots": ["return_reason"],
            "created_at": (now - timedelta(minutes=20)).isoformat(),
            "updated_at": (now - timedelta(minutes=20)).isoformat(),
            "expires_at": (now + timedelta(minutes=10)).isoformat(),
            "completed": False,
        }
        analysis = IntentResult(
            intent="complaint",
            user_goal="complaint",
            emotion="normal",
            order_related=True,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=True,
            need_human=True,
            priority="high",
            confidence=0.9,
            summary="商品质量问题",
            risk_reasons=["complaint"],
            action_type="complaint_submit",
        )

        waiting, waiting_pending = enrich_action_analysis(
            analysis,
            message="商品质量问题",
            selected_order_no=None,
            pending_action_request=pending,
        )
        self.assertEqual(waiting.next_action, "collect_slots")
        self.assertEqual(waiting.missing_slots, ["pending_confirmation"])
        self.assertFalse(waiting.need_order_query)
        self.assertFalse(waiting.need_ticket)
        self.assertEqual(waiting_pending["status"], "awaiting_confirmation")
        agent = object.__new__(CustomerServiceAgent)
        agent.llm_analyzer = None
        confirmation_answer = agent._compose_answer(
            {
                "message": "商品质量问题",
                "analysis": waiting,
                "pending_action_request": waiting_pending,
                "tool_results": [],
                "citations": [],
            }
        )
        self.assertIn("是否继续", confirmation_answer)
        self.assertNotIn("已收到您的质量问题", confirmation_answer)

        resumed, resumed_pending = enrich_action_analysis(
            analysis,
            message="是的",
            selected_order_no=None,
            pending_action_request=waiting_pending,
        )
        self.assertEqual(resumed.action_type, "return_goods")
        self.assertEqual(resumed.action_slots["after_sale_reason"], "商品质量问题")
        self.assertEqual(resumed.next_action, "collect_slots")
        self.assertEqual(resumed.missing_slots, ["return_method"])
        self.assertFalse(resumed.need_ticket)
        self.assertEqual(resumed_pending["status"], "waiting_for_user_input")

    def test_expired_pending_does_not_reuse_old_order(self):
        """超过 30 分钟后只能重新确认诉求，不能继承旧订单直接执行退货。"""
        now = datetime.utcnow()
        expired = {
            "pending_id": "PA-EXPIRED-RETURN",
            "status": "waiting_for_user_input",
            "action_type": "return_goods",
            "action_slots": {"order_no": "EC202606220001"},
            "missing_slots": ["return_reason"],
            "created_at": (now - timedelta(minutes=31)).isoformat(),
            "updated_at": (now - timedelta(minutes=31)).isoformat(),
            "expires_at": (now - timedelta(minutes=1)).isoformat(),
            "completed": False,
        }
        analysis = IntentResult(
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
            summary="商品质量问题",
            risk_reasons=[],
        )

        normalized, restarted = enrich_action_analysis(
            analysis,
            message="商品质量问题",
            selected_order_no=None,
            pending_action_request=expired,
        )

        self.assertEqual(normalized.missing_slots, ["action_confirmation"])
        self.assertEqual(restarted["status"], "awaiting_confirmation")
        self.assertNotIn("order_no", restarted["action_slots"])
        self.assertNotEqual(restarted["pending_id"], expired["pending_id"])

    def test_yes_after_restart_binds_followup_order_confirmation(self):
        """超时重启后的两次“是”应依次确认流程和候选订单，不能丢失上一轮确认状态。"""
        pending = {
            "pending_id": "PA-RESTART-CONFIRM",
            "status": "awaiting_confirmation",
            "action_type": "return_goods",
            "issue_type": "return",
            "action_slots": {},
            "missing_slots": ["action_confirmation"],
            "next_action": "collect_slots",
            "updated_at": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(minutes=30)).isoformat(),
            "completed": False,
        }
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
            confidence=0.55,
            summary="确认",
            risk_reasons=[],
        )
        agent = object.__new__(CustomerServiceAgent)
        agent.llm_analyzer = None

        def prepare_action(state):
            normalized, new_pending = enrich_action_analysis(
                state["analysis"],
                message=state["message"],
                selected_order_no=state.get("selected_order_no"),
                pending_action_request=state.get("pending_action_request"),
                conversation_context=state.get("conversation_context"),
            )
            return {"analysis": normalized, "pending_action_request": new_pending}

        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: draft),
            retrieve_knowledge=lambda _: [],
            query_order=lambda order_no, auth_token: {"status": "success", "query_type": "order_detail", "data": {"orderNo": order_no}},
            query_customer_orders=lambda customer_id, auth_token: {
                "status": "success",
                "query_type": "customer_orders",
                "data": [{"orderNo": "EC202606220001", "productName": "Smart Router AX3000"}],
            },
            query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
            create_ticket=lambda payload, auth_token: self.fail("确认订单前不应创建工单"),
            auto_assign_ticket=lambda ticket_no: {"status": "failed"},
            list_customer_tickets=lambda auth_token: {"status": "success", "data": []},
            query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
            urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
            prepare_action=prepare_action,
            compose_answer=lambda state: agent._compose_answer(state),
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        first_confirm = graph.invoke(
            {
                "message": "是",
                "customer_id": 1,
                "session_id": "S-RESTART",
                "pending_action_request": pending,
                "tool_results": [],
                "citations": [],
            }
        )
        self.assertEqual(first_confirm["analysis"].missing_slots, ["order_confirmation"])
        self.assertEqual(
            first_confirm["pending_action_request"]["action_slots"]["candidate_order_no"],
            "EC202606220001",
        )
        self.assertIn("请确认您要办理的是订单 EC202606220001", first_confirm["answer"])

        second_confirm, continued = enrich_action_analysis(
            draft,
            message="是的",
            selected_order_no=None,
            pending_action_request=first_confirm["pending_action_request"],
        )
        self.assertEqual(second_confirm.action_type, "return_goods")
        self.assertEqual(second_confirm.action_slots["order_no"], "EC202606220001")
        self.assertEqual(second_confirm.missing_slots, ["after_sale_reason", "return_method"])
        self.assertEqual(continued["missing_slots"], ["return_reason", "return_method"])

        second_turn = graph.invoke(
            {
                "message": "是的",
                "customer_id": 1,
                "session_id": "S-RESTART",
                "pending_action_request": first_confirm["pending_action_request"],
                "tool_results": [],
                "citations": [],
            }
        )
        self.assertEqual(second_turn["analysis"].missing_slots, ["after_sale_reason", "return_method"])
        self.assertIn("退货原因", second_turn["answer"])
        self.assertNotIn("是否还有其他信息", second_turn["answer"])

    def test_yes_after_expired_restart_uses_current_selected_order(self):
        """确认重新发起退货时应直接使用前端当前选中订单，不得再次索要订单号。"""
        pending = {
            "pending_id": "PA-RESTART-SELECTED",
            "status": "awaiting_confirmation",
            "action_type": "return_goods",
            "action_slots": {},
            "missing_slots": ["action_confirmation"],
            "next_action": "collect_slots",
            "updated_at": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(minutes=30)).isoformat(),
            "completed": False,
        }
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
            confidence=0.55,
            summary="确认",
            risk_reasons=[],
        )

        normalized, continued = enrich_action_analysis(
            draft,
            message="是",
            selected_order_no="EC202606220001",
            pending_action_request=pending,
        )

        self.assertEqual(normalized.action_slots["order_no"], "EC202606220001")
        self.assertEqual(normalized.missing_slots, ["after_sale_reason", "return_method"])
        self.assertEqual(continued["action_slots"]["order_no"], "EC202606220001")
        agent = object.__new__(CustomerServiceAgent)
        answer = agent._format_action_slot_question(
            {
                "analysis": normalized,
                "pending_action_request": continued,
                "tool_results": [],
            }
        )
        self.assertIn("已关联订单 EC202606220001", answer)
        self.assertIn("退货原因", answer)
        self.assertNotIn("提供订单号", answer)

    def test_current_selected_order_overrides_pending_candidate_when_confirmed(self):
        """客户确认前切换了前端订单时，应以当前选择为准而不是沿用旧候选订单。"""
        pending = {
            "pending_id": "PA-CANDIDATE-CHANGED",
            "status": "awaiting_confirmation",
            "action_type": "return_goods",
            "action_slots": {"candidate_order_no": "EC202606220001"},
            "missing_slots": ["order_confirmation"],
            "next_action": "collect_slots",
            "updated_at": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(minutes=30)).isoformat(),
            "completed": False,
        }
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
            confidence=0.55,
            summary="确认",
            risk_reasons=[],
        )

        normalized, continued = enrich_action_analysis(
            draft,
            message="是的",
            selected_order_no="EC202606220002",
            pending_action_request=pending,
        )

        self.assertEqual(normalized.action_slots["order_no"], "EC202606220002")
        self.assertNotIn("candidate_order_no", normalized.action_slots)
        self.assertEqual(continued["action_slots"]["order_no"], "EC202606220002")

    def test_selected_order_survives_expired_restart_and_reason_collection(self):
        """选中订单后的超时重启确认和原因补充必须连续留在同一退货流程。"""
        expired = {
            "pending_id": "PA-EXPIRED-THREE-TURNS",
            "status": "waiting_for_user_input",
            "action_type": "return_goods",
            "action_slots": {"order_no": "EC202606220099"},
            "missing_slots": ["return_reason"],
            "updated_at": (datetime.utcnow() - timedelta(minutes=31)).isoformat(),
            "expires_at": (datetime.utcnow() - timedelta(minutes=1)).isoformat(),
            "completed": False,
        }
        action_draft = IntentResult(
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
        confirmation_draft = action_draft.model_copy(
            update={"intent": "other", "user_goal": "other", "summary": "确认"}
        )

        first, restarted = enrich_action_analysis(
            action_draft,
            message="我要退货",
            selected_order_no="EC202606220001",
            pending_action_request=expired,
        )
        self.assertEqual(first.missing_slots, ["action_confirmation"])
        self.assertNotIn("order_no", restarted["action_slots"])

        confirmed, collecting_reason = enrich_action_analysis(
            confirmation_draft,
            message="是",
            selected_order_no="EC202606220001",
            pending_action_request=restarted,
        )
        self.assertEqual(confirmed.action_slots["order_no"], "EC202606220001")
        self.assertEqual(confirmed.missing_slots, ["after_sale_reason", "return_method"])

        reason_filled, collecting_method = enrich_action_analysis(
            action_draft,
            message="商品质量问题",
            selected_order_no="EC202606220001",
            pending_action_request=collecting_reason,
        )
        self.assertEqual(reason_filled.action_type, "return_goods")
        self.assertEqual(reason_filled.action_slots["order_no"], "EC202606220001")
        self.assertEqual(reason_filled.action_slots["after_sale_reason"], "商品质量问题")
        self.assertEqual(reason_filled.missing_slots, ["return_method"])
        self.assertEqual(collecting_method["missing_slots"], ["return_method"])

    def test_expired_restart_without_current_selection_does_not_restore_old_order(self):
        """前端取消选择后确认新流程时，不得从旧会话订单上下文恢复过期订单。"""
        pending = {
            "pending_id": "PA-RESTART-SELECTION-CLEARED",
            "status": "awaiting_confirmation",
            "action_type": "return_goods",
            "action_slots": {},
            "missing_slots": ["action_confirmation"],
            "next_action": "collect_slots",
            "updated_at": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(minutes=30)).isoformat(),
            "completed": False,
        }
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
            confidence=0.55,
            summary="确认",
            risk_reasons=[],
        )

        normalized, continued = enrich_action_analysis(
            draft,
            message="是",
            selected_order_no=None,
            pending_action_request=pending,
            conversation_context={
                "order_context": {
                    "order_no": "EC202606220001",
                    "source": "selected_by_user",
                    "confirmed_at": (datetime.utcnow() - timedelta(minutes=31)).isoformat(),
                    "last_used_at": (datetime.utcnow() - timedelta(minutes=31)).isoformat(),
                    "confidence": 0.98,
                }
            },
        )

        self.assertNotIn("order_no", normalized.action_slots)
        self.assertIn("order_no", normalized.missing_slots)
        self.assertNotIn("order_no", continued["action_slots"])

    def test_expired_explicit_return_request_is_not_saved_as_return_reason(self):
        """“我要退货”只能表达重新发起动作，不能被错误保存成退货原因。"""
        now = datetime.utcnow()
        expired = {
            "pending_id": "PA-OLD-EXPLICIT",
            "status": "waiting_for_user_input",
            "action_type": "return_goods",
            "action_slots": {"order_no": "EC202606220001"},
            "missing_slots": ["return_reason"],
            "updated_at": (now - timedelta(minutes=31)).isoformat(),
            "expires_at": (now - timedelta(minutes=1)).isoformat(),
            "completed": False,
        }
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

        normalized, restarted = enrich_action_analysis(
            analysis,
            message="我要退货",
            selected_order_no=None,
            pending_action_request=expired,
        )

        self.assertEqual(normalized.missing_slots, ["action_confirmation"])
        self.assertNotIn("deferred_slots", restarted)
        self.assertNotIn("order_no", restarted["action_slots"])

    def test_legacy_candidate_question_accepts_yes_from_context_order(self):
        """旧会话只保存 order_no 缺槽时，“是的”也应绑定上一轮已展示的候选订单。"""
        pending = {
            "pending_id": "PA-LEGACY-ORDER-CONFIRM",
            "status": "waiting_for_user_input",
            "action_type": "return_goods",
            "action_slots": {},
            "missing_slots": ["order_no", "return_reason"],
            "updated_at": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(minutes=30)).isoformat(),
            "completed": False,
        }
        analysis = IntentResult(
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
            confidence=0.5,
            summary="是的",
            risk_reasons=[],
        )

        normalized, continued = enrich_action_analysis(
            analysis,
            message="是的",
            selected_order_no=None,
            pending_action_request=pending,
            conversation_context={
                "order_context": {
                    "order_no": "EC202606220001",
                    "source": "inferred_from_context",
                    "confirmed_at": None,
                    "last_used_at": datetime.utcnow().isoformat(),
                    "confidence": 0.6,
                    "expires_at": (datetime.utcnow() + timedelta(minutes=30)).isoformat(),
                }
            },
        )

        self.assertEqual(normalized.action_slots["order_no"], "EC202606220001")
        self.assertEqual(normalized.missing_slots, ["after_sale_reason", "return_method"])
        self.assertEqual(continued["missing_slots"], ["return_reason", "return_method"])

    def test_pickup_time_creates_return_goods_ticket_end_to_end(self):
        """退货原因、取件方式和时间补齐后，必须创建包含履约偏好的 refund 工单。"""
        pending = {
            "pending_id": "PA-RETURN-REASON",
            "status": "collecting",
            "action_type": "return_goods",
            "action_slots": {
                "order_no": "EC202606220001",
                "after_sale_reason": "商品质量问题",
                "description": "商品质量问题",
                "return_method": "pickup",
                "pickup_status": "PREFERENCE_RECORDED",
            },
            "missing_slots": ["pickup_time_window"],
            "next_action": "collect_slots",
            "completed": False,
        }
        draft = IntentResult(
            intent="complaint",
            user_goal="complaint",
            emotion="normal",
            order_related=True,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=True,
            need_human=True,
            priority="high",
            confidence=0.9,
            summary="明天下午",
            risk_reasons=["complaint"],
            action_type="complaint_submit",
        )
        agent = object.__new__(CustomerServiceAgent)
        agent.llm_analyzer = None
        created_payloads: list[dict] = []
        queried_orders: list[str] = []

        def prepare_action(state):
            normalized, new_pending = enrich_action_analysis(
                state["analysis"],
                message=state["message"],
                selected_order_no=state.get("selected_order_no"),
                pending_action_request=state.get("pending_action_request"),
                conversation_context=state.get("conversation_context"),
            )
            return {"analysis": normalized, "pending_action_request": new_pending}

        def query_order(order_no, auth_token):
            queried_orders.append(order_no)
            return {
                "status": "success",
                "query_type": "order_detail",
                "order_no": order_no,
                "data": {
                    "orderNo": order_no,
                    "productName": "Smart Router AX3000",
                    "orderStatus": "SIGNED",
                    "afterSaleStatus": "NONE",
                },
            }

        def create_ticket(payload, auth_token):
            created_payloads.append(payload)
            return {
                "status": "success",
                "data": {
                    "ticketNo": "T-RETURN-001",
                    "status": "PENDING_ASSIGN",
                    "ticketType": payload["ticketType"],
                    "orderNo": payload["orderNo"],
                },
            }

        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: draft),
            retrieve_knowledge=lambda _: [],
            query_order=query_order,
            query_customer_orders=lambda customer_id, auth_token: {"status": "success", "data": []},
            query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
            create_ticket=create_ticket,
            auto_assign_ticket=lambda ticket_no: {"status": "failed"},
            list_customer_tickets=lambda auth_token: {"status": "success", "data": []},
            query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
            urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
            prepare_action=prepare_action,
            compose_answer=lambda state: agent._compose_answer(state),
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        result = graph.invoke(
            {
                "message": "明天下午",
                "customer_id": 1,
                "session_id": "S-RETURN",
                "pending_action_request": pending,
                "tool_results": [],
                "citations": [],
            }
        )
        customer_message = agent._build_customer_message(result)

        self.assertEqual(queried_orders, ["EC202606220001"])
        self.assertEqual(len(created_payloads), 1)
        payload = created_payloads[0]
        self.assertEqual(payload["ticketType"], "refund")
        self.assertEqual(payload["orderNo"], "EC202606220001")
        self.assertEqual(payload["idempotency_key"], "agent-ticket:PA-RETURN-REASON")
        self.assertEqual(payload["returnMethod"], "pickup")
        self.assertEqual(payload["pickupTimeWindow"], "明天下午")
        self.assertEqual(payload["pickupStatus"], "PREFERENCE_RECORDED")
        self.assertIn("业务动作：return_goods", payload["content"])
        self.assertIn("after_sale_reason: 商品质量问题", payload["content"])
        self.assertEqual(result["ticket_result"]["status"], "success")
        self.assertEqual(result["pending_action_request"]["status"], "completed")
        self.assertTrue(result["pending_action_request"]["completed"])
        self.assertEqual(result["pending_action_request"]["ticket_no"], "T-RETURN-001")
        self.assertIn("已为您提交退货申请", customer_message)
        self.assertIn("T-RETURN-001", customer_message)
        self.assertIn("明天下午", customer_message)
        self.assertIn("具体取件安排以工作人员或承运方后续确认为准", customer_message)
        self.assertNotIn("投诉", customer_message)

    def test_return_method_and_pickup_time_are_collected_conditionally(self):
        """仅选择上门取件时追问时间，自行寄回可以直接进入建单。"""
        analysis = IntentResult(
            intent="other",
            user_goal="out_of_scope",
            emotion="normal",
            order_related=False,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="low",
            confidence=0.5,
            summary="补充退回方式",
            risk_reasons=[],
        )
        base_pending = {
            "pending_id": "PA-FULFILLMENT",
            "status": "waiting_for_user_input",
            "action_type": "return_goods",
            "action_slots": {
                "order_no": "EC202606220001",
                "after_sale_reason": "拍错了",
                "description": "拍错了",
            },
            "missing_slots": ["return_method"],
            "next_action": "collect_slots",
            "completed": False,
        }

        pickup, pickup_pending = enrich_action_analysis(
            analysis,
            message="需要上门取件",
            selected_order_no=None,
            pending_action_request=base_pending,
        )
        self.assertEqual(pickup.action_slots["return_method"], "pickup")
        self.assertEqual(pickup.missing_slots, ["pickup_time_window"])
        self.assertEqual(pickup_pending["status"], "waiting_for_user_input")

        self_ship, self_ship_pending = enrich_action_analysis(
            analysis,
            message="不用上门，我自己寄回",
            selected_order_no=None,
            pending_action_request=base_pending,
        )
        self.assertEqual(self_ship.action_slots["return_method"], "self_ship")
        self.assertEqual(self_ship.missing_slots, [])
        self.assertEqual(self_ship.next_action, "create_ticket")
        self.assertEqual(self_ship_pending["status"], "ready")

    def test_return_goods_request_without_selected_order_does_not_inherit_history_order(self):
        """未选中订单且未明确指代时，退货申请不能自动关联历史最近订单。"""
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
            conversation_context={
                "last_order": {
                    "value": "EC202606220001",
                    "source": "tool_customer_orders",
                    "confidence": 0.9,
                }
            },
        )

        self.assertNotIn("EC202606220001", normalized.order_no)
        self.assertNotIn("order_no", normalized.action_slots)
        self.assertIn("order_no", normalized.missing_slots)
        self.assertEqual((pending or {}).get("action_slots", {}).get("order_no"), None)

    def test_return_goods_request_with_context_reference_can_inherit_history_order(self):
        """用户明确说“这单”时，可以继承历史订单上下文继续多轮动作。"""
        confirmed_at = (datetime.utcnow() - timedelta(minutes=3)).isoformat()
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
            summary="这单我要退货",
            risk_reasons=[],
            action_type="return_goods",
        )

        normalized, pending = enrich_action_analysis(
            analysis,
            message="这单我要退货",
            selected_order_no=None,
            pending_action_request=None,
            conversation_context={
                "last_order": {
                    "value": "EC202606220001",
                    "source": "tool_customer_orders",
                    "confidence": 0.9,
                },
                "order_context": {
                    "order_no": "EC202606220001",
                    "source": "selected_by_user",
                    "confirmed_at": confirmed_at,
                    "last_used_at": confirmed_at,
                    "confidence": 0.98,
                },
            },
        )

        self.assertIn("EC202606220001", normalized.order_no)
        self.assertEqual(normalized.action_slots.get("order_no"), "EC202606220001")
        self.assertNotIn("order_no", normalized.missing_slots)
        self.assertEqual((pending or {}).get("action_slots", {}).get("order_no"), "EC202606220001")

    def test_five_to_thirty_minute_order_context_requires_confirmation(self):
        """5-30 分钟内的历史订单不能直接执行动作，必须先确认订单。"""
        confirmed_at = (datetime.utcnow() - timedelta(minutes=20)).isoformat()
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
            summary="这单我要退货",
            risk_reasons=[],
            action_type="return_goods",
        )

        normalized, pending = enrich_action_analysis(
            analysis,
            message="这单我要退货",
            selected_order_no=None,
            pending_action_request=None,
            conversation_context={
                "order_context": {
                    "order_no": "EC202606220001",
                    "source": "selected_by_user",
                    "confirmed_at": confirmed_at,
                    "last_used_at": confirmed_at,
                    "confidence": 0.98,
                },
            },
        )

        self.assertEqual(normalized.next_action, "collect_slots")
        self.assertEqual(normalized.missing_slots, ["order_confirmation"])
        self.assertNotIn("EC202606220001", normalized.order_no)
        self.assertEqual((pending or {}).get("action_slots", {}).get("candidate_order_no"), "EC202606220001")

    def test_confirming_stale_order_context_promotes_candidate_order(self):
        """用户确认候选订单后，候选订单才能进入后续售后动作槽位。"""
        pending = {
            "pending_id": "PA-ORDER-CHECK",
            "status": "collecting",
            "action_type": "return_goods",
            "action_slots": {"candidate_order_no": "EC202606220001"},
            "missing_slots": ["order_confirmation"],
            "next_action": "collect_slots",
            "completed": False,
        }
        analysis = IntentResult(
            intent="other",
            user_goal="out_of_scope",
            emotion="normal",
            order_related=False,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="low",
            confidence=0.8,
            summary="确认订单",
            risk_reasons=[],
        )

        normalized, new_pending = enrich_action_analysis(
            analysis,
            message="是的",
            selected_order_no=None,
            pending_action_request=pending,
            conversation_context=None,
        )

        self.assertEqual(normalized.user_goal, "action_request")
        self.assertEqual(normalized.action_slots.get("order_no"), "EC202606220001")
        self.assertNotIn("candidate_order_no", normalized.action_slots)
        self.assertIn("EC202606220001", normalized.order_no)
        self.assertIn("after_sale_reason", normalized.missing_slots)
        self.assertEqual((new_pending or {}).get("action_slots", {}).get("order_no"), "EC202606220001")

    def test_rejecting_stale_order_context_requires_explicit_order(self):
        """用户否认候选订单后，不再继续使用旧订单上下文。"""
        pending = {
            "pending_id": "PA-ORDER-CHECK",
            "status": "collecting",
            "action_type": "return_goods",
            "action_slots": {"candidate_order_no": "EC202606220001"},
            "missing_slots": ["order_confirmation"],
            "next_action": "collect_slots",
            "completed": False,
        }
        analysis = IntentResult(
            intent="other",
            user_goal="out_of_scope",
            emotion="normal",
            order_related=False,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="low",
            confidence=0.8,
            summary="否认订单",
            risk_reasons=[],
        )

        normalized, new_pending = enrich_action_analysis(
            analysis,
            message="不是",
            selected_order_no=None,
            pending_action_request=pending,
            conversation_context=None,
        )

        self.assertEqual(normalized.missing_slots, ["order_no"])
        self.assertNotIn("EC202606220001", normalized.order_no)
        self.assertEqual(normalized.action_slots, {})
        self.assertEqual((new_pending or {}).get("rejected_candidate_order_no"), "EC202606220001")

    def test_over_thirty_minute_order_context_is_not_used(self):
        """超过 30 分钟的订单上下文不再自动关联。"""
        confirmed_at = (datetime.utcnow() - timedelta(minutes=31)).isoformat()
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
            summary="这单我要退货",
            risk_reasons=[],
            action_type="return_goods",
        )

        normalized, pending = enrich_action_analysis(
            analysis,
            message="这单我要退货",
            selected_order_no=None,
            pending_action_request=None,
            conversation_context={
                "order_context": {
                    "order_no": "EC202606220001",
                    "source": "selected_by_user",
                    "confirmed_at": confirmed_at,
                    "last_used_at": confirmed_at,
                    "confidence": 0.98,
                },
            },
        )

        self.assertNotIn("EC202606220001", normalized.order_no)
        self.assertIn("order_no", normalized.missing_slots)
        self.assertNotEqual((pending or {}).get("action_slots", {}).get("order_no"), "EC202606220001")

    def test_selected_order_overrides_stale_history_context(self):
        """当前前端选中订单优先级高于过期历史订单。"""
        confirmed_at = (datetime.utcnow() - timedelta(minutes=31)).isoformat()
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
            selected_order_no="EC202606220002",
            pending_action_request=None,
            conversation_context={
                "order_context": {
                    "order_no": "EC202606220001",
                    "source": "selected_by_user",
                    "confirmed_at": confirmed_at,
                    "last_used_at": confirmed_at,
                    "confidence": 0.98,
                },
            },
        )

        self.assertIn("EC202606220002", normalized.order_no)
        self.assertEqual(normalized.action_slots.get("order_no"), "EC202606220002")
        self.assertEqual((pending or {}).get("action_slots", {}).get("order_no"), "EC202606220002")

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


    def test_restart_confirmation_can_fill_return_reason_in_same_message(self):
        """“确认，原因不想要了”应同时恢复退货流程并补原因，随后继续询问退回方式。"""
        now = datetime.utcnow()
        pending = {
            "pending_id": "PA-COMPOUND-CONFIRM",
            "action_type": "return_goods",
            "action_slots": {},
            "collected_slots": {},
            "missing_slots": ["action_confirmation"],
            "status": "awaiting_confirmation",
            "updated_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=30)).isoformat(),
            "confirmation_reason": "expired_pending_action",
        }
        draft = IntentResult(
            intent="other",
            user_goal="out_of_scope",
            emotion="normal",
            order_related=False,
            order_no=[],
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="low",
            confidence=0.5,
            summary="确认退货原因",
        )

        normalized, continued = enrich_action_analysis(
            draft,
            message="确认，原因不想要了",
            selected_order_no="EC202606220001",
            pending_action_request=pending,
        )
        agent = CustomerServiceAgent.__new__(CustomerServiceAgent)
        question = agent._format_action_slot_question(
            {"analysis": normalized, "pending_action_request": continued, "tool_results": []}
        )

        self.assertEqual(normalized.user_goal, "action_request")
        self.assertEqual(normalized.action_type, "return_goods")
        self.assertEqual(normalized.action_slots["order_no"], "EC202606220001")
        self.assertEqual(normalized.action_slots["after_sale_reason"], "不想要了")
        self.assertEqual(normalized.missing_slots, ["return_method"])
        self.assertEqual((continued or {}).get("missing_slots"), ["return_method"])
        self.assertNotEqual((continued or {}).get("status"), "cancelled")
        self.assertIn("上门取件", question)
        self.assertIn("自行寄回", question)

    def test_order_validation_failure_does_not_fake_ticket_submission(self):
        """订单号已收集但 Java 校验失败时应明确尚未建单，不能再次索要订单号或假报已提交。"""
        analysis = IntentResult(
            intent="refund",
            user_goal="action_request",
            emotion="normal",
            order_related=True,
            order_no=["EC202606220001"],
            need_order_query=True,
            need_ticket=True,
            need_human=True,
            priority="high",
            confidence=0.95,
            summary="申请退货",
            action_type="return_goods",
            action_slots={
                "order_no": "EC202606220001",
                "after_sale_reason": "商品质量问题",
                "return_method": "pickup",
                "pickup_time_window": "明天下午5点",
            },
            missing_slots=[],
            next_action="create_ticket",
        )
        created_payloads = []
        agent = CustomerServiceAgent.__new__(CustomerServiceAgent)
        agent.llm_analyzer = None
        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: analysis),
            retrieve_knowledge=lambda _: [],
            query_order=lambda order_no, auth_token: {
                "status": "failed",
                "query_type": "order_detail",
                "order_no": order_no,
                "error": "4xx",
            },
            query_customer_orders=lambda customer_id, auth_token: {"status": "success", "data": []},
            query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
            create_ticket=lambda payload, auth_token: created_payloads.append(payload) or {"status": "success"},
            auto_assign_ticket=lambda ticket_no: {"status": "success"},
            list_customer_tickets=lambda auth_token: {"status": "success", "data": []},
            query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
            urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
            prepare_action=lambda state: {},
            compose_answer=lambda state: "接下来将为您提交退货申请",
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        result = graph.invoke(
            {
                "message": "是",
                "session_id": "S-ORDER-VALIDATION",
                "customer_id": 1,
                "selected_order_no": "EC202606220001",
                "tool_results": [],
                "citations": [],
            }
        )
        customer_message = agent._build_customer_message(result)

        self.assertEqual(created_payloads, [])
        self.assertEqual(result["analysis"].missing_slots, [])
        self.assertEqual(result["analysis"].next_action, "transfer_human")
        self.assertTrue(result["analysis"].need_human)
        self.assertIn("尚未创建售后工单", customer_message)
        self.assertNotIn("已提交", customer_message)


if __name__ == "__main__":
    unittest.main()
