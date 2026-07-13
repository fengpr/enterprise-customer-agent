"""将 SQLite JSONL 导出导入已初始化的 PostgreSQL；失败时由调用方事务回滚。"""

import argparse
import json
from pathlib import Path

JSON_COLUMNS = {
    "chat_message": {"extra_data"}, "agent_call_log": {"input_data", "output_data"},
    "evaluation_trace": {"payload"}, "evaluation_result": {"result"}, "evaluation_job": {"payload", "report"},
}


def import_export(export: Path, dsn: str) -> dict[str, int]:
    """按 JSONL 顺序导入；调用异常会抛出，psycopg 事务不会提交部分数据。"""
    import psycopg
    from psycopg.types.json import Jsonb

    counts: dict[str, int] = {}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cursor:
            for line in export.read_text(encoding="utf-8").splitlines():
                item = json.loads(line); table, row = item["table"], item["row"]
                for column in JSON_COLUMNS.get(table, set()):
                    if isinstance(row.get(column), str):
                        row[column] = json.loads(row[column])
                    if row.get(column) is not None:
                        row[column] = Jsonb(row[column])
                columns = list(row)
                placeholders = ", ".join(["%s"] * len(columns))
                cursor.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING", [row[column] for column in columns])
                counts[table] = counts.get(table, 0) + 1
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("export", type=Path); parser.add_argument("--dsn", required=True)
    print(json.dumps(import_export(parser.parse_args().export, parser.parse_args().dsn), ensure_ascii=False))
