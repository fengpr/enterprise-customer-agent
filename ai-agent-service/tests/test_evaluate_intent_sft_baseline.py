"""验证意图 SFT 基线评测的统计口径。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.evaluate_intent_sft_baseline import build_report


def test_build_report_calculates_field_accuracy_and_high_risk_recall():
    """高风险样本只有同时转人工和建工单时才计为路由成功。"""
    cases = [
        {
            "message": "退款多久到账？",
            "expected": {
                "intent": "refund",
                "user_goal": "policy_consult",
                "need_order_query": False,
                "need_ticket": False,
                "need_human": False,
                "next_action": None,
            },
        },
        {
            "message": "我要投诉少发商品。",
            "expected": {
                "intent": "complaint",
                "user_goal": "complaint",
                "need_order_query": True,
                "need_ticket": True,
                "need_human": True,
                "next_action": "create_ticket",
            },
        },
    ]
    predictions = [
        dict(cases[0]["expected"]),
        {
            **cases[1]["expected"],
            "need_ticket": False,
        },
    ]

    report = build_report(cases, predictions)

    assert report["field_accuracy"]["intent"] == 1.0
    assert report["field_accuracy"]["need_ticket"] == 0.5
    assert report["high_risk_route_recall"] == 0.0
