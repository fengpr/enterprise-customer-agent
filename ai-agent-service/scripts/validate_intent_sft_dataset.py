"""校验客服意图 SFT 数据集是否符合当前 Schema 与消息格式。"""

import json
import sys
from pathlib import Path

from pydantic import ValidationError

# 允许从 scripts 目录直接执行时仍能导入 Agent 服务包。
SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from schemas.intent_schema import LLMIntentDraft


DATASET_DIR = SERVICE_ROOT / "datasets" / "intent_sft"
DATASET_FILES = ("train.jsonl", "validation.jsonl", "test.jsonl")
EXPECTED_ROLES = ("system", "user", "assistant")


def validate_dataset_file(path: Path) -> list[str]:
    """逐行校验 JSONL、消息角色和助手结构化输出，返回全部错误信息。"""
    errors: list[str] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            messages = record["messages"]
            roles = tuple(message["role"] for message in messages)
            # 训练样本统一为三段消息，避免不同格式混入首期小规模数据集。
            if roles != EXPECTED_ROLES:
                raise ValueError(f"消息角色必须为 {EXPECTED_ROLES}，实际为 {roles}")
            assistant_content = messages[-1]["content"]
            payload = json.loads(assistant_content)
            LLMIntentDraft.model_validate(payload)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            errors.append(f"{path.name}:{line_number} - {exc}")
    return errors


def count_samples(path: Path) -> int:
    """统计非空 JSONL 行数，避免校验提示中的样本数量写死。"""
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def main() -> int:
    """校验全部数据集分片，并以进程退出码表达校验结果。"""
    errors: list[str] = []
    for filename in DATASET_FILES:
        path = DATASET_DIR / filename
        if not path.is_file():
            errors.append(f"缺少数据集文件：{path}")
            continue
        errors.extend(validate_dataset_file(path))

    if errors:
        print("数据集校验失败：")
        print("\n".join(errors))
        return 1
    counts = {path.stem: count_samples(path) for path in (DATASET_DIR / filename for filename in DATASET_FILES)}
    print("数据集校验通过：" + "，".join(f"{name}={count}" for name, count in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
