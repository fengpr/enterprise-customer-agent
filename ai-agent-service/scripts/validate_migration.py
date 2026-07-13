"""校验 SQLite 导出文件：行数、关键字段和确定性摘要，用于导入前后比对。"""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def summarize(path: Path) -> dict:
    """计算表行数与按主业务标识排序的 SHA256 摘要，避免暴露业务内容。"""
    counts, digests = Counter(), []
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line); table, row = item["table"], item["row"]
        counts[table] += 1
        key = row.get("id") or row.get("session_no") or row.get("trace_id") or row.get("job_id")
        digests.append(f"{table}:{key}")
    checksum = hashlib.sha256("\n".join(sorted(digests)).encode()).hexdigest()
    return {"counts": dict(counts), "checksum": checksum}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("export", type=Path)
    print(json.dumps(summarize(parser.parse_args().export), ensure_ascii=False))
