"""验证 RAG 生成阶段的引用校验、幻觉检测和 LLM 裁判接入。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.quality import LLMJudge, ensure_citation_ids, evaluate_answer_groundedness
from schemas.intent_schema import Citation


class RagQualityTest(unittest.TestCase):
    """覆盖回答必须引用本轮证据、伪造引用和裁判 JSON 解析。"""

    def setUp(self):
        """构造一条可复用的退款政策证据。"""
        self.citation = Citation(
            doc_name="退款到账政策",
            version="V1",
            paragraph="退款审核通过后通常 1-7 个工作日原路退回，具体到账时间以支付渠道处理为准。",
            score=0.9,
        )
        ensure_citation_ids([self.citation])

    def test_supported_claim_with_retrieved_citation_passes(self):
        """事实结论引用本轮真实片段且文本一致时，通过有据性校验。"""
        answer = f"退款审核通过后通常 1-7 个工作日原路退回。【来源：{self.citation.citation_id}】"

        result = evaluate_answer_groundedness(answer, [self.citation])

        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["hallucination_detected"])
        self.assertEqual(result["groundedness"], 1.0)

    def test_unsupported_claim_is_detected(self):
        """回答编造证据中不存在的具体到账承诺时，必须识别为幻觉。"""
        answer = f"退款会在 24 小时内到账。【来源：{self.citation.citation_id}】"

        result = evaluate_answer_groundedness(answer, [self.citation])

        self.assertTrue(result["hallucination_detected"])
        self.assertEqual(result["unsupported_claims"][0]["reason"], "missing_or_unrelated_citation")

    def test_fabricated_citation_is_detected(self):
        """模型输出不存在的片段 ID 时，不能被当作有效引用。"""
        result = evaluate_answer_groundedness("退款审核通过后会到账。【来源：kb-000000000000】", [self.citation])

        self.assertTrue(result["hallucination_detected"])
        self.assertEqual(result["unsupported_claims"][0]["reason"], "invalid_citation")

    def test_complaint_transfer_claim_with_citation_passes(self):
        """投诉转人工属于业务断言，带真实引用时必须进入并通过校验。"""
        self.citation.paragraph = "客户提出投诉时必须转人工处理，由人工客服进一步核实。"
        answer = f"您的投诉已记录，现转交人工客服处理。【来源：{self.citation.citation_id}】"

        result = evaluate_answer_groundedness(answer, [self.citation])

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["claim_count"], 1)

    def test_llm_judge_returns_structured_result(self):
        """LLM 裁判应把模型 JSON 转成可聚合的结构化结果。"""
        judge = LLMJudge(lambda _: '{"answer_correctness": 5, "hallucination": false, "unsupported_claims": []}')

        result = judge({"query": "退款多久到账", "answer": "示例"})

        self.assertEqual(result["answer_correctness"], 5)
        self.assertFalse(result["hallucination"])


if __name__ == "__main__":
    unittest.main()
