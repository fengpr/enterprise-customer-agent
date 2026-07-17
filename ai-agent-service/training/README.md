# 客服意图 QLoRA 训练包

本目录仅用于离线训练客服意图结构化输出 Adapter。训练输入为 `datasets/intent_sft` 的脱敏数据；不读取线上数据库、Redis、Trace、Authorization 或 Java 业务接口。

## 云端准备

推荐在 Linux + NVIDIA GPU 的云端 Pod 中执行，使用 Python 3.11。将 `datasets/intent_sft` 与本目录上传到 `/workspace/intent-ft`，不要上传 `.env`、真实会话或完整项目数据。

```bash
cd /workspace/intent-ft
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r training/requirements-qlora.txt
```

## 首次小规模训练

当前数据集仅用于跑通训练流程，不代表可上线的业务质量。

```bash
python training/train_intent_qlora.py \
  --train-file datasets/intent_sft/train.jsonl \
  --validation-file datasets/intent_sft/validation.jsonl \
  --output-dir artifacts/intent_sft/customer-intent-lora
```

训练脚本使用 NF4 4-bit 基础模型与 LoRA Adapter；仅助手 JSON Token 参与 Loss。输出目录仅保存 Adapter、Tokenizer、Trainer 状态和训练清单，不保存基础模型副本。

## 独立测试集评测

```bash
python training/evaluate_intent_adapter.py \
  --adapter-dir artifacts/intent_sft/customer-intent-lora \
  --test-file datasets/intent_sft/test.jsonl
```

重点比较 `schema_valid_rate`、字段准确率和 `high_risk_route_recall`，再与 `scripts/evaluate_intent_sft_baseline.py` 的 Prompt 基线报告对照。

## 训练完成后

下载 `artifacts/intent_sft/customer-intent-lora` 回本机。该 Adapter 必须搭配训练时的基础模型加载；在独立测试集证明高风险路由不退化前，不得替换生产 Agent 的意图分析路径。
