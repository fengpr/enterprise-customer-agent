"""验证 PostgreSQL 迁移资产的关键表、类型和工单幂等约束。"""

from pathlib import Path

from scripts.validate_migration import summarize


ROOT = Path(__file__).resolve().parents[1]


def test_python_postgres_schema_contains_required_tables_and_jsonb():
    """Python 侧迁移必须覆盖会话、日志与评测表，并把 JSON 文本映射为 JSONB。"""
    sql = (ROOT / "migrations" / "postgres" / "V1__agent_schema.sql").read_text(encoding="utf-8").lower()
    for table in ("chat_session", "chat_message", "agent_call_log", "evaluation_trace", "evaluation_job", "evaluation_result"):
        assert f"create table if not exists {table}" in sql
    assert "jsonb" in sql and "bigserial" in sql


def test_business_schema_has_ticket_idempotency_constraints():
    """人工工单迁移必须具备 request_id 与客户幂等键唯一索引。"""
    sql = (ROOT.parent / "business-service" / "src" / "main" / "resources" / "db" / "migration" / "V1__business_postgres_schema.sql").read_text(encoding="utf-8").lower()
    assert "uq_support_ticket_request_id" in sql
    assert "uq_support_ticket_customer_idempotency" in sql
    assert "bigserial primary key" in sql


def test_export_summary_counts_and_checksum(tmp_path):
    """迁移校验摘要应稳定统计表行数，不读取或输出客户字段内容。"""
    export = tmp_path / "export.jsonl"
    export.write_text('{"table":"chat_session","row":{"id":1}}\n{"table":"chat_message","row":{"id":2}}\n', encoding="utf-8")
    report = summarize(export)
    assert report["counts"] == {"chat_session": 1, "chat_message": 1}
    assert len(report["checksum"]) == 64
