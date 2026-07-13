import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from repositories.database import DatabaseAdapter, DatabaseConfig


class AgentCallLogRepository:
    """Agent 调用日志仓储，负责把模型调用和工具调用审计记录持久化到 SQLite。"""

    def __init__(self, db_path: str | None = None, database: DatabaseAdapter | None = None) -> None:
        """初始化日志数据库路径，并确保 agent_call_log 表存在。"""
        default_path = Path(__file__).resolve().parents[1] / "data" / "agent_logs.db"
        self.db_path = Path(db_path or os.getenv("AGENT_LOG_DB_PATH", str(default_path)))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.database = database or DatabaseAdapter(DatabaseConfig.from_env(self.db_path))
        if not self.database.is_postgres:
            self._init_table()

    def save(self, tool_name: str, input_data: dict[str, Any], output_data: dict[str, Any]) -> dict[str, Any]:
        """保存一条 Agent 调用日志，返回带主键的日志记录供内存兼容使用。"""
        created_at = datetime.utcnow().isoformat()
        status = str(output_data.get("status") or "success")
        error_message = output_data.get("error") or output_data.get("error_message")
        input_json = self._to_json(input_data)
        output_json = self._to_json(output_data)

        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO agent_call_log (
                    tool_name, input_data, output_data, status, error_message, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (tool_name, input_json, output_json, status, error_message, created_at),
            )
            log_id = cursor.lastrowid

        return {
            "id": log_id,
            "tool_name": tool_name,
            "input_data": input_data,
            "output_data": output_data,
            "status": status,
            "error_message": error_message,
            "created_at": created_at,
        }

    def list_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """按时间倒序查询最近的 Agent 调用日志，用于排查模型和工具异常。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, tool_name, input_data, output_data, status, error_message, created_at
                FROM agent_call_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "id": row["id"],
                "tool_name": row["tool_name"],
                "input_data": self._from_json(row["input_data"]),
                "output_data": self._from_json(row["output_data"]),
                "status": row["status"],
                "error_message": row["error_message"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _init_table(self) -> None:
        """创建 agent_call_log 表和时间索引，保证服务首次启动即可写日志。"""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_call_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool_name TEXT NOT NULL,
                    input_data TEXT NOT NULL,
                    output_data TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_call_log_created_at
                ON agent_call_log(created_at)
                """
            )

    def _connect(self):
        """每次操作使用独立连接，避免 FastAPI 多请求下共享连接导致线程问题。"""
        return self.database.connection()

    def _to_json(self, value: dict[str, Any]) -> str:
        """把调用入参和出参序列化为 JSON 字符串，便于后续落 MySQL 时平滑迁移。"""
        return json.dumps(value, ensure_ascii=False, default=str)

    def _from_json(self, value: str) -> dict[str, Any]:
        """读取日志时还原 JSON 字段，解析失败时保留原文避免日志丢失。"""
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {"value": data}
        except json.JSONDecodeError:
            return {"raw": value}
