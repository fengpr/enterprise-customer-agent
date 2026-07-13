"""验证 RAG 离线评估集和评估脚本。"""

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_return_goods_query_hits_return_goods_policy(self):
        """退货规则样本应使用文档级 scope，避免退款到账文档抢占首位。"""
        store = InMemoryVectorStore(load_kb_chunks(str(KB_DIR)))

        results = store.similarity_search(
            "我想退货，这个商品能退吗",
            intent="refund",
            user_goal="policy_consult",
            business_scope="return_goods",
        )

        self.assertTrue(results)
        self.assertEqual(results[0].doc_name, "return_goods_policy")
        self.assertEqual(results[0].business_scope, "return_goods")

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
        self.assertIn("answer_groundedness", report["metrics"])
        self.assertIn("hallucination_count", report["metrics"])

    def test_evaluate_supports_generation_and_llm_judge(self):
        """端到端评测应接受回答生成器与独立 LLM 裁判，并保存生成阶段结果。"""
        judge_payloads = []

        def answer_generator(sample, citations):
            return f"{citations[0].paragraph}【来源：{citations[0].citation_id}】"

        def judge(payload):
            judge_payloads.append(payload)
            return {"answer_correctness": 5, "hallucination": False}

        report = evaluate(str(EVAL_DIR), str(KB_DIR), answer_generator=answer_generator, judge=judge)

        self.assertEqual(report["metrics"]["llm_judged_count"], 40)
        self.assertEqual(len(judge_payloads), 40)

    def test_evaluate_supports_parallel_sample_execution(self):
        """黄金集并发执行时应保持全部样本输出顺序，避免串行模型调用拖慢小批量评测。"""
        def answer_generator(sample, citations):
            time.sleep(0.01)
            return f"{citations[0].paragraph}【来源：{citations[0].citation_id}】"

        report = evaluate(str(EVAL_DIR), str(KB_DIR), max_samples=3, max_workers=3, answer_generator=answer_generator)

        self.assertEqual(report["metrics"]["total"], 3)

    def test_deepeval_timeout_keeps_agent_evaluation_report(self):
        """Judge 超时只能标记 DeepEval 技术异常，不能令已完成的 Agent 评测任务整体失败。"""
        with patch("rag.deepeval_adapter.evaluate_golden_case", side_effect=TimeoutError("Request timed out.")):
            report = evaluate(
                str(EVAL_DIR),
                str(KB_DIR),
                max_samples=1,
                use_deepeval=True,
                answer_generator=lambda sample, citations: f"{citations[0].paragraph}【来源：{citations[0].citation_id}】",
            )

        self.assertEqual(report["metrics"]["deepeval_error_count"], 1)
        self.assertEqual(report["metrics"]["failed_count"], 0)

    def test_required_fact_allows_equivalent_phrases(self):
        """黄金样本可为同一事实配置可接受的等价自然语言表达。"""
        sample = next(item for item in load_eval_samples(str(EVAL_DIR)) if item.query == "我要投诉你们")

        self.assertEqual(sample.required_facts[0], ["转人工", "转交人工", "人工专员", "人工团队"])


if __name__ == "__main__":
    unittest.main()
