"""对客服意图 SFT 独立测试集执行微调前基线评测。"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from agents.llm_intent_analyzer import LLMIntentAnalyzer
from schemas.intent_schema import LLMIntentDraft


DEFAULT_DATASET_PATH = SERVICE_ROOT / "datasets" / "intent_sft" / "test.jsonl"
METRIC_FIELDS = (
    "intent",
    "user_goal",
    "need_order_query",
    "need_ticket",
    "need_human",
    "next_action",
)


def load_cases(dataset_path: Path) -> list[dict[str, Any]]:
    """读取独立 JSONL 测试集，并提取用户问题和标注答案。"""
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(dataset_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        messages = record["messages"]
        if len(messages) != 3 or messages[1]["role"] != "user" or messages[2]["role"] != "assistant":
            raise ValueError(f"{dataset_path}:{line_number} 不是标准三段式 SFT 样本")
        cases.append(
            {
                "message": messages[1]["content"],
                "expected": LLMIntentDraft.model_validate(json.loads(messages[2]["content"])).model_dump(),
            }
        )
    return cases


def build_report(cases: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    """比较标准答案与模型预测，计算字段准确率和高风险路由召回率。"""
    if len(cases) != len(predictions):
        raise ValueError("预测数量必须与测试样本数量一致")

    field_correct = {field: 0 for field in METRIC_FIELDS}
    high_risk_total = 0
    high_risk_routed = 0
    rows: list[dict[str, Any]] = []

    for case, prediction in zip(cases, predictions, strict=True):
        expected = case["expected"]
        matches = {field: prediction.get(field) == expected.get(field) for field in METRIC_FIELDS}
        for field, matched in matches.items():
            field_correct[field] += int(matched)

        # 人工或工单任一要求为真时，属于不能因模型误判而漏掉的受控场景。
        is_high_risk = bool(expected["need_human"] or expected["need_ticket"])
        if is_high_risk:
            high_risk_total += 1
            high_risk_routed += int(bool(prediction.get("need_human") and prediction.get("need_ticket")))

        rows.append(
            {
                "message": case["message"],
                "expected": {field: expected[field] for field in METRIC_FIELDS},
                "prediction": {field: prediction.get(field) for field in METRIC_FIELDS},
                "matches": matches,
            }
        )

    total = len(cases)
    return {
        "sample_count": total,
        "field_accuracy": {field: round(correct / total, 4) if total else 0 for field, correct in field_correct.items()},
        "high_risk_route_recall": round(high_risk_routed / high_risk_total, 4) if high_risk_total else None,
        "high_risk_sample_count": high_risk_total,
        "rows": rows,
    }


def main() -> int:
    """调用当前生产分析器，输出可作为微调对照组的基线报告。"""
    parser = argparse.ArgumentParser(description="评测当前客服意图模型在 SFT 测试集上的基线表现")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH, help="独立测试集 JSONL 路径")
    parser.add_argument("--output", type=Path, help="可选的报告输出 JSON 路径")
    args = parser.parse_args()

    if not LLMIntentAnalyzer.is_configured():
        print("未配置 LLM API Key，无法执行在线基线评测。请配置后重试。", file=sys.stderr)
        return 2

    cases = load_cases(args.dataset)
    analyzer = LLMIntentAnalyzer()
    predictions = [analyzer.invoke(case["message"]).model_dump() for case in cases]
    report = build_report(cases, predictions)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
