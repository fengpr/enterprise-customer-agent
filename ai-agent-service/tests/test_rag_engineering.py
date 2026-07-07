"""验证第一阶段 RAG 工程化基线：清洗、chunk、metadata filter、BM25 和风险分级。"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from langchain_core.runnables import RunnableLambda

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graphs.ticket_process_graph import build_ticket_process_graph
from rag.rag_chain import RagChain
from rag.text_processing import KnowledgeChunk, clean_knowledge_text, split_into_chunks
from rag.vector_store import InMemoryVectorStore
from schemas.intent_schema import Citation, IntentResult


def _analysis(**overrides):
    """构造测试用结构化意图。"""
    data = {
        "intent": "refund",
        "user_goal": "policy_consult",
        "emotion": "normal",
        "order_related": False,
        "order_no": [],
        "product_name": None,
        "need_order_query": False,
        "need_ticket": False,
        "need_human": False,
        "priority": "medium",
        "confidence": 0.9,
        "summary": "客户咨询政策",
        "risk_reasons": [],
    }
    data.update(overrides)
    return IntentResult(**data)


class RagEngineeringTest(unittest.TestCase):
    def setUp(self):
        """RAG 工程基线测试固定使用 memory 后端，避免本地 .env 影响断言。"""
        os.environ["RAG_STORE_BACKEND"] = "memory"

    """覆盖 RAG 第一阶段的知识工程和检索行为。"""

    def test_clean_text_removes_internal_notes(self):
        """文档清洗应去掉内部审批备注并压缩空白。"""
        raw = """
# 退款规则
内部备注：该规则仍在审批中，不能给客户看

退款审核通过后 1-7 个工作日原路退回。
第 1 页 / 共 3 页
"""
        cleaned = clean_knowledge_text(raw)

        self.assertIn("退款审核通过后", cleaned)
        self.assertNotIn("内部备注", cleaned)
        self.assertNotIn("第 1 页", cleaned)

    def test_chunk_keeps_heading_and_metadata(self):
        """chunk 应保留标题层级和业务 metadata。"""
        chunks = split_into_chunks(
            "# 售后政策\n## 退款到账\n退款审核通过后 1-7 个工作日原路退回。",
            doc_name="退款政策",
            version="V1.0",
            collection="refund_policy",
            business_scope="refund",
            answerable_intents=["refund"],
            risk_level="medium",
        )

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].heading_path, ["售后政策", "退款到账"])
        self.assertEqual(chunks[0].business_scope, "refund")
        self.assertEqual(chunks[0].risk_level, "medium")

    def test_metadata_filter_prevents_scope_pollution(self):
        """相似文本不能跨 business_scope 污染检索结果。"""
        store = InMemoryVectorStore(
            [
                KnowledgeChunk(
                    doc_name="退款政策",
                    version="V1",
                    paragraph="退款申请需要审核，到账时间以支付渠道为准。",
                    collection="refund_policy",
                    business_scope="refund",
                    answerable_intents=["refund"],
                ),
                KnowledgeChunk(
                    doc_name="发票政策",
                    version="V1",
                    paragraph="发票申请需要审核，抬头和税号需要准确填写。",
                    collection="invoice_policy",
                    business_scope="invoice",
                    answerable_intents=["invoice"],
                ),
            ]
        )

        results = store.similarity_search("申请需要审核", intent="invoice", business_scope="invoice")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].business_scope, "invoice")
        self.assertEqual(results[0].doc_name, "发票政策")

    def test_customer_invisible_policy_chunks_are_filtered(self):
        """检索结果不能返回适用范围、典型表达、检索提示或 AI 内部约束片段。"""
        store = InMemoryVectorStore(
            [
                KnowledgeChunk(
                    doc_name="退货规则",
                    version="V1",
                    paragraph="本文档适用于客户咨询退货条件。适合回答的典型表达包括：“我想退货”。",
                    collection="refund_policy",
                    business_scope="return_goods",
                    answerable_intents=["refund", "consult"],
                    heading_path=["退货条件政策", "适用范围"],
                ),
                KnowledgeChunk(
                    doc_name="退货规则",
                    version="V1",
                    paragraph="签收后 7 天内且商品不影响二次销售时，可申请退货；是否通过以售后审核结果为准。",
                    collection="refund_policy",
                    business_scope="return_goods",
                    answerable_intents=["refund", "consult"],
                    heading_path=["退货条件政策", "客户可见规则"],
                ),
                KnowledgeChunk(
                    doc_name="退货规则",
                    version="V1",
                    paragraph="AI 应说明需要结合商品规则和订单状态判断，不得直接承诺一定可退。",
                    collection="refund_policy",
                    business_scope="return_goods",
                    answerable_intents=["refund", "consult"],
                    heading_path=["退货条件政策", "内部处理分支"],
                ),
            ]
        )

        results = store.similarity_search(
            "查看退货规则",
            intent="refund",
            user_goal="policy_consult",
            business_scope="return_goods",
        )

        self.assertEqual(len(results), 1)
        self.assertIn("签收后 7 天内", results[0].paragraph)
        self.assertNotIn("适用范围", results[0].paragraph)
        self.assertNotIn("AI 应", results[0].paragraph)

    def test_expired_and_unpublished_chunks_are_filtered(self):
        """草稿、下线或过期 chunk 不参与检索。"""
        store = InMemoryVectorStore(
            [
                KnowledgeChunk(
                    doc_name="旧退款政策",
                    version="V0",
                    paragraph="退款多久到账",
                    collection="refund_policy",
                    business_scope="refund",
                    answerable_intents=["refund"],
                    expire_time=datetime.utcnow() - timedelta(days=1),
                ),
                KnowledgeChunk(
                    doc_name="草稿退款政策",
                    version="Draft",
                    paragraph="退款多久到账",
                    collection="refund_policy",
                    business_scope="refund",
                    answerable_intents=["refund"],
                    status="DRAFT",
                ),
                KnowledgeChunk(
                    doc_name="新退款政策",
                    version="V1",
                    paragraph="退款审核通过后通常 1-7 个工作日到账。",
                    collection="refund_policy",
                    business_scope="refund",
                    answerable_intents=["refund"],
                ),
            ]
        )

        results = store.similarity_search("退款多久到账", intent="refund", business_scope="refund")

        self.assertEqual([item.doc_name for item in results], ["新退款政策"])

    def test_rag_chain_retrieves_by_structured_payload(self):
        """RagChain 支持结构化 payload，按 intent/scope 检索。"""
        rag = RagChain()

        results = rag.retrieve({"query": "退款多久到账", "intent": "refund", "user_goal": "policy_consult"})

        self.assertTrue(results)
        self.assertEqual(results[0].business_scope, "refund")
        self.assertEqual(results[0].retrieval_source, "bm25_rerank")
        self.assertIn("rewritten_query", results[0].metadata)

    def test_high_risk_citation_forces_human_review(self):
        """高风险知识片段命中时，Agent 不应自动发送。"""
        graph = build_ticket_process_graph(
            analyzer_chain=RunnableLambda(lambda _: _analysis(intent="complaint", user_goal="complaint", need_human=False, need_ticket=False)),
            retrieve_knowledge=lambda _: [
                Citation(
                    doc_name="投诉处理",
                    version="V1",
                    paragraph="投诉和赔付必须人工处理。",
                    score=0.9,
                    risk_level="high",
                    business_scope="complaint",
                    collection="complaint_policy",
                )
            ],
            query_order=lambda order_no, auth_token: {"status": "empty"},
            query_customer_orders=lambda customer_id, auth_token: {"status": "empty", "data": []},
            query_order_logistics=lambda order_no, auth_token: {"status": "empty"},
            create_ticket=lambda payload, auth_token: {"status": "success", "data": {"ticketNo": "T1", "status": "PENDING_ASSIGN"}},
            auto_assign_ticket=lambda ticket_no: {"status": "failed"},
            list_customer_tickets=lambda auth_token: {"status": "success", "data": []},
            query_ticket_status=lambda ticket_no, auth_token: {"status": "empty"},
            urge_ticket=lambda ticket_no, reason, auth_token: {"status": "empty"},
            prepare_action=lambda state: {"analysis": state["analysis"]},
            compose_answer=lambda state: "ok",
            log_tool_call=lambda tool_name, input_data, output_data: None,
        )

        result = graph.invoke({"message": "我要投诉并要求赔付", "tool_results": [], "citations": []})

        self.assertTrue(result["need_human"])
        self.assertIn("high_risk_knowledge", result["risk_reasons"])


if __name__ == "__main__":
    unittest.main()
