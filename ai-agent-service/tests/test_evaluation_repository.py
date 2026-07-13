"""验证在线 Trace 采集不调用 Judge，且采样、脱敏、队列状态可恢复。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from repositories.evaluation_repository import EvaluationRepository


class EvaluationRepositoryTest(unittest.TestCase):
    """覆盖企业级评测队列最关键的数据治理与采样行为。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.previous_budget = os.environ.get("EVAL_DAILY_BUDGET")
        os.environ["EVAL_DAILY_BUDGET"] = "200"
        self.repository = EvaluationRepository(str(Path(self.tempdir.name) / "evaluation.db"))

    def tearDown(self):
        if self.previous_budget is None: os.environ.pop("EVAL_DAILY_BUDGET", None)
        else: os.environ["EVAL_DAILY_BUDGET"] = self.previous_budget
        self.tempdir.cleanup()

    def test_high_risk_trace_is_always_queued_and_sanitized(self):
        """人工审核场景必须入队，且订单号、手机号和邮箱不得落入评测 Trace。"""
        saved = self.repository.capture_online_trace({
            "customer_id": 1, "message": "订单 EC202607120001 手机13800138000 邮箱 a@b.com，我要投诉", "answer": "已转人工处理。",
            "analysis": {"need_human": True, "risk_reasons": ["complaint"]}, "citations": [], "tool_results": [],
        })
        claimed = self.repository.claim_pending(1)[0]

        self.assertEqual(saved["status"], "PENDING")
        self.assertEqual(claimed["payload"]["input"], "订单 [ORDER] 手机[PHONE] 邮箱 [EMAIL]，我要投诉")
        self.assertNotIn("customer_id", claimed["payload"])

    def test_normal_sampling_is_stable(self):
        """同一普通输入的采样决定必须稳定，避免重复请求得到不同结果。"""
        payload = {"customer_id": 2, "message": "会员权益有哪些", "answer": "可查看会员中心。", "analysis": {"confidence": 0.9, "risk_reasons": []}, "citations": [], "tool_results": []}
        first = self.repository.capture_online_trace(payload)
        second = self.repository.capture_online_trace(payload)

        self.assertEqual(first["status"], second["status"])
        self.assertEqual(first["sampling_reason"], second["sampling_reason"])

    def test_budget_marks_trace_as_skipped(self):
        """超过每日预算时必须保留 Trace 状态但不能继续进入 Judge 队列。"""
        os.environ["EVAL_DAILY_BUDGET"] = "0"
        saved = self.repository.capture_online_trace({"customer_id": 3, "message": "我要人工", "answer": "已转人工", "analysis": {"need_human": True}, "citations": [], "tool_results": []})

        self.assertEqual(saved["status"], "SKIPPED_BUDGET")

    def test_golden_job_is_persistent_and_completes(self):
        """黄金集任务必须脱离 FastAPI 内存保存，Worker 重启后仍可领取和读取报告。"""
        created = self.repository.create_job("GOLDEN", {"max_samples": 12})
        claimed = self.repository.claim_job()
        self.repository.complete_job(created["job_id"], report={"metrics": {"total": 1}})

        saved = self.repository.get_job(created["job_id"])
        self.assertEqual(claimed["job_id"], created["job_id"])
        self.assertEqual(claimed["payload"]["max_samples"], 12)
        self.assertEqual(saved["status"], "SUCCEEDED")
        self.assertEqual(saved["report"]["metrics"]["total"], 1)


if __name__ == "__main__":
    unittest.main()
