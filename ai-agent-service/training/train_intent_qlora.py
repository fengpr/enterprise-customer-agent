"""使用 QLoRA 训练客服意图结构化输出 Adapter。"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch
from datasets import Dataset, load_dataset
from peft import LoraConfig, TaskType, get_peft_model, prepare_model_for_kbit_training
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, Trainer, TrainingArguments


SERVICE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET_DIR = SERVICE_ROOT / "datasets" / "intent_sft"
DEFAULT_OUTPUT_DIR = SERVICE_ROOT / "artifacts" / "intent_sft" / "customer-intent-lora"


@dataclass
class AssistantOnlyDataCollator:
    """将不同长度的样本补齐，并屏蔽系统提示与用户输入的损失。"""

    tokenizer: Any

    def __call__(self, features: list[dict[str, list[int]]]) -> dict[str, torch.Tensor]:
        """按当前批次最长序列补齐 input、attention mask 与标签。"""
        max_length = max(len(feature["input_ids"]) for feature in features)
        input_ids, attention_masks, labels = [], [], []
        for feature in features:
            padding = max_length - len(feature["input_ids"])
            input_ids.append(feature["input_ids"] + [self.tokenizer.pad_token_id] * padding)
            attention_masks.append(feature["attention_mask"] + [0] * padding)
            # 非回答区域与补齐区域均为 -100，不参与交叉熵损失计算。
            labels.append(feature["labels"] + [-100] * padding)
        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "attention_mask": torch.tensor(attention_masks, dtype=torch.long),
            "labels": torch.tensor(labels, dtype=torch.long),
        }


def parse_arguments() -> argparse.Namespace:
    """读取训练配置，默认值面向首个客服意图 QLoRA 实验。"""
    parser = argparse.ArgumentParser(description="训练客服意图 JSON 输出的 QLoRA Adapter")
    parser.add_argument("--model-name", default="Qwen/Qwen2.5-3B-Instruct", help="Hugging Face 基础模型名称")
    parser.add_argument("--train-file", type=Path, default=DEFAULT_DATASET_DIR / "train.jsonl")
    parser.add_argument("--validation-file", type=Path, default=DEFAULT_DATASET_DIR / "validation.jsonl")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-length", type=int, default=1024)
    parser.add_argument("--epochs", type=float, default=3)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=16)
    return parser.parse_args()


def build_tokenize_function(tokenizer: Any, max_length: int):
    """构建将三段式 SFT 样本转成仅监督助手回答 Token 的函数。"""

    def tokenize_example(example: dict[str, Any]) -> dict[str, list[int]]:
        messages = example["messages"]
        roles = [message.get("role") for message in messages]
        if roles != ["system", "user", "assistant"]:
            raise ValueError(f"训练样本必须是 system、user、assistant 三段消息，实际为：{roles}")

        # Chat Template 负责生成与基础 Instruct 模型匹配的系统与用户提示。
        prompt_ids = tokenizer.apply_chat_template(messages[:-1], tokenize=True, add_generation_prompt=True)
        answer_text = messages[-1]["content"] + tokenizer.eos_token
        answer_ids = tokenizer(answer_text, add_special_tokens=False)["input_ids"]
        input_ids = (prompt_ids + answer_ids)[:max_length]
        prompt_length = min(len(prompt_ids), max_length)
        labels = [-100] * prompt_length + input_ids[prompt_length:]
        return {
            "input_ids": input_ids,
            "attention_mask": [1] * len(input_ids),
            "labels": labels,
        }

    return tokenize_example


def load_and_tokenize_dataset(path: Path, tokenizer: Any, max_length: int) -> Dataset:
    """读取 JSONL 数据集并预处理为模型训练所需的 Token 字段。"""
    dataset = load_dataset("json", data_files=str(path), split="train")
    tokenized = dataset.map(
        build_tokenize_function(tokenizer, max_length),
        remove_columns=dataset.column_names,
        desc=f"处理 {path.name}",
    )
    # 只保留仍包含助手回答 Token 的样本，防止超长提示挤掉监督目标。
    return tokenized.filter(lambda item: any(token != -100 for token in item["labels"]))


def build_model(model_name: str):
    """以 NF4 量化加载基础模型，并仅开放 LoRA Adapter 参数参与训练。"""
    compute_dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    quantization_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=compute_dtype,
    )
    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        quantization_config=quantization_config,
        device_map="auto",
        torch_dtype=compute_dtype,
    )
    model.config.use_cache = False
    model = prepare_model_for_kbit_training(model)
    lora_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    )
    return get_peft_model(model, lora_config)


def main() -> int:
    """执行训练、验证与 Adapter 保存；该脚本必须在具备 CUDA 的云端环境运行。"""
    args = parse_arguments()
    if not torch.cuda.is_available():
        print("未检测到 CUDA GPU。请在云端 NVIDIA GPU Pod 中运行本脚本。", file=sys.stderr)
        return 2
    if not args.train_file.is_file() or not args.validation_file.is_file():
        print("训练集或验证集文件不存在，请先上传 datasets/intent_sft。", file=sys.stderr)
        return 2

    tokenizer = AutoTokenizer.from_pretrained(args.model_name, use_fast=True)
    if tokenizer.pad_token is None:
        # 因果语言模型没有独立补齐符时，复用 EOS，避免批处理填充失败。
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    train_dataset = load_and_tokenize_dataset(args.train_file, tokenizer, args.max_length)
    validation_dataset = load_and_tokenize_dataset(args.validation_file, tokenizer, args.max_length)
    if len(train_dataset) == 0 or len(validation_dataset) == 0:
        print("Token 化后没有可训练样本，请检查样本格式与 max-length。", file=sys.stderr)
        return 2

    model = build_model(args.model_name)
    model.print_trainable_parameters()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    training_args = TrainingArguments(
        output_dir=str(args.output_dir),
        num_train_epochs=args.epochs,
        learning_rate=args.learning_rate,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        gradient_checkpointing=True,
        eval_strategy="epoch",
        save_strategy="epoch",
        logging_steps=5,
        report_to="none",
        fp16=not torch.cuda.is_bf16_supported(),
        bf16=torch.cuda.is_bf16_supported(),
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=validation_dataset,
        data_collator=AssistantOnlyDataCollator(tokenizer),
    )
    trainer.train()
    trainer.save_state()
    model.save_pretrained(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    (args.output_dir / "training_manifest.json").write_text(
        json.dumps(
            {
                "base_model": args.model_name,
                "train_samples": len(train_dataset),
                "validation_samples": len(validation_dataset),
                "max_length": args.max_length,
                "epochs": args.epochs,
                "learning_rate": args.learning_rate,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"训练完成，Adapter 已保存到：{args.output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
