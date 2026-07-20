"""验证多轮语义上下文能贯穿意图识别、动作判断和工具路由。"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from langchain_core.runnables import RunnableLambda

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.action_request import enrich_action_analysis
from agents.conversation_context import build_conversation_context
from agents.customer_service_agent import CustomerServiceAgent
from agents.intent_normalizer import is_order_detail_query_message
from agents.llm_intent_analyzer import LLMIntentAnalyzer
from graphs.ticket_process_graph import build_ticket_process_graph
from schemas.intent_schema import IntentResult, LLMIntentDraft


def _status_analysis(**overrides):
    """构造低风险状态查询识别结果。"""
    data = {
        "intent": "logistics",
        "user_goal": "status_query",
        "emotion": "normal",
        "order_related": True,
        "order_no": [],
        "product_name": None,
        "need_order_query": True,
        "need_ticket": False,
        "need_human": False,
        "priority": "medium",
        "confidence": 0.9,
        "summary": "客户查询进度",
        "risk_reasons": [],
    }
    data.update(overrides)
    return IntentResult(**data)


class ConversationContextTest(unittest.TestCase):
    """覆盖上下文构建、安全摘要和跨节点使用。"""

    def test_clear_logistics_query_skips_intent_llm(self):
        """明确物流查询应走确定性识别，避免额外等待一次意图模型调用。"""
        agent = object.__new__(CustomerServiceAgent)

        class UnexpectedLLM:
            """若快速路径失效则主动报错，确保本测试能发现性能回归。"""

            def invoke(self, message):
                raise AssertionError(f"明确物流查询不应调用 LLM：{message}")

        agent.llm_analyzer = UnexpectedLLM()
        result = agent._analyze_with_llm_fallback({"message": "查询当前订单物流状态"})

        self.assertEqual(result.intent, "logistics")
        self.assertEqual(result.user_goal, "status_query")
        self.assertTrue(result.need_order_query)

    def test_realtime_order_query_skips_rag_retrieval(self):
        """订单实时查询已有 Java 权威结果时，不应再等待 RAG 检索。"""
        retrieval_calls: list[dict] = []
        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: _status_analysis()),
            retrieve_knowledge=lambda payload: retrieval_calls.append(payload) or [],
            query_order=lambda order_no, auth_token: {
                "status": "success",
                "query_type": "order_detail",
                "data": {"orderNo": order_no, "productName": "测试商品", "orderStatus": "SIGNED"},
            },
            query_customer_orders=lambda customer_id, auth_token: {"status": "empty", "data": []},
            query_order_logistics=lambda order_no, auth_token: {"status": "success", "query_type": "order_logistics", "data": {}},
            create_ticket=lambda payload, auth_token: {"status": "skipped"},
            auto_assign_ticket=lambda ticket_no: {"status": "skipped"},
            list_customer_tickets=lambda auth_token: {"status": "empty", "data": []},
            query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
            urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
            prepare_action=lambda state: {"analysis": state["analysis"]},
            compose_answer=lambda state: "订单状态已查询",
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        result = graph.invoke(
            {
                "message": "查询订单状态",
                "customer_id": 7,
                "selected_order_no": "EC202607160009",
                "tool_results": [],
                "citations": [],
            }
        )

        self.assertEqual(retrieval_calls, [])
        self.assertEqual(result["citations"], [])

    def test_selected_order_tool_failure_does_not_request_order_number_again(self):
        """前端已选订单时，下游查询失败应提示重试，不能错误要求客户再次提供订单号。"""
        agent = object.__new__(CustomerServiceAgent)
        analysis = _status_analysis()
        answer = agent._compose_answer(
            {
                "message": "查询订单状态",
                "analysis": analysis,
                "selected_order_no": "EC202607160008",
                "tool_results": [
                    {
                        "status": "failed",
                        "query_type": "order_detail",
                        "order_no": "EC202607160008",
                        "error": "5xx",
                    }
                ],
                "citations": [],
            }
        )

        self.assertIn("已关联您当前选中的订单 EC202607160008", answer)
        self.assertNotIn("提供订单号", answer)

    def test_selected_order_product_inquiry_uses_entity_semantics_not_fixed_phrase(self):
        """已选订单下的“这款商品怎么样”应作为商品咨询，而非越界兜底。"""
        agent = object.__new__(CustomerServiceAgent)
        context = build_conversation_context(
            messages=[],
            pending_action_request=None,
            selected_order_no="EC202607160010",
            selected_ticket_no=None,
        )
        out_of_scope = IntentResult(
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
            confidence=0.6,
            summary="用户询问商品怎么样",
            risk_reasons=["out_of_scope"],
        )

        result = agent._apply_context_guardrails("这款商品怎么样", out_of_scope, context)

        self.assertEqual(result.intent, "consult")
        self.assertEqual(result.user_goal, "info_query")
        self.assertTrue(result.order_related)
        self.assertTrue(result.need_order_query)
        self.assertEqual(result.order_no, ["EC202607160010"])
        self.assertFalse(result.need_human)

    def test_selected_order_product_inquiry_builds_product_answer(self):
        """同一语义路由应进入可信商品事实与自然补充回复，而不是无证据兜底。"""
        agent = object.__new__(CustomerServiceAgent)
        agent.llm_analyzer = None
        context = build_conversation_context(
            messages=[],
            pending_action_request=None,
            selected_order_no="EC202607160010",
            selected_ticket_no=None,
        )
        analysis = _status_analysis(intent="consult", user_goal="info_query", order_no=["EC202607160010"])
        answer = agent._compose_answer(
            {
                "message": "这款商品怎么样",
                "analysis": analysis,
                "conversation_context": context,
                "tool_results": [
                    {
                        "status": "success",
                        "query_type": "order_detail",
                        "data": {
                            "orderNo": "EC202607160010",
                            "productName": "Noise Cancelling Headset",
                            "productCategory": "audio",
                            "amount": 699,
                            "orderStatus": "SHIPPED",
                            "afterSaleStatus": "NONE",
                        },
                    }
                ],
                "citations": [],
            }
        )

        self.assertIn("Noise Cancelling Headset", answer)
        self.assertIn("订单实付：¥699", answer)
        self.assertNotIn("没有找到足够明确的业务依据", answer)

    def test_selected_order_product_question_uses_current_order_context(self):
        """本轮已选订单的商品详情问题应绑定该订单并触发只读查询。"""
        agent = object.__new__(CustomerServiceAgent)
        context = build_conversation_context(
            messages=[],
            pending_action_request=None,
            selected_order_no="EC202607160009",
            selected_ticket_no=None,
        )
        draft = _status_analysis(
            intent="other",
            user_goal="info_query",
            order_related=False,
            need_order_query=False,
            order_no=[],
        )

        guarded = agent._apply_context_guardrails("介绍一下该订单的商品", draft, context)

        self.assertTrue(is_order_detail_query_message("介绍一下该订单的商品"))
        self.assertEqual(guarded.order_no, ["EC202607160009"])
        self.assertTrue(guarded.order_related)
        self.assertTrue(guarded.need_order_query)

    def test_selected_order_product_answer_uses_verified_fields(self):
        """商品介绍应展示订单工具返回的可信字段，不再索要已选订单号或编造产品能力。"""
        agent = object.__new__(CustomerServiceAgent)
        answer = agent._compose_answer(
            {
                "message": "介绍一下该订单的商品",
                "analysis": _status_analysis(intent="other", user_goal="info_query"),
                "selected_order_no": "EC202607160009",
                "tool_results": [
                    {
                        "status": "success",
                        "query_type": "order_detail",
                        "order_no": "EC202607160009",
                        "data": {
                            "orderNo": "EC202607160009",
                            "productName": "Smart Router AX3000",
                            "productCategory": "智能网络设备",
                            "quantity": 1,
                            "amount": 399,
                            "warrantyDays": 365,
                            "returnable": True,
                            "orderStatus": "SIGNED",
                            "afterSaleStatus": "NONE",
                        },
                    }
                ],
                "citations": [],
            }
        )

        self.assertIn("Smart Router AX3000", answer)
        self.assertIn("智能网络设备", answer)
        self.assertIn("质保期：365 天", answer)
        self.assertNotIn("补充订单号", answer)
        self.assertNotIn("足够明确的业务依据", answer)

    def test_selected_order_product_answer_appends_grounded_llm_overview(self):
        """商品事实后可追加自然用途说明，但必须把缺少评价证据的边界传给模型。"""
        class FakeProductLLM:
            def __init__(self):
                self.payloads = []

            def generate_customer_reply(self, payload):
                self.payloads.append(payload)
                return "从品类看，它通常用于家庭或小型办公网络。是否划算还需结合实际覆盖需求；目前暂无可核验的用户评价数据。"

        fake_llm = FakeProductLLM()
        agent = object.__new__(CustomerServiceAgent)
        agent.llm_analyzer = fake_llm
        agent._log = lambda *args, **kwargs: None
        answer = agent._compose_answer(
            {
                "message": "介绍一下该订单的商品",
                "analysis": _status_analysis(intent="other", user_goal="info_query"),
                "selected_order_no": "EC202607160009",
                "tool_results": [
                    {
                        "status": "success",
                        "query_type": "order_detail",
                        "order_no": "EC202607160009",
                        "data": {
                            "orderNo": "EC202607160009",
                            "productName": "Smart Router AX3000",
                            "productCategory": "network",
                            "quantity": 1,
                            "amount": 399,
                            "orderStatus": "SIGNED",
                        },
                    }
                ],
                "citations": [],
            }
        )

        self.assertIn("订单 EC202607160009", answer)
        self.assertIn("补充说明", answer)
        self.assertIn("家庭或小型办公网络", answer)
        self.assertIn("暂无可核验的用户评价数据", answer)
        self.assertEqual(fake_llm.payloads[0]["reply_mode"], "product_overview")
        self.assertFalse(fake_llm.payloads[0]["extra_context"]["review_evidence_available"])

    def test_logistics_status_keeps_absolute_expired_delivery_time(self):
        """物流工具中的历史预计时间必须保留绝对日期，不能被模型改写成今天或明天。"""
        agent = object.__new__(CustomerServiceAgent)
        analysis = _status_analysis(intent="logistics")
        answer = agent._compose_answer(
            {
                "message": "查询订单状态",
                "analysis": analysis,
                "selected_order_no": "EC202607160008",
                "tool_results": [
                    {
                        "status": "success",
                        "query_type": "order_detail",
                        "order_no": "EC202607160008",
                        "data": {
                            "orderNo": "EC202607160008",
                            "productName": "Noise Cancelling Headset",
                            "orderStatus": "SHIPPED",
                        },
                    },
                    {
                        "status": "success",
                        "query_type": "order_logistics",
                        "order_no": "EC202607160008",
                        "data": {
                            "orderNo": "EC202607160008",
                            "logisticsStatus": "OUT_FOR_DELIVERY",
                            "estimatedDeliveryTime": "2000-01-01T09:00:00",
                            "traces": [],
                        },
                    },
                ],
                "citations": [],
            }
        )

        self.assertIn("2000-01-01 09:00", answer)
        self.assertIn("该预计时间已过", answer)
        self.assertNotIn("今天", answer)

    def test_delivery_contingency_without_order_gives_actionable_order_guidance(self):
        """未来未收到再售后的表达应说明核查路径，而不是退化成泛化安慰话术。"""
        agent = object.__new__(CustomerServiceAgent)
        answer = agent._compose_answer(
            {
                "message": "明天收不到就帮我退了吧",
                "analysis": _status_analysis(intent="refund", user_goal="action_request"),
                "selected_order_no": None,
                "tool_results": [],
                "citations": [],
            }
        )

        self.assertIn("还没有关联具体订单", answer)
        self.assertIn("先核查最新物流和签收记录", answer)
        self.assertIn("不能提前承诺退货一定成功", answer)
        self.assertNotIn("已为您提交退货申请", answer)

    def test_delivery_contingency_uses_selected_order_without_creating_return_early(self):
        """已选订单的条件售后应带出订单上下文，但不能在未来条件尚未发生时提前建单。"""
        agent = object.__new__(CustomerServiceAgent)
        answer = agent._compose_answer(
            {
                "message": "如果明天还收不到就帮我退货",
                "analysis": _status_analysis(intent="refund", user_goal="action_request"),
                "selected_order_no": "EC202607160008",
                "tool_results": [],
                "citations": [],
            }
        )

        self.assertIn("订单 EC202607160008", answer)
        self.assertIn("现在不宜提前提交退货申请", answer)
        self.assertIn("系统显示已签收但您未收到", answer)
        self.assertNotIn("提供订单号", answer)

    def test_llm_draft_accepts_null_order_no(self):
        """LLM 返回 order_no=null 时应视为无订单号，不能让整轮意图识别降级。"""
        draft = LLMIntentDraft.model_validate(
            {
                "intent": "refund",
                "user_goal": "policy_consult",
                "emotion": "normal",
                "order_related": False,
                "order_no": None,
                "product_name": None,
                "need_order_query": None,
                "need_ticket": None,
                "need_human": False,
                "priority": "medium",
                "confidence": 0.92,
                "summary": "查看退货规则",
                "risk_reasons": [],
                "action_type": "return_goods",
                "action_slots": None,
                "missing_slots": None,
                "next_action": None,
            }
        )
        analyzer = object.__new__(LLMIntentAnalyzer)

        result = analyzer._complete_intent_result("查看退货规则", draft)

        self.assertEqual(result.order_no, [])
        self.assertEqual(result.action_slots, {})
        self.assertEqual(result.missing_slots, [])
        self.assertEqual(result.user_goal, "policy_consult")
        self.assertFalse(result.need_order_query)

    def test_session_memory_keeps_recent_visible_turns_and_login_identity(self):
        """当前会话记忆应保留最近客户可见问答，并携带安全登录态身份。"""
        context = build_conversation_context(
            messages=[
                {"id": 1, "sender_type": "customer", "content": "查询退货规则", "extra_data": {}, "created_at": "t1"},
                {"id": 2, "sender_type": "ai", "content": "退货规则说明", "extra_data": {"customer_message": "商品保持完好可申请退货"}, "created_at": "t2"},
            ],
            pending_action_request=None,
            selected_order_no=None,
            selected_ticket_no=None,
            login_user_context={"display_name": "张三", "role": "customer", "verified": True, "source": "java_auth"},
        )

        self.assertEqual(context["login_user_context"]["display_name"], "张三")
        self.assertEqual(context["session_memory"]["last_user_question"], "查询退货规则")
        self.assertEqual(context["session_memory"]["last_ai_answer"], "商品保持完好可申请退货")
        self.assertFalse(context["session_memory"]["identity_conflict"])

    def test_session_memory_contains_current_pending_action(self):
        """当前会话记忆应包含安全 pending 摘要，供多轮流程优先级判断。"""
        pending = {
            "pending_id": "PA-MEMORY",
            "status": "waiting_for_user_input",
            "action_type": "return_goods",
            "issue_type": "return",
            "order_no": "EC202606220001",
            "action_slots": {"order_no": "EC202606220001"},
            "collected_slots": {"order_no": "EC202606220001"},
            "missing_slots": ["return_reason"],
            "created_at": "2026-07-15T10:00:00",
            "updated_at": "2026-07-15T10:01:00",
            "expires_at": "2026-07-15T10:31:00",
            "source": "current_session_explicit",
            "confidence": 0.95,
            "completed": False,
        }

        context = build_conversation_context(
            messages=[],
            pending_action_request=pending,
            selected_order_no=None,
            selected_ticket_no=None,
        )

        memory = context["session_memory"]["pending_action"]
        self.assertEqual(memory["action_type"], "return_goods")
        self.assertEqual(memory["missing_slots"], ["return_reason"])
        self.assertEqual(memory["collected_slots"]["order_no"], "EC202606220001")

    def test_session_memory_marks_self_claim_conflict_without_overriding_login(self):
        """用户自称姓名与登录名不同只能标记冲突，不能覆盖登录态身份。"""
        context = build_conversation_context(
            messages=[
                {"id": 1, "sender_type": "customer", "content": "我叫李四", "extra_data": {}, "created_at": "t1"},
            ],
            pending_action_request=None,
            selected_order_no=None,
            selected_ticket_no=None,
            login_user_context={"display_name": "张三", "role": "customer", "verified": True, "source": "java_auth"},
        )

        self.assertEqual(context["login_user_context"]["display_name"], "张三")
        self.assertEqual(context["session_memory"]["preferred_name"], "李四")
        self.assertTrue(context["session_memory"]["identity_conflict"])

    def test_unselected_current_request_does_not_inherit_historical_selected_order(self):
        """当前前端未选中订单时，不能继承历史 selected_by_user 订单上下文。"""
        context = build_conversation_context(
            messages=[
                {
                    "id": 1,
                    "sender_type": "customer",
                    "content": "我要退货",
                    "extra_data": {"selected_order_no": "EC202606220001"},
                    "created_at": (datetime.utcnow() - timedelta(minutes=3)).isoformat(),
                },
            ],
            pending_action_request=None,
            selected_order_no=None,
            selected_ticket_no=None,
            login_user_context=None,
        )

        self.assertIsNone(context["order_context"])

    def test_mentioned_order_context_survives_without_current_selection(self):
        """用户文本明确提到过订单号时，即使当前未选中，也可作为当前会话近期上下文。"""
        context = build_conversation_context(
            messages=[
                {
                    "id": 1,
                    "sender_type": "customer",
                    "content": "订单 EC202606220001 我要退货",
                    "extra_data": {},
                    "created_at": (datetime.utcnow() - timedelta(minutes=3)).isoformat(),
                },
            ],
            pending_action_request=None,
            selected_order_no=None,
            selected_ticket_no=None,
            login_user_context=None,
        )

        self.assertEqual((context["order_context"] or {}).get("order_no"), "EC202606220001")
        self.assertEqual((context["order_context"] or {}).get("source"), "mentioned_by_user")

    def test_user_identity_answer_prefers_login_identity_when_claim_conflicts(self):
        """“我是谁”必须优先回答登录态身份，并说明会话称呼不能用于校验。"""
        agent = object.__new__(CustomerServiceAgent)
        analysis = _status_analysis(intent="consult", user_goal="info_query", order_related=False, need_order_query=False)
        answer = agent._compose_answer(
            {
                "message": "我是谁",
                "analysis": analysis,
                "citations": [],
                "tool_results": [],
                "conversation_context": {
                    "login_user_context": {"display_name": "张三", "role": "customer", "verified": True, "source": "java_auth"},
                    "session_memory": {"preferred_name": "李四", "self_claimed_name": "李四", "identity_conflict": True},
                },
            }
        )

        self.assertIn("当前登录账号显示为张三", answer)
        self.assertIn("称呼为李四", answer)
        self.assertIn("以当前登录账号为准", answer)
        self.assertNotIn("identity_conflict", answer)

    def test_session_memory_question_returns_previous_user_message(self):
        """“我刚刚问了什么”应读取当前 session 的上一条用户问题。"""
        agent = object.__new__(CustomerServiceAgent)
        analysis = _status_analysis(intent="consult", user_goal="info_query", order_related=False, need_order_query=False)
        answer = agent._compose_answer(
            {
                "message": "我刚刚问了什么问题",
                "analysis": analysis,
                "citations": [],
                "tool_results": [],
                "conversation_context": {
                    "session_memory": {"last_user_question": "查询退货规则", "recent_user_messages": [{"content": "查询退货规则"}]},
                },
            }
        )

        self.assertIn("查询退货规则", answer)

    def test_self_claimed_name_cannot_drive_order_query(self):
        """本轮自称姓名不能作为订单查询身份，必须阻断订单工具路由。"""
        agent = object.__new__(CustomerServiceAgent)
        result = _status_analysis(order_no=[], need_order_query=True, order_related=True)

        guarded = agent._apply_context_guardrails(
            "我是李四，帮我查订单",
            result,
            {
                "login_user_context": {"display_name": "张三", "role": "customer", "verified": True, "source": "java_auth"},
                "session_memory": {},
            },
        )

        self.assertFalse(guarded.need_order_query)
        self.assertFalse(guarded.order_related)
        self.assertEqual(guarded.user_goal, "info_query")

    def test_preferred_name_cannot_be_used_as_order_query_identity(self):
        """preferred_name 只用于称呼，不能让“查李四的订单”绕过登录态身份。"""
        agent = object.__new__(CustomerServiceAgent)
        result = _status_analysis(order_no=[], need_order_query=True, order_related=True)

        guarded = agent._apply_context_guardrails(
            "帮我查李四的订单",
            result,
            {
                "login_user_context": {"display_name": "张三", "role": "customer", "verified": True, "source": "java_auth"},
                "session_memory": {"preferred_name": "李四", "self_claimed_name": "李四", "identity_conflict": True},
            },
        )

        self.assertFalse(guarded.need_order_query)
        self.assertEqual(guarded.user_goal, "info_query")

    def test_graph_does_not_query_history_order_for_bare_return_request(self):
        """未选中订单且未指代时，图节点不能用历史 last_order 调订单详情。"""
        calls: list[str] = []
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
        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: analysis),
            retrieve_knowledge=lambda state: [],
            query_order=lambda order_no, auth_token: calls.append(order_no) or {"status": "success", "query_type": "order_detail", "data": {"orderNo": order_no}},
            query_customer_orders=lambda customer_id, auth_token: {"status": "empty", "query_type": "customer_orders", "data": []},
            query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
            create_ticket=lambda payload, auth_token: {"status": "skipped"},
            auto_assign_ticket=lambda ticket_no: {"status": "skipped"},
            list_customer_tickets=lambda auth_token: {"status": "empty", "data": []},
            query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
            urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
            prepare_action=lambda state: {
                "analysis": enrich_action_analysis(
                    state["analysis"],
                    message=state["message"],
                    selected_order_no=state.get("selected_order_no"),
                    pending_action_request=state.get("pending_action_request"),
                    conversation_context=state.get("conversation_context"),
                )[0]
            },
            compose_answer=lambda state: "ok",
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        result = graph.invoke(
            {
                "message": "我要退货",
                "customer_id": 7,
                "selected_order_no": None,
                "conversation_context": {
                    "last_order": {
                        "value": "EC202606220001",
                        "source": "tool_customer_orders",
                        "confidence": 0.9,
                    }
                },
                "tool_results": [],
                "citations": [],
            }
        )

        self.assertEqual(calls, [])
        self.assertIn("order_no", result["analysis"].missing_slots)

    def test_stale_but_not_expired_order_context_prompts_confirmation(self):
        """5-30 分钟订单上下文只用于确认追问，不直接查询或执行动作。"""
        agent = object.__new__(CustomerServiceAgent)
        confirmed_at = (datetime.utcnow() - timedelta(minutes=20)).isoformat()
        analysis = _status_analysis(intent="refund", user_goal="info_query", order_related=False, need_order_query=False)

        answer = agent._compose_answer(
            {
                "message": "这个能退吗",
                "analysis": analysis,
                "citations": [],
                "tool_results": [],
                "conversation_context": {
                    "order_context": {
                        "order_no": "EC202606220001",
                        "source": "selected_by_user",
                        "confirmed_at": confirmed_at,
                        "last_used_at": confirmed_at,
                        "confidence": 0.98,
                    }
                },
            }
        )

        self.assertIn("请确认", answer)
        self.assertIn("EC202606220001", answer)

    def test_missing_valid_order_context_prompts_user_to_select_order(self):
        """没有有效订单上下文时，模糊订单问题应提示选择订单或提供订单号。"""
        agent = object.__new__(CustomerServiceAgent)
        analysis = _status_analysis(intent="refund", user_goal="info_query", order_related=False, need_order_query=False)

        answer = agent._compose_answer(
            {
                "message": "这个订单怎么样了",
                "analysis": analysis,
                "citations": [],
                "tool_results": [],
                "conversation_context": {},
            }
        )

        self.assertIn("选择订单", answer)
        self.assertIn("订单号", answer)

    def test_context_builder_extracts_tool_facts_without_raw_history(self):
        """上下文从工具结果提取高可信事实，但安全摘要不包含历史原文和内部字段。"""
        messages = [
            {
                "id": 1,
                "sender_type": "customer",
                "content": "我买的鞋子质量不好，商家不给换",
                "extra_data": {},
            },
            {
                "id": 2,
                "sender_type": "ai",
                "content": "已为您记录",
                "extra_data": {
                    "internal_suggestion": "内部建议不应进入摘要",
                    "risk_reasons": ["after_sale_dispute"],
                    "tool_results": [
                        {
                            "status": "success",
                            "query_type": "order_detail",
                            "data": {"orderNo": "EC202606220001", "productName": "轻便跑鞋"},
                        }
                    ],
                    "ticket_result": {"status": "success", "data": {"ticketNo": "T20260625095309625900"}},
                },
            },
        ]

        context = build_conversation_context(
            messages=messages,
            pending_action_request=None,
            selected_order_no=None,
            selected_ticket_no=None,
        )

        self.assertEqual(context["last_order"]["value"], "EC202606220001")
        self.assertEqual(context["last_order"]["source"], "tool_order_detail")
        self.assertEqual(context["last_order"]["confidence"], 0.95)
        self.assertEqual(context["last_product"]["value"], "轻便跑鞋")
        self.assertEqual(context["last_ticket"]["value"], "T20260625095309625900")
        summary = context["safe_context_summary"]
        self.assertIn("EC202606220001", summary)
        self.assertIn("轻便跑鞋", summary)
        self.assertNotIn("质量不好", summary)
        self.assertNotIn("internal_suggestion", summary)
        self.assertNotIn("risk_reasons", summary)

    def test_intent_analysis_uses_safe_context_order(self):
        """用户用“刚才那个订单”指代时，规则兜底识别会补入上下文订单号。"""
        confirmed_at = (datetime.utcnow() - timedelta(minutes=3)).isoformat()
        context = {
            "last_order": {"value": "EC202606220001", "source": "tool_order_detail", "confidence": 0.95},
            "order_context": {
                "order_no": "EC202606220001",
                "source": "selected_by_user",
                "confirmed_at": confirmed_at,
                "last_used_at": confirmed_at,
                "confidence": 0.98,
            },
            "safe_context_summary": "最近关联订单号为 EC202606220001，来源 tool_order_detail，置信度 0.95",
        }

        with patch.dict(os.environ, {}, clear=True):
            agent = CustomerServiceAgent()
            result = agent._analyze_with_llm_fallback(
                {"message": "刚才那个订单到哪了", "conversation_context": context}
            )

        self.assertEqual(result.user_goal, "status_query")
        self.assertIn("EC202606220001", result.order_no)
        self.assertTrue(result.need_order_query)

    def test_ticket_urge_uses_context_ticket_no(self):
        """用户省略工单号催办时，图节点使用上下文中的最近工单号。"""
        calls: list[str] = []

        def urge_ticket(ticket_no, reason, auth_token):
            calls.append(ticket_no)
            return {"status": "success", "query_type": "ticket_urge", "data": {"ticketNo": ticket_no}}

        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: _status_analysis(intent="consult", order_related=False, need_order_query=False)),
            retrieve_knowledge=lambda _: [],
            query_order=lambda order_no, auth_token: {"status": "empty"},
            query_customer_orders=lambda customer_id, auth_token: {"status": "empty", "data": []},
            query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
            create_ticket=lambda payload, auth_token: {"status": "failed"},
            auto_assign_ticket=lambda ticket_no: {"status": "failed"},
            list_customer_tickets=lambda auth_token: {"status": "success", "data": []},
            query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
            urge_ticket=urge_ticket,
            prepare_action=lambda state: {"analysis": state["analysis"]},
            compose_answer=lambda state: "ok",
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        result = graph.invoke(
            {
                "message": "帮我催一下",
                "conversation_context": {
                    "last_ticket": {
                        "value": "T20260625095309625900",
                        "source": "tool_create_ticket",
                        "confidence": 0.95,
                    }
                },
                "tool_results": [],
                "citations": [],
            }
        )

        self.assertEqual(calls, ["T20260625095309625900"])
        self.assertEqual(result["tool_results"][0]["query_type"], "ticket_urge")

    def test_pending_order_conflict_cancels_old_action_and_keeps_new_query(self):
        """上一轮退货 A，本轮 B 不退了查物流时，应取消旧 pending 并按 B 查询。"""
        pending = {
            "pending_id": "PA001",
            "status": "collecting",
            "action_type": "return_goods",
            "action_slots": {"order_no": "EC202606220001"},
            "missing_slots": ["after_sale_reason"],
            "next_action": "collect_slots",
            "completed": False,
        }
        analysis = _status_analysis(intent="refund", user_goal="action_request", action_type="return_goods")
        context: dict = {"debug_context": {}}

        normalized, new_pending = enrich_action_analysis(
            analysis,
            message="EC202606220002 不退了，帮我查一下物流",
            selected_order_no=None,
            pending_action_request=pending,
            conversation_context=context,
        )

        self.assertEqual(new_pending["status"], "cancelled")
        self.assertTrue(new_pending["completed"])
        self.assertEqual(normalized.user_goal, "status_query")
        self.assertIn("EC202606220002", normalized.order_no)
        self.assertTrue(normalized.need_order_query)
        self.assertEqual(context["debug_context"]["context_conflict"]["type"], "order_changed")

    def test_cancel_short_reply_uses_pending_context(self):
        """槽位追问中用户说“算了”应取消上一轮申请，不应被识别为越界问题。"""
        agent = CustomerServiceAgent()
        pending = {
            "pending_id": "PA-CANCEL",
            "status": "collecting",
            "action_type": "return_goods",
            "action_slots": {"order_no": "EC202606220001"},
            "missing_slots": ["after_sale_reason"],
            "next_action": "collect_slots",
            "completed": False,
        }
        analysis = _status_analysis(intent="other", user_goal="out_of_scope")

        normalized, new_pending = enrich_action_analysis(
            analysis,
            message="算了",
            selected_order_no=None,
            pending_action_request=pending,
        )

        self.assertEqual(normalized.user_goal, "action_request")
        self.assertEqual(normalized.next_action, "cancel_pending")
        self.assertEqual(new_pending["status"], "cancelled")
        self.assertEqual(new_pending["cancel_reason"], "user_cancel")
        answer = agent._compose_answer(
            {
                "message": "算了",
                "analysis": normalized,
                "tool_results": [],
                "citations": [],
                "pending_action_request": new_pending,
            }
        )
        self.assertIn("取消", answer)

    def test_logistics_query_switches_away_from_return_pending(self):
        """客户明确查询物流时应结束退货补槽，并按本轮物流意图继续处理。"""
        pending = {
            "pending_id": "PA-SWITCH-LOGISTICS",
            "status": "waiting_for_user_input",
            "action_type": "return_goods",
            "action_slots": {"order_no": "EC202606220001"},
            "missing_slots": ["return_reason"],
            "next_action": "collect_slots",
            "updated_at": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(minutes=30)).isoformat(),
            "completed": False,
        }
        analysis = _status_analysis(intent="logistics", user_goal="status_query")

        normalized, cancelled = enrich_action_analysis(
            analysis,
            message="查一下物流",
            selected_order_no="EC202606220001",
            pending_action_request=pending,
        )

        self.assertEqual(normalized.user_goal, "status_query")
        self.assertEqual(normalized.intent, "logistics")
        self.assertIsNone(normalized.action_type)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["cancel_reason"], "non_action_intent")

    def test_not_received_followup_cannot_restart_expired_return_action(self):
        """“我没收到”属于物流状态反馈，不能被宽泛短句兜底保存成过期退货原因。"""
        expired = {
            "pending_id": "PA-EXPIRED-RETURN",
            "status": "waiting_for_user_input",
            "action_type": "return_goods",
            "action_slots": {"order_no": "EC202606220001"},
            "missing_slots": ["after_sale_reason"],
            "updated_at": (datetime.utcnow() - timedelta(minutes=31)).isoformat(),
            "expires_at": (datetime.utcnow() - timedelta(minutes=1)).isoformat(),
            "completed": False,
        }

        normalized, restarted = enrich_action_analysis(
            _status_analysis(intent="logistics", user_goal="status_query"),
            message="我没收到",
            selected_order_no=None,
            pending_action_request=expired,
        )

        self.assertEqual(normalized.intent, "logistics")
        self.assertEqual(normalized.user_goal, "status_query")
        self.assertIsNone(normalized.action_type)
        self.assertIsNone(restarted)

    def test_signed_but_not_received_creates_logistics_exception_ticket(self):
        """Java 显示签收但客户反馈未收到时，应进入异常核实并建一次物流工单。"""
        created_payloads: list[dict] = []
        active_tickets: list[dict] = []

        def prepare_action(state):
            normalized, pending = enrich_action_analysis(
                state["analysis"],
                message=state["message"],
                selected_order_no=state.get("selected_order_no"),
                pending_action_request=state.get("pending_action_request"),
                conversation_context=state.get("conversation_context"),
            )
            return {"analysis": normalized, "pending_action_request": pending}

        def create_ticket(payload, auth_token):
            created_payloads.append(payload)
            ticket = {
                "ticketNo": "T-LOGISTICS-EXCEPTION",
                "ticketType": "logistics",
                "orderNo": "EC202606220001",
                "status": "PENDING_ASSIGN",
                "content": payload["content"],
            }
            active_tickets.append(ticket)
            return {"status": "success", "data": ticket}

        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: _status_analysis()),
            retrieve_knowledge=lambda _: [],
            query_order=lambda order_no, auth_token: {
                "status": "success",
                "query_type": "order_detail",
                "order_no": order_no,
                "data": {"orderNo": order_no, "orderStatus": "SIGNED"},
            },
            query_customer_orders=lambda customer_id, auth_token: {"status": "success", "data": []},
            query_order_logistics=lambda order_no, auth_token: {
                "status": "success",
                "query_type": "order_logistics",
                "order_no": order_no,
                "data": {
                    "orderNo": order_no,
                    "logisticsStatus": "SIGNED",
                    "latestLocation": "上海浦东签收点",
                    "traces": [{"status": "SIGNED", "description": "快件已签收"}],
                },
            },
            create_ticket=create_ticket,
            auto_assign_ticket=lambda ticket_no: {"status": "failed"},
            list_customer_tickets=lambda auth_token: {"status": "success", "data": list(active_tickets)},
            query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
            urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
            prepare_action=prepare_action,
            compose_answer=lambda state: "物流异常已登记",
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        result = graph.invoke({
            "message": "我没收到",
            "customer_id": 1,
            "selected_order_no": "EC202606220001",
            "tool_results": [],
            "citations": [],
        })
        repeated = graph.invoke({
            "message": "我还是没有收到",
            "customer_id": 1,
            "selected_order_no": "EC202606220001",
            "tool_results": [],
            "citations": [],
        })

        self.assertEqual(len(created_payloads), 1)
        self.assertEqual(created_payloads[0]["ticketType"], "logistics")
        self.assertIn("异常类型：物流显示签收但客户反馈未收到", created_payloads[0]["content"])
        self.assertIn("delivery_status_conflict", result["risk_reasons"])
        self.assertTrue(result["need_human"])
        answer = CustomerServiceAgent.__new__(CustomerServiceAgent)._build_customer_message(result)
        self.assertIn("显示包裹已签收", answer)
        self.assertIn("实际没有收到", answer)
        self.assertIn("物流签收异常", answer)
        self.assertNotIn("退货流程已超时", answer)
        self.assertTrue(repeated["ticket_result"]["deduplicated"])

    def test_short_out_of_scope_reply_during_pending_is_contextual_cancel(self):
        """即使短句被模型判成越界，也应优先结合 pending 解释为取消上一轮动作。"""
        pending = {
            "pending_id": "PA-CANCEL",
            "status": "collecting",
            "action_type": "return_goods",
            "action_slots": {"order_no": "EC202606220001"},
            "missing_slots": ["after_sale_reason"],
            "next_action": "collect_slots",
            "completed": False,
        }
        analysis = _status_analysis(intent="other", user_goal="out_of_scope")

        normalized, new_pending = enrich_action_analysis(
            analysis,
            message="不用",
            selected_order_no=None,
            pending_action_request=pending,
        )

        self.assertEqual(normalized.user_goal, "action_request")
        self.assertEqual(normalized.next_action, "cancel_pending")
        self.assertEqual(new_pending["cancel_reason"], "user_cancel")

    def test_agent_identity_question_does_not_create_ticket(self):
        """客户问“你是谁”是基础信息咨询，不能继承订单或旧退货申请并创建工单。"""
        calls = {"query_order": 0, "create_ticket": 0}
        pending = {
            "pending_id": "PA001",
            "status": "collecting",
            "action_type": "return_goods",
            "action_slots": {"order_no": "EC202606220001"},
            "missing_slots": ["after_sale_reason"],
            "next_action": "collect_slots",
            "completed": False,
        }
        draft = IntentResult(
            intent="refund",
            user_goal="action_request",
            emotion="normal",
            order_related=True,
            order_no=[],
            product_name=None,
            need_order_query=True,
            need_ticket=True,
            need_human=True,
            priority="high",
            confidence=0.55,
            summary="询问客服身份",
            risk_reasons=["low_confidence"],
            action_type="return_goods",
        )
        agent = object.__new__(CustomerServiceAgent)
        agent.llm_analyzer = None
        analysis = agent._apply_business_guardrails("你是谁", draft)

        def query_order(order_no, auth_token):
            calls["query_order"] += 1
            return {"status": "success", "data": {"orderNo": order_no}}

        def create_ticket(payload, auth_token):
            calls["create_ticket"] += 1
            return {"status": "success", "data": {"ticketNo": "T1", "status": "PENDING_ASSIGN"}}

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
            analyzer_chain=RunnableLambda(lambda _: analysis),
            retrieve_knowledge=lambda _: [],
            query_order=query_order,
            query_customer_orders=lambda customer_id, auth_token: {"status": "empty", "data": []},
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
                "message": "你是谁",
                "selected_order_no": "EC202606220001",
                "pending_action_request": pending,
                "tool_results": [],
                "citations": [],
            }
        )

        self.assertEqual(result["analysis"].user_goal, "info_query")
        self.assertFalse(result["analysis"].need_ticket)
        self.assertFalse(result["analysis"].need_human)
        self.assertTrue(result["auto_send"])
        self.assertIsNone(result["ticket_result"])
        self.assertEqual(calls, {"query_order": 0, "create_ticket": 0})
        self.assertIn("智能客服助手", result["answer"])

    def test_agent_capability_question_uses_llm_reply_when_available(self):
        """客户问能力介绍时，优先让 LLM 在安全能力清单内自然生成回复。"""
        class FakeReplyLLM:
            def __init__(self):
                self.payloads = []

            def generate_customer_reply(self, payload):
                self.payloads.append(payload)
                return "我可以帮您查物流、看工单进度、解答退换货和发票问题；复杂情况也能帮您转人工继续处理。"

        fake_llm = FakeReplyLLM()
        agent = object.__new__(CustomerServiceAgent)
        agent.llm_analyzer = fake_llm
        agent.log_repository = None
        agent.call_logs = []
        agent._log = lambda *args, **kwargs: None
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
            summary="询问客服能力",
            risk_reasons=["low_confidence"],
        )
        analysis = agent._apply_business_guardrails("你有什么能力", draft)

        answer = agent._compose_answer({"message": "你有什么能力", "analysis": analysis, "tool_results": [], "citations": []})

        self.assertEqual(answer, "我可以帮您查物流、看工单进度、解答退换货和发票问题；复杂情况也能帮您转人工继续处理。")
        self.assertEqual(len(fake_llm.payloads), 1)
        self.assertEqual(fake_llm.payloads[0]["reply_mode"], "info_query")
        self.assertIn("capabilities", fake_llm.payloads[0]["extra_context"])
        self.assertNotIn("当前知识库没有足够依据", answer)

    def test_how_to_logistics_question_does_not_use_selected_context(self):
        """操作步骤咨询不能被选中订单或工单上下文劫持为真实查询。"""
        agent = object.__new__(CustomerServiceAgent)
        draft = IntentResult(
            intent="consult",
            user_goal="policy_consult",
            emotion="normal",
            order_related=False,
            order_no=[],
            product_name=None,
            need_order_query=False,
            need_ticket=False,
            need_human=False,
            priority="low",
            confidence=0.8,
            summary="客户询问物流状态查询方式",
            risk_reasons=[],
        )
        analysis = agent._apply_business_guardrails("怎么查询物流状态", draft)
        calls: dict[str, list[str]] = {"query_order": [], "query_order_logistics": [], "query_ticket_status": []}

        def query_order(order_no, auth_token):
            calls["query_order"].append(order_no)
            return {"status": "success", "query_type": "order_detail", "data": {"orderNo": order_no, "orderStatus": "SIGNED"}}

        def query_order_logistics(order_no, auth_token):
            calls["query_order_logistics"].append(order_no)
            return {"status": "empty", "query_type": "order_logistics", "order_no": order_no}

        def query_ticket_status(ticket_no, auth_token):
            calls["query_ticket_status"].append(ticket_no)
            return {"status": "success", "query_type": "ticket_status", "data": {"ticketNo": ticket_no}}

        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: analysis),
            retrieve_knowledge=lambda _: [],
            query_order=query_order,
            query_customer_orders=lambda customer_id, auth_token: {"status": "empty", "data": []},
            query_order_logistics=query_order_logistics,
            create_ticket=lambda payload, auth_token: {"status": "failed"},
            auto_assign_ticket=lambda ticket_no: {"status": "failed"},
            list_customer_tickets=lambda auth_token: {"status": "success", "data": []},
            query_ticket_status=query_ticket_status,
            urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
            prepare_action=lambda state: {"analysis": state["analysis"], "pending_action_request": None},
            compose_answer=lambda state: agent._compose_answer(state),
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        result = graph.invoke(
            {
                "message": "怎么查询物流状态",
                "selected_order_no": "EC202606220001",
                "selected_ticket_no": "T20260707184244342000",
                "tool_results": [],
                "citations": [],
            }
        )

        self.assertEqual(result["analysis"].intent, "logistics")
        self.assertEqual(result["analysis"].user_goal, "how_to")
        self.assertEqual(calls["query_order"], [])
        self.assertEqual(calls["query_order_logistics"], [])
        self.assertEqual(calls["query_ticket_status"], [])
        self.assertFalse(result["analysis"].need_ticket)
        self.assertIn("您可以这样查询物流状态", result["answer"])

    def test_user_goal_matrix_is_goal_first(self):
        """核心话术先判定用户目标，再决定业务域和工具链。"""
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
            summary="待识别",
            risk_reasons=["low_confidence"],
        )
        cases = [
            ("怎么查询物流状态", "logistics", "how_to", False, False),
            ("帮我查物流", "logistics", "status_query", True, False),
            ("物流到哪了", "logistics", "status_query", True, False),
            ("查看退货规则", "refund", "policy_consult", False, False),
            ("怎么申请退货", "refund", "how_to", False, False),
            ("我要退货", "refund", "action_request", True, True),
            ("你有什么能力", "consult", "info_query", False, False),
            ("这个工单进度怎么样", "consult", "status_query", False, False),
            ("天空为什么是蓝色的", "other", "out_of_scope", False, False),
            ("转人工", "consult", "human_request", False, False),
            ("我要投诉", "complaint", "complaint", False, True),
        ]

        for message, intent, goal, need_order_query, need_ticket in cases:
            with self.subTest(message=message):
                result = agent._apply_business_guardrails(message, draft)
                self.assertEqual(result.intent, intent)
                self.assertEqual(result.user_goal, goal)
                self.assertEqual(result.need_order_query, need_order_query)
                self.assertEqual(result.need_ticket, need_ticket)

    def test_out_of_scope_question_auto_replies_without_ticket(self):
        """普通越界问题可简短回答并说明能力边界，不因知识库缺失转人工。"""
        agent = object.__new__(CustomerServiceAgent)
        captured_payloads = []

        class FakeLLM:
            def generate_customer_reply(self, payload):
                captured_payloads.append(payload)
                return "天空呈蓝色主要是因为大气分子对蓝色光的散射更明显。"

        agent.llm_analyzer = FakeLLM()
        agent._log = lambda *args, **kwargs: None
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
            confidence=0.4,
            summary="普通常识问题",
            risk_reasons=["low_confidence"],
        )
        analysis = agent._apply_business_guardrails("天空为什么是蓝色的", draft)
        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: analysis),
            retrieve_knowledge=lambda _: [],
            query_order=lambda order_no, auth_token: {"status": "empty"},
            query_customer_orders=lambda customer_id, auth_token: {"status": "empty", "data": []},
            query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
            create_ticket=lambda payload, auth_token: {"status": "failed"},
            auto_assign_ticket=lambda ticket_no: {"status": "failed"},
            list_customer_tickets=lambda auth_token: {"status": "success", "data": []},
            query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
            urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
            prepare_action=lambda state: {"analysis": state["analysis"], "pending_action_request": None},
            compose_answer=lambda state: agent._compose_answer(state),
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        result = graph.invoke({"message": "天空为什么是蓝色的", "tool_results": [], "citations": []})

        self.assertEqual(result["analysis"].user_goal, "out_of_scope")
        self.assertFalse(result["need_human"])
        self.assertTrue(result["auto_send"])
        self.assertIsNone(result["ticket_result"])
        self.assertIn("蓝色光", result["answer"])
        self.assertIn("订单", result["answer"])
        self.assertIn("物流", result["answer"])
        self.assertIn("售后", result["answer"])
        self.assertEqual(captured_payloads[0]["reply_mode"], "out_of_scope")
        self.assertIsNone(captured_payloads[0]["order"])
        self.assertEqual(captured_payloads[0]["citations"], [])

    def test_general_out_of_scope_answer_is_generated_by_llm_with_boundary(self):
        """普通越界问题统一由大模型简答，并自动补齐客服能力边界。"""
        agent = object.__new__(CustomerServiceAgent)
        captured_payloads = []

        class FakeLLM:
            def generate_customer_reply(self, payload):
                captured_payloads.append(payload)
                if payload["reply_mode"] == "out_of_scope_boundary":
                    return "如果后面想看订单进度、物流状态或售后处理，我也可以继续帮您查。"
                return "可以：为什么程序员喜欢深色模式？因为光会吸引 bug。"

        agent.llm_analyzer = FakeLLM()
        agent._log = lambda *args, **kwargs: None
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
            confidence=0.9,
            summary="讲个笑话",
            risk_reasons=["out_of_scope"],
        )

        answer = agent._compose_answer(
            {
                "message": "给我讲个笑话",
                "analysis": analysis,
                "tool_results": [{"status": "success", "data": {"orderNo": "EC-IGNORED"}}],
                "citations": [],
            }
        )

        self.assertIn("程序员", answer)
        self.assertIn("如果后面想看订单进度、物流状态或售后处理", answer)
        self.assertNotIn("不过我主要负责", answer)
        self.assertEqual(captured_payloads[0]["reply_mode"], "out_of_scope")
        self.assertEqual(captured_payloads[1]["reply_mode"], "out_of_scope_boundary")
        self.assertIsNone(captured_payloads[0]["order"])
        self.assertIsNone(captured_payloads[0]["logistics"])

    def test_natural_llm_out_of_scope_boundary_is_preserved(self):
        """模型生成了自然业务边界时应直接保留，不强行替换成固定模板。"""
        raw_answer = (
            "海洋偏蓝主要和水体对不同波长光的吸收、散射有关，红橙光更容易被吸收，蓝色光更容易留下来被看见。"
            "如果后面想查订单进度、物流状态或售后处理，我也可以继续帮您看。"
        )

        answer = CustomerServiceAgent._ensure_out_of_scope_boundary(raw_answer)

        self.assertEqual(answer, raw_answer)
        self.assertNotIn("不过我主要负责", answer)

    def test_out_of_scope_boundary_is_normalized_without_question_specific_patch(self):
        """普通越界回复应统一替换突兀身份声明，而不是只修补某个具体问题。"""
        raw_answer = (
            "天空看起来是蓝色，是因为太阳光穿过大气层时，波长较短的蓝光被空气分子散射得更多。\n\n"
            "我是您的企业客服助手，主要处理订单、物流、售后、工单和发票相关问题。如果您有这些方面的疑问，欢迎继续提问。"
        )

        answer = CustomerServiceAgent._ensure_out_of_scope_boundary(raw_answer)

        self.assertIn("天空看起来是蓝色", answer)
        self.assertNotIn("不过我主要负责订单、物流、售后、工单和发票等业务问题", answer)
        self.assertNotIn("我是您的企业客服助手", answer)
        self.assertNotIn("欢迎继续提问", answer)

    def test_out_of_scope_boundary_cleanup_keeps_answer_in_same_paragraph(self):
        """模型把答案和身份边界写在同一段时，也只能移除边界句，不能丢掉答案。"""
        raw_answer = (
            "海洋看起来是蓝色的，主要因为海水和大气对不同颜色光的吸收、散射程度不同。"
            "我是自助客服助手，主要处理订单、物流、售后、工单和发票相关的问题。如果您有这方面的需要，欢迎继续问我。"
        )

        answer = CustomerServiceAgent._ensure_out_of_scope_boundary(raw_answer)

        self.assertIn("海洋看起来是蓝色", answer)
        self.assertIn("吸收、散射程度不同", answer)
        self.assertNotIn("不过我主要负责订单、物流、售后、工单和发票等业务问题", answer)
        self.assertNotIn("我是自助客服助手", answer)
        self.assertNotIn("欢迎继续问我", answer)

    def test_human_request_takes_over_conversation_without_ticket(self):
        """明确转人工请求只接管当前会话，不创建业务工单。"""
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
            confidence=0.8,
            summary="请求转人工",
            risk_reasons=[],
        )
        analysis = agent._apply_business_guardrails("转人工", draft)
        created_payloads = []

        def create_ticket(payload, auth_token):
            created_payloads.append(payload)
            return {"status": "success", "data": {"ticketNo": "T-HUMAN", "status": "PENDING_ASSIGN"}}

        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: analysis),
            retrieve_knowledge=lambda _: [],
            query_order=lambda order_no, auth_token: {"status": "empty"},
            query_customer_orders=lambda customer_id, auth_token: {"status": "empty", "data": []},
            query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
            create_ticket=create_ticket,
            auto_assign_ticket=lambda ticket_no: {"status": "failed"},
            list_customer_tickets=lambda auth_token: {"status": "success", "data": []},
            query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
            urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
            prepare_action=lambda state: {"analysis": state["analysis"], "pending_action_request": None},
            compose_answer=lambda state: agent._compose_answer(state),
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        result = graph.invoke({"message": "转人工", "tool_results": [], "citations": []})

        self.assertEqual(result["analysis"].user_goal, "human_request")
        self.assertTrue(result["need_human"])
        self.assertFalse(result["analysis"].need_ticket)
        self.assertFalse(result["auto_send"])
        self.assertEqual(created_payloads, [])
        self.assertIsNone(result["ticket_result"])
        self.assertIn("已为您转接人工客服", result["answer"])
        self.assertNotIn("工单", result["answer"])
        self.assertNotIn("当前知识库没有足够依据", result["answer"])

    def test_low_risk_no_kb_policy_does_not_transfer_human(self):
        """低风险客服规则问题无知识库命中时，先兜底澄清，不自动转人工。"""
        agent = object.__new__(CustomerServiceAgent)
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
            summary="咨询退款到账",
            risk_reasons=[],
        )
        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: analysis),
            retrieve_knowledge=lambda _: [],
            query_order=lambda order_no, auth_token: {"status": "empty"},
            query_customer_orders=lambda customer_id, auth_token: {"status": "empty", "data": []},
            query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
            create_ticket=lambda payload, auth_token: {"status": "failed"},
            auto_assign_ticket=lambda ticket_no: {"status": "failed"},
            list_customer_tickets=lambda auth_token: {"status": "success", "data": []},
            query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
            urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
            prepare_action=lambda state: {"analysis": state["analysis"], "pending_action_request": None},
            compose_answer=lambda state: agent._compose_answer(state),
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        result = graph.invoke({"message": "退款多久到账", "tool_results": [], "citations": []})

        self.assertFalse(result["need_human"])
        self.assertTrue(result["auto_send"])
        self.assertIsNone(result["ticket_result"])
        self.assertIn("没有找到", result["answer"])
        self.assertNotIn("已转人工客服处理", result["answer"])

    def test_high_risk_out_of_scope_gives_boundary_only(self):
        """高风险越界问题只给安全边界，不提供具体建议也不转人工。"""
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
            confidence=0.4,
            summary="高风险越界问题",
            risk_reasons=["low_confidence"],
        )
        analysis = agent._apply_business_guardrails("心脏疼怎么办", draft)
        answer = agent._compose_answer({"message": "心脏疼怎么办", "analysis": analysis, "tool_results": [], "citations": []})

        self.assertEqual(analysis.user_goal, "out_of_scope")
        self.assertFalse(analysis.need_human)
        self.assertIn("超出了我作为客户服务助手的能力范围", answer)
        self.assertNotIn("当前知识库没有足够依据", answer)


if __name__ == "__main__":
    unittest.main()
