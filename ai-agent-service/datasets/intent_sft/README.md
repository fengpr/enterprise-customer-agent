# 客服意图 SFT 数据集

本目录保存企业客服 Agent 意图识别的脱敏监督微调样本。首期目标是让模型更稳定地输出与 `schemas/intent_schema.py` 一致的 `LLMIntentDraft` JSON，不替代 RAG、Java 业务 Tool、人工审核或权限校验。

## 文件说明

- `train.jsonl`：训练样本，用于更新 LoRA Adapter 参数。
- `validation.jsonl`：验证样本，用于选择训练轮次和超参数。
- `test.jsonl`：独立测试样本，只用于比较 Prompt 基线与微调模型。
- `label_guideline.md`：标注口径和安全边界。

## 数据安全

样本必须使用虚构订单号（如 `EC_TEST_0001`），不得包含真实客户姓名、手机号、地址、邮箱、登录凭证、完整订单号、Prompt 或工具原始返回。原始会话数据不得放入本目录。

## 校验命令

在 `ai-agent-service` 目录运行：

```powershell
python scripts/validate_intent_sft_dataset.py
```

校验脚本会检查 JSONL 格式、消息角色顺序、助手输出 JSON，以及输出是否能通过当前 `LLMIntentDraft` Schema 校验。
