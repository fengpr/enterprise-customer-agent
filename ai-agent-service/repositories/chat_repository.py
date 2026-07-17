import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from repositories.database import DatabaseAdapter, DatabaseConfig


class ChatSessionRepository:
    """会话仓储，负责客服会话的创建、状态更新和按客户隔离查询。"""

    def __init__(self, db_path: str | None = None, database: DatabaseAdapter | None = None) -> None:
        """初始化会话数据库，并确保会话表和消息表存在。"""
        default_path = Path(__file__).resolve().parents[1] / "data" / "customer_agent.db"
        self.db_path = Path(db_path or os.getenv("CHAT_DB_PATH", str(default_path)))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.database = database or DatabaseAdapter(DatabaseConfig.from_env(self.db_path))
        if not self.database.is_postgres:
            self._init_tables()

    def create(self, customer_id: int | None, title: str) -> dict[str, Any]:
        """创建新客服会话，customer_id 来自登录态，用于后续数据隔离。"""
        now = datetime.utcnow().isoformat()
        session_no = f"S{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_session (
                    session_no, customer_id, status, title, intent, emotion,
                    priority, ai_summary, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_no, customer_id, "AI_ONLY", title[:128], None, None, None, None, now, now),
            )
            session_id = cursor.lastrowid
        return self.get_by_id(session_id)

    def get_by_session_no(self, session_no: str) -> dict[str, Any] | None:
        """按会话编号查询会话，内部流程使用；对外接口应使用带客户校验的方法。"""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, session_no, customer_id, status, title, intent, emotion,
                       priority, ai_summary, human_requested_at, human_assigned_staff_id,
                       human_assigned_staff_name, human_accepted_at, human_closed_at,
                       handoff_reason, created_at, updated_at, deleted_at, pinned_at
                FROM chat_session
                WHERE session_no = ?
                """,
                (session_no,),
            ).fetchone()
        return self._row_to_session(row) if row else None

    def get_by_session_no_for_customer(self, session_no: str, customer_id: int) -> dict[str, Any] | None:
        """按会话编号和客户 ID 查询会话，防止用户越权读取他人会话。"""
        session = self.get_by_session_no(session_no)
        if not session or session["customer_id"] != customer_id or session.get("deleted_at"):
            return None
        return session

    def get_by_id(self, session_id: int) -> dict[str, Any]:
        """按数据库主键查询会话，供创建后回读完整字段。"""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, session_no, customer_id, status, title, intent, emotion,
                       priority, ai_summary, human_requested_at, human_assigned_staff_id,
                       human_assigned_staff_name, human_accepted_at, human_closed_at,
                       handoff_reason, created_at, updated_at, deleted_at, pinned_at
                FROM chat_session
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        if not row:
            raise ValueError(f"会话不存在: {session_id}")
        return self._row_to_session(row)

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """查询最近会话列表，保留给内部排查；前端应使用按客户过滤的方法。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_no, customer_id, status, title, intent, emotion,
                       priority, ai_summary, human_requested_at, human_assigned_staff_id,
                       human_assigned_staff_name, human_accepted_at, human_closed_at,
                       handoff_reason, created_at, updated_at, deleted_at, pinned_at
                FROM chat_session
                WHERE deleted_at IS NULL
                ORDER BY pinned_at DESC NULLS LAST, updated_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def list_recent_for_customer(self, customer_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """查询当前客户自己的会话列表，避免不同登录用户之间串数据。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_no, customer_id, status, title, intent, emotion,
                       priority, ai_summary, human_requested_at, human_assigned_staff_id,
                       human_assigned_staff_name, human_accepted_at, human_closed_at,
                       handoff_reason, created_at, updated_at, deleted_at, pinned_at
                FROM chat_session
                WHERE customer_id = ? AND deleted_at IS NULL
                ORDER BY pinned_at DESC NULLS LAST, updated_at DESC, id DESC
                LIMIT ?
                """,
                (customer_id, limit),
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def update_after_agent_reply(self, session_no: str, analysis: dict[str, Any], status: str) -> None:
        """Agent 回复后回写会话分析结果，方便列表按意图和状态筛选。"""
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            current = conn.execute(
                "SELECT status FROM chat_session WHERE session_no = ? AND deleted_at IS NULL",
                (session_no,),
            ).fetchone()
            current_status = current["status"] if current else None
            # 人工请求挂起或接入期间，AI 回复不能覆盖人工服务状态。
            next_status = current_status if current_status in {"HUMAN_PENDING", "HUMAN_ACTIVE"} and status == "AI_ONLY" else status
            conn.execute(
                """
                UPDATE chat_session
                SET status = ?, intent = ?, emotion = ?, priority = ?,
                    ai_summary = ?, title = CASE
                        WHEN title IS NULL OR title = '' OR title = '新会话' THEN ?
                        ELSE title
                    END,
                    updated_at = ?
                WHERE session_no = ? AND deleted_at IS NULL
                """,
                (
                    next_status,
                    analysis.get("intent"),
                    analysis.get("emotion"),
                    analysis.get("priority"),
                    analysis.get("summary"),
                    str(analysis.get("summary") or "新会话")[:128],
                    now,
                    session_no,
                ),
            )

    def update_status(self, session_no: str, status: str) -> None:
        """更新会话处理状态，用于坐席确认发送客户回复后刷新客户侧进度。"""
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE chat_session
                SET status = ?, updated_at = ?
                WHERE session_no = ? AND deleted_at IS NULL
                """,
                (status, now, session_no),
            )

    def request_handoff(self, session_no: str, reason: str) -> dict[str, Any] | None:
        """记录客户转人工请求，进入待接入队列但不创建业务工单。"""
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE chat_session
                SET status = 'HUMAN_PENDING',
                    human_requested_at = COALESCE(human_requested_at, ?),
                    human_closed_at = NULL,
                    handoff_reason = ?,
                    updated_at = ?
                WHERE session_no = ? AND deleted_at IS NULL
                """,
                (now, reason, now, session_no),
            )
        return self.get_by_session_no(session_no)

    def accept_handoff(self, session_no: str, staff_id: str, staff_name: str) -> dict[str, Any] | None:
        """坐席接入人工会话，后续客户消息优先进入人工处理。"""
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE chat_session
                SET status = 'HUMAN_ACTIVE',
                    human_assigned_staff_id = ?,
                    human_assigned_staff_name = ?,
                    human_accepted_at = ?,
                    human_closed_at = NULL,
                    updated_at = ?
                WHERE session_no = ?
                  AND status = 'HUMAN_PENDING'
                  AND deleted_at IS NULL
                """,
                (staff_id, staff_name, now, now, session_no),
            )
        return self.get_by_session_no(session_no) if cursor.rowcount else None

    def close_handoff(self, session_no: str, staff_id: str, status: str = "HUMAN_CLOSED") -> dict[str, Any] | None:
        """坐席结束人工接管，会话回到 AI 协助或关闭状态。"""
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE chat_session
                SET status = ?,
                    human_closed_at = ?,
                    updated_at = ?
                WHERE session_no = ?
                  AND human_assigned_staff_id = ?
                  AND status = 'HUMAN_ACTIVE'
                  AND deleted_at IS NULL
                """,
                (status, now, now, session_no, staff_id),
            )
        return self.get_by_session_no(session_no) if cursor.rowcount else None

    def list_handoff_sessions(self, staff_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """查询待接入和当前坐席已接入的人工会话，供坐席工作台展示。"""
        params: list[Any] = []
        condition = "status = 'HUMAN_PENDING'"
        if staff_id:
            condition = "(status = 'HUMAN_PENDING' OR (status = 'HUMAN_ACTIVE' AND human_assigned_staff_id = ?))"
            params.append(staff_id)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, session_no, customer_id, status, title, intent, emotion,
                       priority, ai_summary, human_requested_at, human_assigned_staff_id,
                       human_assigned_staff_name, human_accepted_at, human_closed_at,
                       handoff_reason, created_at, updated_at, deleted_at, pinned_at
                FROM chat_session
                WHERE {condition} AND deleted_at IS NULL
                ORDER BY
                    CASE status WHEN 'HUMAN_ACTIVE' THEN 0 WHEN 'HUMAN_PENDING' THEN 1 ELSE 2 END,
                    updated_at DESC,
                    id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def count_active_handoff_by_staff(self, staff_id: str) -> int:
        """统计坐席当前接入的人工会话数，用于避免继续分配给已满负载坐席。"""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM chat_session
                WHERE status = 'HUMAN_ACTIVE'
                  AND human_assigned_staff_id = ?
                  AND deleted_at IS NULL
                """,
                (staff_id,),
            ).fetchone()
        return int(row["total"] if row else 0)

    def soft_delete_for_customer(self, session_no: str, customer_id: int) -> bool:
        """按客户身份软删除会话，保留消息用于工单追踪和客服审计。"""
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE chat_session
                SET deleted_at = ?, updated_at = ?
                WHERE session_no = ? AND customer_id = ? AND deleted_at IS NULL
                """,
                (now, now, session_no, customer_id),
            )
        return cursor.rowcount > 0

    def set_pinned_for_customer(self, session_no: str, customer_id: int, pinned: bool) -> dict[str, Any] | None:
        """按客户归属更新会话置顶状态，置顶时间用于稳定排序且不会修改会话业务更新时间。"""
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE chat_session
                SET pinned_at = ?
                WHERE session_no = ? AND customer_id = ? AND deleted_at IS NULL
                """,
                (now if pinned else None, session_no, customer_id),
            )
        return self.get_by_session_no_for_customer(session_no, customer_id) if cursor.rowcount else None

    def _init_tables(self) -> None:
        """创建会话与消息表，确保服务首次启动即可持久化客服记录。"""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_session (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_no TEXT NOT NULL UNIQUE,
                    customer_id INTEGER,
                    status TEXT NOT NULL,
                    title TEXT,
                    intent TEXT,
                    emotion TEXT,
                    priority TEXT,
                    ai_summary TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    pinned_at TEXT
                )
                """
            )
            self._ensure_column(conn, "chat_session", "deleted_at", "TEXT")
            self._ensure_column(conn, "chat_session", "human_requested_at", "TEXT")
            self._ensure_column(conn, "chat_session", "human_assigned_staff_id", "TEXT")
            self._ensure_column(conn, "chat_session", "human_assigned_staff_name", "TEXT")
            self._ensure_column(conn, "chat_session", "human_accepted_at", "TEXT")
            self._ensure_column(conn, "chat_session", "human_closed_at", "TEXT")
            self._ensure_column(conn, "chat_session", "handoff_reason", "TEXT")
            self._ensure_column(conn, "chat_session", "pinned_at", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_message (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    sender_type TEXT NOT NULL,
                    sender_id TEXT,
                    content TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    extra_data TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES chat_session(id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_session_customer_updated
                ON chat_session(customer_id, updated_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_message_session_created
                ON chat_message(session_id, created_at)
                """
            )

    def _connect(self):
        """每次数据库操作使用独立连接，避免多请求共享连接带来的线程问题。"""
        return self.database.connection()

    def _ensure_column(self, conn, table_name: str, column_name: str, column_type: str) -> None:
        """为旧 SQLite 数据库补充新增字段，避免历史 Demo 数据启动失败。"""
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        if any(row["name"] == column_name for row in rows):
            return
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

    def _row_to_session(self, row) -> dict[str, Any]:
        """把 SQLite 行转换为接口返回字典。"""
        return {
            "id": row["id"],
            "session_id": row["session_no"],
            "customer_id": row["customer_id"],
            "status": row["status"],
            "title": row["title"],
            "intent": row["intent"],
            "emotion": row["emotion"],
            "priority": row["priority"],
            "ai_summary": row["ai_summary"],
            "human_requested_at": row["human_requested_at"],
            "human_assigned_staff_id": row["human_assigned_staff_id"],
            "human_assigned_staff_name": row["human_assigned_staff_name"],
            "human_accepted_at": row["human_accepted_at"],
            "human_closed_at": row["human_closed_at"],
            "handoff_reason": row["handoff_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
            "pinned_at": row["pinned_at"],
        }


class ChatMessageRepository:
    """消息仓储，负责保存用户消息、AI 回复以及按会话查询历史。"""

    def __init__(self, session_repository: ChatSessionRepository) -> None:
        """复用会话仓储的数据库路径，保证会话和消息写入同一个 SQLite 文件。"""
        self.session_repository = session_repository
        self.db_path = session_repository.db_path

    def save(
        self,
        session_no: str,
        sender_type: str,
        content: str,
        message_type: str = "text",
        sender_id: str | None = None,
        extra_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """保存会话消息，支持用户原文、AI 回复和后续系统消息。"""
        session = self.session_repository.get_by_session_no(session_no)
        if not session:
            raise ValueError(f"会话不存在: {session_no}")

        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_message (
                    session_id, sender_type, sender_id, content,
                    message_type, extra_data, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session["id"],
                    sender_type,
                    sender_id,
                    content,
                    message_type,
                    json.dumps(extra_data or {}, ensure_ascii=False, default=str),
                    now,
                ),
            )
            conn.execute(
                "UPDATE chat_session SET updated_at = ? WHERE id = ?",
                (now, session["id"]),
            )
            message_id = cursor.lastrowid
        return {
            "id": message_id,
            "session_id": session_no,
            "sender_type": sender_type,
            "sender_id": sender_id,
            "content": content,
            "message_type": message_type,
            "extra_data": extra_data or {},
            "created_at": now,
        }

    def list_by_session(self, session_no: str) -> list[dict[str, Any]]:
        """按会话编号查询消息历史，内部流程使用；对外接口应先校验会话归属。"""
        session = self.session_repository.get_by_session_no(session_no)
        if not session:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, sender_type, sender_id, content, message_type, extra_data, created_at
                FROM chat_message
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session["id"],),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "session_id": session_no,
                "sender_type": row["sender_type"],
                "sender_id": row["sender_id"],
                "content": row["content"],
                "message_type": row["message_type"],
                "extra_data": self._load_extra(row["extra_data"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_by_session_for_customer(self, session_no: str, customer_id: int) -> list[dict[str, Any]]:
        """按客户身份查询会话消息，防止用户用 session_id 读取他人消息。"""
        session = self.session_repository.get_by_session_no_for_customer(session_no, customer_id)
        if not session:
            return []
        return self.list_by_session(session_no)

    def find_ticket_context(self, ticket_no: str) -> dict[str, Any] | None:
        """兼容旧工单：根据 AI 消息扩展字段中的工单号定位客户会话。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    m.id AS message_id,
                    m.sender_type,
                    m.sender_id,
                    m.content,
                    m.message_type,
                    m.extra_data,
                    m.created_at AS message_created_at,
                    s.id AS session_db_id,
                    s.session_no,
                    s.customer_id,
                    s.status,
                    s.title,
                    s.intent,
                    s.emotion,
                    s.priority,
                    s.ai_summary,
                    s.created_at AS session_created_at,
                    s.updated_at AS session_updated_at
                FROM chat_message m
                JOIN chat_session s ON s.id = m.session_id
                WHERE m.extra_data IS NOT NULL
                ORDER BY m.id DESC
                """
            ).fetchall()

        for row in rows:
            extra_data = self._load_extra(row["extra_data"])
            ticket_result = extra_data.get("ticket_result") or {}
            ticket_data = ticket_result.get("data") or {}
            if ticket_data.get("ticketNo") != ticket_no:
                continue
            # 旧数据没有 externalSessionNo，仍可通过 AI 回复扩展字段定位会话。
            return {
                "session": {
                    "id": row["session_db_id"],
                    "session_id": row["session_no"],
                    "customer_id": row["customer_id"],
                    "status": row["status"],
                    "title": row["title"],
                    "intent": row["intent"],
                    "emotion": row["emotion"],
                    "priority": row["priority"],
                    "ai_summary": row["ai_summary"],
                    "created_at": row["session_created_at"],
                    "updated_at": row["session_updated_at"],
                },
                "message": {
                    "id": row["message_id"],
                    "session_id": row["session_no"],
                    "sender_type": row["sender_type"],
                    "sender_id": row["sender_id"],
                    "content": row["content"],
                    "message_type": row["message_type"],
                    "extra_data": extra_data,
                    "created_at": row["message_created_at"],
                },
                "ticket_result": ticket_result,
            }
        return None

    def _connect(self):
        """每次消息操作使用独立连接，保证 Streamlit 多次请求下稳定读写。"""
        return self.session_repository.database.connection()

    def _load_extra(self, value: str | None) -> dict[str, Any]:
        """还原消息扩展 JSON，解析失败时返回空对象避免接口异常。"""
        if not value:
            return {}
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {"value": data}
        except json.JSONDecodeError:
            return {}
