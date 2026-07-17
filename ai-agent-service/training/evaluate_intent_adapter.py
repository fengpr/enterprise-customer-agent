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
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from schemas.intent_schema import LLMIntentDraft
from scripts.evaluate_intent_sft_baseline import build_report, load_cases


DEFAULT_ADAPTER_DIR = SERVICE_ROOT / "artifacts" / "intent_sft" / "customer-intent-lora"
DEFAULT_TEST_FILE = SERVICE_ROOT / "datasets" / "intent_sft" / "test.jsonl"


def parse_arguments() -> argparse.Namespace:
    """读取 Adapter 与测试集路径。"""
    parser = argparse.ArgumentParser(description="评测客服意图 QLoRA Adapter")
    parser.add_argument("--adapter-dir", type=Path, default=DEFAULT_ADAPTER_DIR)
    parser.add_argument("--test-file", type=Path, default=DEFAULT_TEST_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_ADAPTER_DIR / "adapter_evaluation.json")
    return parser.parse_args()


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型回答中提取 JSON，并验证其能否通过当前意图草稿 Schema。"""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        return LLMIntentDraft.model_validate(json.loads(match.group(0))).model_dump()
    except (json.JSONDecodeError, ValueError):
        return None


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
