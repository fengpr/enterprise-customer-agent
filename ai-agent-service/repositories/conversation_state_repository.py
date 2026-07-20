"""会话状态、定时复核任务与客户站内通知的持久化仓储。"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repositories.database import DatabaseAdapter, DatabaseConfig


class ConversationStateRepository:
    """维护每个 session 的结构化状态，并通过版本号避免并发覆盖。"""

    def __init__(self, db_path: str | None = None, database: DatabaseAdapter | None = None) -> None:
        default_path = Path(__file__).resolve().parents[1] / "data" / "customer_agent.db"
        self.db_path = Path(db_path or os.getenv("CHAT_DB_PATH", str(default_path)))
        self.database = database or DatabaseAdapter(DatabaseConfig.from_env(self.db_path))
        if not self.database.is_postgres:
            self._init_tables()

    def get(self, session_no: str) -> dict[str, Any] | None:
        """读取会话状态快照；损坏 JSON 按空状态处理，避免阻断主链路。"""
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT state_json, version, updated_at FROM conversation_state WHERE session_no = ?",
                (session_no,),
            ).fetchone()
        if not row:
            return None
        raw = row["state_json"]
        if isinstance(raw, str):
            try:
                state = json.loads(raw)
            except (TypeError, ValueError):
                state = {}
        else:
            state = dict(raw or {})
        state["version"] = int(row["version"] or 0)
        state["updated_at"] = str(row["updated_at"] or "")
        return state

    def save(self, session_no: str, state: dict[str, Any], expected_version: int | None = None) -> dict[str, Any]:
        """以乐观锁保存状态；版本冲突由调用方重新加载并归并。"""
        now = datetime.now(UTC).isoformat()
        payload = dict(state)
        payload.pop("version", None)
        payload.pop("updated_at", None)
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        with self.database.connection() as conn:
            current = conn.execute(
                "SELECT version FROM conversation_state WHERE session_no = ?",
                (session_no,),
            ).fetchone()
            if not current:
                json_value = "CAST(? AS JSONB)" if self.database.is_postgres else "?"
                conn.execute(
                    f"INSERT INTO conversation_state(session_no, state_json, version, updated_at) VALUES (?, {json_value}, ?, ?)",
                    (session_no, encoded, 1, now),
                )
                version = 1
            else:
                current_version = int(current["version"] or 0)
                if expected_version is not None and current_version != expected_version:
                    raise ConversationStateConflict(session_no)
                version = current_version + 1
                json_value = "CAST(? AS JSONB)" if self.database.is_postgres else "?"
                cursor = conn.execute(
                    f"UPDATE conversation_state SET state_json = {json_value}, version = ?, updated_at = ? WHERE session_no = ? AND version = ?",
                    (encoded, version, now, session_no, current_version),
                )
                if cursor.rowcount != 1:
                    raise ConversationStateConflict(session_no)
        return {**payload, "version": version, "updated_at": now}

    def _init_tables(self) -> None:
        """为 SQLite 本地开发环境创建结构化状态表。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.database.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_state (
                    session_no TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_no) REFERENCES chat_session(session_no)
                )
                """
            )


class FollowupNotificationRepository:
    """持久化定时复核任务及其客户可见通知。"""

    def __init__(self, db_path: str | None = None, database: DatabaseAdapter | None = None) -> None:
        default_path = Path(__file__).resolve().parents[1] / "data" / "customer_agent.db"
        self.db_path = Path(db_path or os.getenv("CHAT_DB_PATH", str(default_path)))
        self.database = database or DatabaseAdapter(DatabaseConfig.from_env(self.db_path))
        if not self.database.is_postgres:
            self._init_tables()

    def create_followup(
        self,
        *,
        session_no: str,
        customer_id: int,
        order_no: str,
        scheduled_at: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """幂等创建物流复核任务，同一条件诉求不会重复调度。"""
        existing = self.get_by_idempotency_key(idempotency_key)
        if existing:
            return existing
        now = datetime.now(UTC).isoformat()
        followup_id = f"F{uuid.uuid4().hex}"
        with self.database.connection() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_followup(
                    followup_id, session_no, customer_id, task_type, order_no,
                    scheduled_at, status, attempts, max_attempts, idempotency_key,
                    result_summary, error_code, created_at, updated_at
                ) VALUES (?, ?, ?, 'DELIVERY_RECHECK', ?, ?, 'PENDING', 0, 3, ?, NULL, NULL, ?, ?)
                """,
                (followup_id, session_no, customer_id, order_no, scheduled_at, idempotency_key, now, now),
            )
        return self.get_followup(followup_id) or {}

    def get_followup(self, followup_id: str) -> dict[str, Any] | None:
        with self.database.connection() as conn:
            row = conn.execute("SELECT * FROM scheduled_followup WHERE followup_id = ?", (followup_id,)).fetchone()
        return self._row(row) if row else None

    def get_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        with self.database.connection() as conn:
            row = conn.execute("SELECT * FROM scheduled_followup WHERE idempotency_key = ?", (key,)).fetchone()
        return self._row(row) if row else None

    def list_due(self, now_iso: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_followup WHERE status = 'PENDING' AND scheduled_at <= ? ORDER BY scheduled_at LIMIT ?",
                (now_iso, limit),
            ).fetchall()
        return [self._row(row) for row in rows]

    def update_status(
        self,
        followup_id: str,
        status: str,
        *,
        attempts: int | None = None,
        result_summary: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        """更新调度状态，结果仅保留客户安全摘要。"""
        now = datetime.now(UTC).isoformat()
        fields = ["status = ?", "updated_at = ?", "error_code = ?"]
        values: list[Any] = [status, now, error_code]
        if attempts is not None:
            fields.append("attempts = ?")
            values.append(attempts)
        if result_summary is not None:
            fields.append("result_summary = CAST(? AS JSONB)" if self.database.is_postgres else "result_summary = ?")
            values.append(json.dumps(result_summary, ensure_ascii=False, default=str))
        values.append(followup_id)
        with self.database.connection() as conn:
            conn.execute(f"UPDATE scheduled_followup SET {', '.join(fields)} WHERE followup_id = ?", tuple(values))

    def list_followups(self, customer_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self.database.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_followup WHERE customer_id = ? ORDER BY created_at DESC LIMIT ?",
                (customer_id, limit),
            ).fetchall()
        return [self._row(row) for row in rows]

    def cancel_followup(self, followup_id: str, customer_id: int) -> bool:
        """仅允许所属客户取消尚未执行的复核任务。"""
        now = datetime.now(UTC).isoformat()
        with self.database.connection() as conn:
            cursor = conn.execute(
                "UPDATE scheduled_followup SET status = 'CANCELLED', updated_at = ? WHERE followup_id = ? AND customer_id = ? AND status IN ('PENDING', 'QUEUED')",
                (now, followup_id, customer_id),
            )
        return cursor.rowcount == 1

    def create_notification(
        self,
        *,
        customer_id: int,
        session_no: str,
        followup_id: str | None,
        title: str,
        content: str,
        notification_type: str = "DELIVERY_RECHECK",
    ) -> dict[str, Any]:
        """创建站内通知；followup_id 保证同一任务只产生一条通知。"""
        if followup_id:
            with self.database.connection() as conn:
                existing = conn.execute(
                    "SELECT * FROM customer_notification WHERE followup_id = ?",
                    (followup_id,),
                ).fetchone()
            if existing:
                return self._notification_row(existing)
        notification_id = f"N{uuid.uuid4().hex}"
        now = datetime.now(UTC).isoformat()
        with self.database.connection() as conn:
            conn.execute(
                """
                INSERT INTO customer_notification(
                    notification_id, customer_id, session_no, followup_id,
                    notification_type, title, content, is_read, created_at, read_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (notification_id, customer_id, session_no, followup_id, notification_type, title[:128], content, False, now),
            )
        return self.get_notification(notification_id) or {}

    def get_notification(self, notification_id: str) -> dict[str, Any] | None:
        with self.database.connection() as conn:
            row = conn.execute("SELECT * FROM customer_notification WHERE notification_id = ?", (notification_id,)).fetchone()
        return self._notification_row(row) if row else None

    def list_notifications(self, customer_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self.database.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM customer_notification WHERE customer_id = ? ORDER BY created_at DESC LIMIT ?",
                (customer_id, limit),
            ).fetchall()
        return [self._notification_row(row) for row in rows]

    def unread_count(self, customer_id: int) -> int:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM customer_notification WHERE customer_id = ? AND is_read = ?",
                (customer_id, False),
            ).fetchone()
        return int(row["total"] if row else 0)

    def mark_read(self, notification_id: str, customer_id: int) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.database.connection() as conn:
            cursor = conn.execute(
                "UPDATE customer_notification SET is_read = ?, read_at = ? WHERE notification_id = ? AND customer_id = ?",
                (True, now, notification_id, customer_id),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _decode_json(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        try:
            return json.loads(str(value))
        except (TypeError, ValueError):
            return None

    def _row(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["result_summary"] = self._decode_json(data.get("result_summary"))
        return data

    @staticmethod
    def _notification_row(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["is_read"] = bool(data.get("is_read"))
        return data

    def _init_tables(self) -> None:
        """创建 SQLite 开发环境所需表和索引。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.database.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_followup (
                    followup_id TEXT PRIMARY KEY, session_no TEXT NOT NULL, customer_id INTEGER NOT NULL,
                    task_type TEXT NOT NULL, order_no TEXT NOT NULL, scheduled_at TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
                    idempotency_key TEXT NOT NULL UNIQUE, result_summary TEXT, error_code TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_no) REFERENCES chat_session(session_no)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_followup_due ON scheduled_followup(status, scheduled_at)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS customer_notification (
                    notification_id TEXT PRIMARY KEY, customer_id INTEGER NOT NULL, session_no TEXT,
                    followup_id TEXT, notification_type TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
                    is_read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, read_at TEXT,
                    FOREIGN KEY(session_no) REFERENCES chat_session(session_no),
                    FOREIGN KEY(followup_id) REFERENCES scheduled_followup(followup_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_customer_notification_unread ON customer_notification(customer_id, is_read, created_at)")


class ConversationStateConflict(RuntimeError):
    """会话状态版本已变化，调用方需要重新加载后再归并。"""
