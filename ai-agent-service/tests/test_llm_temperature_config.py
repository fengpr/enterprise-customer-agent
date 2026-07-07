"""验证意图分析和客户回复使用独立的大模型温度配置。"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.runnables import RunnableLambda

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.llm_intent_analyzer import LLMIntentAnalyzer


def _reply_payload() -> dict:
    """构造回复生成所需的最小安全上下文。"""
    return {
        "message": "退款多久到账",
        "intent": "refund",
        "user_goal": "policy_consult",
        "summary": "客户咨询退款到账时间",
        "reply_mode": "policy_consult",
        "service_instruction": "回答退款到账规则。",
        "citations": [],
        "order": None,
        "logistics": None,
        "ticket": None,
        "extra_context": {},
    }


class LLMTemperatureConfigTest(unittest.TestCase):
    """确保结构化分析和客户回复不会继续共用单一 LLM_TEMPERATURE。"""

    def test_default_temperatures_are_split(self):
        """未配置新变量时，分析默认 0，回复默认 0.3。"""
        temperatures: list[float] = []

        def fake_build_llm(_self, temperature: float):
            temperatures.append(temperature)
            return RunnableLambda(lambda _prompt: "好的，我会根据现有信息为您说明。")

        with patch.dict(os.environ, {"LLM_API_KEY": "test-key"}, clear=True):
            with patch.object(LLMIntentAnalyzer, "_build_llm", fake_build_llm):
                analyzer = LLMIntentAnalyzer()
                analyzer.generate_customer_reply(_reply_payload())

        self.assertEqual(analyzer.analysis_temperature, 0)
        self.assertEqual(analyzer.response_temperature, 0)
        self.assertEqual(temperatures, [0, 0])

    def test_configured_temperatures_are_used_separately(self):
        """配置两个新变量时，分析链和回复链分别使用自己的温度。"""
        temperatures: list[float] = []

        def fake_build_llm(_self, temperature: float):
            temperatures.append(temperature)
            return RunnableLambda(lambda _prompt: "这是一条自然的客户回复。")

        with patch.dict(
            os.environ,
            {
                "LLM_API_KEY": "test-key",
                "LLM_ANALYSIS_TEMPERATURE": "0",
                "LLM_RESPONSE_TEMPERATURE": "0.3",
                "LLM_TEMPERATURE": "0.9",
            },
            clear=True,
        ):
            with patch.object(LLMIntentAnalyzer, "_build_llm", fake_build_llm):
                analyzer = LLMIntentAnalyzer()
                analyzer.generate_customer_reply(_reply_payload())

        self.assertEqual(analyzer.analysis_temperature, 0)
        self.assertEqual(analyzer.response_temperature, 0.3)
        # 旧 LLM_TEMPERATURE 不再控制分析或回复链路。
        self.assertEqual(temperatures, [0, 0.3])


if __name__ == "__main__":
    unittest.main()
