"""加载 QLoRA Adapter，并在独立意图测试集上执行离线评测。"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import torch
from peft import PeftConfig, PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig


SERVICE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ADAPTER_DIR = SERVICE_ROOT / "artifacts" / "intent_sft" / "customer-intent-lora"
DEFAULT_TEST_FILE = SERVICE_ROOT / "datasets" / "intent_sft" / "test.jsonl"
METRIC_FIELDS = (
    "intent",
    "user_goal",
    "need_order_query",
    "need_ticket",
    "need_human",
    "next_action",
)
VALID_INTENTS = {"consult", "logistics", "refund", "exchange", "repair", "complaint", "invoice", "member", "other"}
VALID_USER_GOALS = {"policy_consult", "how_to", "status_query", "action_request", "human_request", "out_of_scope", "complaint", "dispute", "info_query", "other"}
VALID_NEXT_ACTIONS = {"collect_slots", "validate_order", "call_business_tool", "create_ticket", "ask_clarification", "transfer_human", "cancel_pending", "unsupported", None}


def parse_arguments() -> argparse.Namespace:
    """读取 Adapter 与测试集路径。"""
    parser = argparse.ArgumentParser(description="评测客服意图 QLoRA Adapter")
    parser.add_argument("--adapter-dir", type=Path, default=DEFAULT_ADAPTER_DIR)
    parser.add_argument("--test-file", type=Path, default=DEFAULT_TEST_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_ADAPTER_DIR / "adapter_evaluation.json")
    return parser.parse_args()


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型回答中提取 JSON，并验证关键意图字段是否符合训练契约。"""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("intent") not in VALID_INTENTS or payload.get("user_goal") not in VALID_USER_GOALS:
        return None
    if payload.get("next_action") not in VALID_NEXT_ACTIONS:
        return None
    if not all(isinstance(payload.get(field), bool) for field in ("need_order_query", "need_ticket", "need_human")):
        return None
    return payload


def load_cases(dataset_path: Path) -> list[dict[str, Any]]:
    """读取独立测试集，提取用户问题及其人工标注的结构化结果。"""
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(dataset_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        messages = record.get("messages", [])
        if len(messages) != 3 or messages[1].get("role") != "user" or messages[2].get("role") != "assistant":
            raise ValueError(f"{dataset_path}:{line_number} 不是标准三段式 SFT 样本")
        cases.append({"message": messages[1]["content"], "expected": json.loads(messages[2]["content"])})
    return cases


def build_report(cases: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    """统计关键字段准确率与高风险样本的受控路由召回率。"""
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
        if expected["need_human"] or expected["need_ticket"]:
            high_risk_total += 1
            high_risk_routed += int(bool(prediction.get("need_human") and prediction.get("need_ticket")))
        rows.append({"message": case["message"], "expected": {field: expected[field] for field in METRIC_FIELDS}, "prediction": {field: prediction.get(field) for field in METRIC_FIELDS}, "matches": matches})
    total = len(cases)
    return {
        "sample_count": total,
        "field_accuracy": {field: round(correct / total, 4) if total else 0 for field, correct in field_correct.items()},
        "high_risk_route_recall": round(high_risk_routed / high_risk_total, 4) if high_risk_total else None,
        "high_risk_sample_count": high_risk_total,
        "rows": rows,
    }


def load_adapter(adapter_dir: Path):
    """以 4-bit 方式加载基础模型和 LoRA Adapter，降低评测显存占用。"""
    if not torch.cuda.is_available():
        raise RuntimeError("Adapter 评测需要 CUDA GPU，请在云端 Pod 中运行。")
    config = PeftConfig.from_pretrained(adapter_dir)
    compute_dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    quantization_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=compute_dtype,
    )
    tokenizer = AutoTokenizer.from_pretrained(adapter_dir)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    base_model = AutoModelForCausalLM.from_pretrained(
        config.base_model_name_or_path,
        quantization_config=quantization_config,
        device_map="auto",
        torch_dtype=compute_dtype,
    )
    return PeftModel.from_pretrained(base_model, adapter_dir), tokenizer


def main() -> int:
    """对独立测试集做贪婪生成，并输出字段指标和 JSON 合法率。"""
    args = parse_arguments()
    model, tokenizer = load_adapter(args.adapter_dir)
    model.eval()
    cases = load_cases(args.test_file)
    predictions: list[dict[str, Any]] = []
    schema_valid_count = 0

    for case in cases:
        messages = [
            {
                "role": "system",
                "content": "你是企业客服意图识别节点。根据用户输入输出合法 JSON，不输出解释或 Markdown。字段必须符合既定客服意图 Schema。涉及投诉、争议、人工转接或真实业务动作时，必须标记为需要人工处理。",
            },
            {"role": "user", "content": case["message"]},
        ]
        inputs = tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
        inputs = inputs.to(model.get_input_embeddings().weight.device)
        with torch.inference_mode():
            output_ids = model.generate(inputs, max_new_tokens=512, do_sample=False, pad_token_id=tokenizer.eos_token_id)
        generated = tokenizer.decode(output_ids[0][inputs.shape[1]:], skip_special_tokens=True)
        payload = extract_json(generated)
        if payload is not None:
            schema_valid_count += 1
            predictions.append(payload)
        else:
            # 非法 JSON 应计入所有字段错误，并在报告中单独体现。
            predictions.append({})

    report = build_report(cases, predictions)
    report["schema_valid_rate"] = round(schema_valid_count / len(cases), 4) if cases else 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
