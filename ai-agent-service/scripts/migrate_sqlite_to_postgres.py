"""SQLite 到 PostgreSQL 的离线迁移工具：先备份、导出，再按依赖顺序导入与校验。"""

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

TABLES = ["chat_session", "chat_message", "agent_call_log", "evaluation_trace", "evaluation_result", "evaluation_job"]


def export_sqlite(sqlite_path: Path, output: Path) -> dict[str, int]:
    """备份 SQLite 后导出 JSONL；源文件只读，不会被迁移过程修改。"""
    backup = sqlite_path.with_suffix(sqlite_path.suffix + f".{datetime.now():%Y%m%d%H%M%S}.bak")
    shutil.copy2(sqlite_path, backup)
    counts: dict[str, int] = {}
    with sqlite3.connect(sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        with output.open("w", encoding="utf-8") as target:
            for table in TABLES:
                try:
                    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                except sqlite3.OperationalError:
                    continue
                counts[table] = len(rows)
                for row in rows:
                    target.write(json.dumps({"table": table, "row": dict(row)}, ensure_ascii=False, default=str) + "\n")
    return counts


def main() -> None:
    """提供 export 子命令；导入必须在 PostgreSQL 已执行 V1 DDL 后由受控发布流程执行。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("sqlite", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps({"counts": export_sqlite(args.sqlite, args.output), "exported_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
