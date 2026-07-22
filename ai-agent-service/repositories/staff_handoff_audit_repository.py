"""人工会话历史访问审计，记录最小必要的查看行为而不保存聊天正文。"""

import json
from datetime import datetime, timezone
from typing import Any

from repositories.chat_repository import ChatSessionRepository


class StaffHandoffAuditRepository:
    """保存座席主动展开人工会话历史的审计事件。"""

    def __init__(self, session_repository: ChatSessionRepository) -> None:
        self.session_repository = session_repository
        self.database = session_repository.database
        if not self.database.is_postgres:
            self._init_table()

    def record_history_access(
        self,
        *,
        session_no: str,
        staff_id: str,
        before_message_id: int,
        returned_count: int,
    ) -> None:
        """记录主动历史查看，不写入客户正文、Token 或工具原始结果。"""
        metadata = json.dumps(
            {"before_message_id": before_message_id, "returned_count": returned_count},
            ensure_ascii=False,
        )
        with self.database.connection() as conn:
            conn.execute(
                """
                INSERT INTO staff_handoff_audit_log (
                    session_no, staff_id, action, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_no,
                    staff_id,
                    "expand_history",
                    metadata,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def _init_table(self) -> None:
        """为本地 SQLite 创建审计表；PostgreSQL 由 Flyway SQL 接管。"""
        with self.database.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS staff_handoff_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_no TEXT NOT NULL,
                    staff_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_staff_handoff_audit_session_created
                ON staff_handoff_audit_log(session_no, created_at DESC)
                """
            )
