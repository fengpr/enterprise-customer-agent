"""在线评测 Trace、任务和结果仓储，在线链路只写入，不执行模型评测。"""

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from repositories.database import DatabaseAdapter, DatabaseConfig


class EvaluationRepository:
    """使用 SQLite 实现可恢复的评测队列；生产环境可按同一接口迁移 PostgreSQL。"""

    def __init__(self, db_path: str | None = None, database: DatabaseAdapter | None = None) -> None:
        default = Path(__file__).resolve().parents[1] / "data" / "evaluations.db"
        self.db_path = Path(db_path or os.getenv("EVALUATION_DB_PATH", str(default)))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.database = database or DatabaseAdapter(DatabaseConfig.from_env(self.db_path))
        if not self.database.is_postgres:
            self._init_tables()

    def capture_online_trace(self, payload: dict[str, Any]) -> dict[str, Any]:
        """脱敏、采样并持久化在线 Trace；该方法不调用 DeepEval 或任何 Judge。"""
        trace_id = f"etr-{uuid.uuid4().hex[:16]}"
        safe = _sanitize_payload(payload)
        reason = _sampling_reason(safe)
        status = "PENDING" if reason else "SKIPPED_SAMPLE"
        if status == "PENDING" and self._daily_budget_used() >= int(os.getenv("EVAL_DAILY_BUDGET", "200")):
            status, reason = "SKIPPED_BUDGET", "daily_budget_exhausted"
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO evaluation_trace(trace_id, status, sampling_reason, payload, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (trace_id, status, reason or "stable_random_10pct", json.dumps(safe, ensure_ascii=False), now, now),
            )
        return {"trace_id": trace_id, "status": status, "sampling_reason": reason or "stable_random_10pct"}

    def claim_pending(self, limit: int = 10) -> list[dict[str, Any]]:
        """原子领取待评测 Trace，服务重启后可重新领取超时任务。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            expired = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
            conn.execute("UPDATE evaluation_trace SET status='PENDING', updated_at=? WHERE status='PROCESSING' AND updated_at < ?", (_now(), expired))
            rows = conn.execute("SELECT * FROM evaluation_trace WHERE status='PENDING' AND (next_retry_at IS NULL OR next_retry_at <= ?) ORDER BY created_at LIMIT ?", (_now(), limit)).fetchall()
            for row in rows:
                conn.execute("UPDATE evaluation_trace SET status='PROCESSING', attempts=attempts+1, updated_at=? WHERE trace_id=?", (_now(), row["trace_id"]))
        return [self._row_to_trace(row) for row in rows]

    def save_result(self, trace_id: str, result: dict[str, Any]) -> None:
        """保存 DeepEval 与硬规则结果，并把 Trace 标记为已完成。"""
        now = _now()
        status = "EVALUATED" if not result.get("worker_error") else "FAILED"
        with self._connect() as conn:
            if self.database.is_postgres:
                conn.execute("INSERT INTO evaluation_result(trace_id, result, created_at) VALUES (?, ?::jsonb, ?) ON CONFLICT(trace_id) DO UPDATE SET result=EXCLUDED.result, created_at=EXCLUDED.created_at", (trace_id, json.dumps(result, ensure_ascii=False), now))
            else:
                conn.execute("INSERT OR REPLACE INTO evaluation_result(trace_id, result, created_at) VALUES (?, ?, ?)", (trace_id, json.dumps(result, ensure_ascii=False), now))
            conn.execute("UPDATE evaluation_trace SET status=?, updated_at=? WHERE trace_id=?", (status, now, trace_id))

    def mark_failed(self, trace_id: str, error: str) -> None:
        """超过重试次数时保留错误，避免坏 Trace 阻塞整个队列。"""
        with self._connect() as conn:
            conn.execute("UPDATE evaluation_trace SET status='FAILED', last_error=?, updated_at=? WHERE trace_id=?", (error[:1000], _now(), trace_id))

    def release_for_retry(self, trace_id: str, error: str, attempts: int) -> None:
        """把暂时性 Judge 故障退回队列，下一轮 Worker 继续处理。"""
        delay_minutes = min(60, 2 ** max(0, attempts - 1))
        next_retry_at = (datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)).isoformat()
        with self._connect() as conn:
            conn.execute("UPDATE evaluation_trace SET status='PENDING', last_error=?, next_retry_at=?, updated_at=? WHERE trace_id=?", (error[:1000], next_retry_at, _now(), trace_id))

    def queue_status(self) -> dict[str, Any]:
        """返回队列积压、预算与状态计数，供内部监控页面读取。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) count FROM evaluation_trace GROUP BY status").fetchall()
        counts = {row["status"]: row["count"] for row in rows}
        return {"counts": counts, "daily_budget": int(os.getenv("EVAL_DAILY_BUDGET", "200")), "budget_used": self._daily_budget_used()}

    def create_job(self, job_type: str = "GOLDEN", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """创建持久化评测任务，供独立 Worker 在服务重启后继续领取。"""
        job_id, now = f"ejob-{uuid.uuid4().hex[:16]}", _now()
        with self._connect() as conn:
            conn.execute("INSERT INTO evaluation_job(job_id, job_type, status, payload, created_at, updated_at) VALUES (?, ?, 'PENDING', ?, ?, ?)", (job_id, job_type, "{}" if not payload else json.dumps(payload, ensure_ascii=False), now, now))
        return self.get_job(job_id) or {"job_id": job_id, "status": "PENDING"}

    def claim_job(self) -> dict[str, Any] | None:
        """原子领取一条后台评测任务，防止多个 Worker 重复执行同一黄金集。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM evaluation_job WHERE status='PENDING' ORDER BY created_at LIMIT 1").fetchone()
            if not row: return None
            conn.execute("UPDATE evaluation_job SET status='PROCESSING', started_at=?, updated_at=? WHERE job_id=?", (_now(), _now(), row["job_id"]))
        data = dict(row)
        data["payload"] = _load(data.get("payload")) if data.get("payload") else {}
        return data

    def complete_job(self, job_id: str, report: dict[str, Any] | None = None, error: str | None = None) -> None:
        """保存黄金集任务结果或错误，供坐席页面查询与复盘。"""
        status = "SUCCEEDED" if error is None else "FAILED"
        with self._connect() as conn:
            conn.execute("UPDATE evaluation_job SET status=?, report=?, error=?, finished_at=?, updated_at=? WHERE job_id=?", (status, json.dumps(report, ensure_ascii=False) if report else None, error, _now(), _now(), job_id))

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """读取持久化任务状态与报告。"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM evaluation_job WHERE job_id=?", (job_id,)).fetchone()
        if not row: return None
        data = dict(row)
        data["report"] = _load(data.get("report")) if data.get("report") else None
        data["payload"] = _load(data.get("payload")) if data.get("payload") else {}
        return data

    def online_report(self, days: int = 7, limit: int = 50) -> dict[str, Any]:
        """聚合线上已评测结果，返回趋势所需的平均分与失败 Trace。"""
        since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT t.trace_id, t.sampling_reason, t.created_at, t.status, t.payload, r.result
                   FROM evaluation_trace t LEFT JOIN evaluation_result r ON r.trace_id=t.trace_id
                   WHERE t.created_at >= ? ORDER BY t.created_at DESC LIMIT ?""", (since, limit)
            ).fetchall()
        items = []
        for row in rows:
            payload, result = _load(row["payload"]), _load(row["result"])
            items.append({"trace_id": row["trace_id"], "status": row["status"], "sampling_reason": row["sampling_reason"], "created_at": row["created_at"], "trace": payload, "result": result})
        evaluated = [item for item in items if item["result"]]
        metrics = _average_metrics(evaluated)
        failures = [item for item in evaluated if item["result"].get("failures")]
        return {"queue": self.queue_status(), "metrics": metrics, "items": items, "failures": failures}

    def _daily_budget_used(self) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM evaluation_trace WHERE created_at >= ? AND status IN ('PENDING','PROCESSING','EVALUATED')", (start,)).fetchone()[0])

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS evaluation_trace (
                trace_id TEXT PRIMARY KEY, status TEXT NOT NULL, sampling_reason TEXT NOT NULL, payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, next_retry_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            self._ensure_column(conn, "evaluation_trace", "next_retry_at", "TEXT")
            conn.execute("""CREATE TABLE IF NOT EXISTS evaluation_result (
                trace_id TEXT PRIMARY KEY, result TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(trace_id) REFERENCES evaluation_trace(trace_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS evaluation_job (
                job_id TEXT PRIMARY KEY, job_type TEXT NOT NULL, status TEXT NOT NULL, payload TEXT, report TEXT, error TEXT,
                created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL)""")
            self._ensure_column(conn, "evaluation_job", "payload", "TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evaluation_trace_status ON evaluation_trace(status, created_at)")

    def _connect(self):
        """每次操作关闭 SQLite 连接，避免 Windows 测试和 Worker 长运行锁定数据库文件。"""
        return self.database.connection()

    @staticmethod
    def _row_to_trace(row) -> dict[str, Any]:
        return {"trace_id": row["trace_id"], "attempts": row["attempts"] + 1, "payload": _load(row["payload"])}

    @staticmethod
    def _ensure_column(conn, table: str, column: str, column_type: str) -> None:
        """兼容已创建的本地评测库，避免新增重试字段后启动失败。"""
        columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not any(row["name"] == column for row in columns):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """删除客户身份与敏感业务字段，只保留 DeepEval 所需的最小 Trace。"""
    citations = [{"citation_id": item.get("citation_id"), "paragraph": _sanitize_text(str(item.get("paragraph") or ""))[:500]} for item in payload.get("citations", [])]
    tools = [{"name": item.get("query_type") or item.get("tool_name"), "status": item.get("status"), "has_data": bool(item.get("data"))} for item in payload.get("tool_results", [])]
    return {
        "trace_version": "v1", "model_version": os.getenv("LLM_MODEL", "unknown"), "prompt_version": "customer_service_reply_v1",
        "customer_hash": _hash(str(payload.get("customer_id") or "")), "input": _sanitize_text(str(payload.get("message") or "")),
        "output": _sanitize_text(str(payload.get("answer") or "")), "citations": citations, "tools": tools,
        "analysis": {key: (payload.get("analysis") or {}).get(key) for key in ("intent", "user_goal", "confidence", "need_human", "risk_reasons")},
        "citation_validation": payload.get("citation_validation") or {}, "decision_type": payload.get("decision_type"),
    }


def _sampling_reason(trace: dict[str, Any]) -> str | None:
    """风险和异常 Trace 强制采样，普通会话使用稳定 10% 抽样。"""
    analysis, validation = trace.get("analysis") or {}, trace.get("citation_validation") or {}
    risks = set(analysis.get("risk_reasons") or [])
    if analysis.get("need_human") or trace.get("decision_type") in {"review_required", "human_takeover"}: return "human_or_high_risk"
    if any(item.get("status") == "failed" for item in trace.get("tools") or []): return "tool_failed"
    if float(analysis.get("confidence") or 1) < 0.7 or "no_kb_hit" in risks: return "low_confidence_or_no_kb"
    if validation.get("hallucination_detected") or "model_analyze_failed" in risks: return "citation_or_model_anomaly"
    bucket = int(hashlib.sha256((trace["customer_hash"] + trace["input"]).encode()).hexdigest()[:8], 16) % 100
    return "stable_random_10pct" if bucket < 10 else None


def _sanitize_text(text: str) -> str:
    text = re.sub(r"(?<!\d)1\d{10}(?!\d)", "[PHONE]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+", "[EMAIL]", text)
    text = re.sub(r"\b(?:EC)?\d{10,18}\b", "[ORDER]", text, flags=re.I)
    return text[:2000]


def _hash(value: str) -> str:
    return hashlib.sha256((os.getenv("EVALUATION_HASH_SALT", "local-eval-salt") + value).encode()).hexdigest()[:16]


def _load(value: str | None) -> dict[str, Any]:
    try: return json.loads(value or "{}")
    except json.JSONDecodeError: return {}


def _average_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    keys = ("faithfulness", "answer_relevancy", "contextual_relevancy", "customer_safety")
    result: dict[str, Any] = {"evaluated_count": len(items), "failure_count": sum(1 for item in items if item["result"].get("failures"))}
    for key in keys:
        values = [float(item["result"].get("metrics", {}).get(key)) for item in items if item["result"].get("metrics", {}).get(key) is not None]
        result[key] = round(sum(values) / len(values), 4) if values else None
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
