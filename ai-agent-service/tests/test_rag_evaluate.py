"""验证 RAG 离线评估集和评估脚本。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.evaluate import evaluate, load_eval_samples, load_kb_chunks
from rag.vector_store import InMemoryVectorStore


BASE_DIR = Path(__file__).resolve().parents[1]
EVAL_DIR = BASE_DIR / "data" / "rag_eval"
KB_DIR = BASE_DIR / "data" / "kb_sources"


class RagEvaluateTest(unittest.TestCase):
    """覆盖评估样本解析、知识加载和关键业务命中。"""

    def test_eval_samples_are_parseable(self):
        """所有 JSONL 评估样本都能解析为结构化样本。"""
        samples = load_eval_samples(str(EVAL_DIR))

        self.assertEqual(len(samples), 40)
        self.assertTrue(all(sample.query and sample.expected_doc for sample in samples))

    def test_kb_sources_load_into_chunks(self):
        """评估脚本应从 kb_sources 生成带 metadata 的 chunk。"""
        chunks = load_kb_chunks(str(KB_DIR))

        self.assertTrue(chunks)
        self.assertTrue(any(chunk.doc_name == "refund_arrival_policy" for chunk in chunks))
        self.assertTrue(any(chunk.collection == "complaint_policy" and chunk.risk_level == "high" for chunk in chunks))

    def test_refund_query_hits_refund_policy(self):
        """退款评估问题应命中退款知识集合。"""
        store = InMemoryVectorStore(load_kb_chunks(str(KB_DIR)))

        results = store.similarity_search(
            "退款多久到账？",
            intent="refund",
            user_goal="policy_consult",
            business_scope="refund",
        )

        self.assertTrue(results)
        self.assertEqual(results[0].collection, "refund_policy")
        self.assertEqual(results[0].business_scope, "refund")

    def test_complaint_query_hits_high_risk_knowledge(self):
        """投诉赔付问题应命中高风险投诉知识。"""
        store = InMemoryVectorStore(load_kb_chunks(str(KB_DIR)))

        results = store.similarity_search(
            "赔我损失，不然我起诉",
            intent="complaint",
            user_goal="complaint",
            business_scope="complaint",
        )

        self.assertTrue(results)
        self.assertEqual(results[0].collection, "complaint_policy")
        self.assertEqual(results[0].risk_level, "high")

    def test_evaluate_returns_metrics_and_failures(self):
        """评估脚本默认只返回报告，不因为失败样本中断。"""
        report = evaluate(eval_dir=str(EVAL_DIR), kb_dir=str(KB_DIR))

        self.assertIn("metrics", report)
        self.assertIn("failures", report)
        self.assertEqual(report["metrics"]["total"], 40)
        self.assertEqual(report["metrics"]["no_hit_count"], 0)


if __name__ == "__main__":
    unittest.main()
