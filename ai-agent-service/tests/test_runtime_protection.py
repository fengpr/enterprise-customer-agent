"""验证高并发在线保护组件。"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.runtime_protection import AdmissionController, RuntimeMetrics


class RuntimeProtectionTest(unittest.TestCase):
    """覆盖本地开发模式下的并发闸门和指标输出。"""

    def setUp(self) -> None:
        """避免本机环境变量将单元测试连接到外部 Redis。"""
        self.previous_redis_url = os.environ.pop("REDIS_URL", None)

    def tearDown(self) -> None:
        """恢复测试前环境变量，避免影响其它测试模块。"""
        if self.previous_redis_url:
            os.environ["REDIS_URL"] = self.previous_redis_url

    def test_admission_limits_subject_and_releases_slot(self):
        """单主体达到并发上限时拒绝，释放后可再次进入。"""
        controller = AdmissionController()
        controller.global_limit = 1
        controller.per_subject_limit = 1
        controller._global = __import__("threading").BoundedSemaphore(1)

        self.assertTrue(controller.try_acquire("customer-1"))
        self.assertFalse(controller.try_acquire("customer-1"))
        controller.release("customer-1")
        self.assertTrue(controller.try_acquire("customer-1"))

    def test_metrics_exposes_prometheus_text(self):
        """监控端点应包含请求计数、降级次数和延迟指标。"""
        metrics = RuntimeMetrics()
        metrics.observe("/api/agent/reply", 200, 123)
        metrics.mark_degraded()

        text = metrics.prometheus()

        self.assertIn("agent_http_requests_total", text)
        self.assertIn("agent_degraded_total 1", text)


if __name__ == "__main__":
    unittest.main()
