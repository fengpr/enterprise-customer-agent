-- Python Agent PostgreSQL 基线。SQLite 仍为默认开发数据库；生产切换前由本脚本初始化。

CREATE TABLE IF NOT EXISTS chat_session (
    id BIGSERIAL PRIMARY KEY, session_no VARCHAR(64) NOT NULL UNIQUE, customer_id BIGINT,
    status VARCHAR(32) NOT NULL, title VARCHAR(128), intent VARCHAR(64), emotion VARCHAR(32), priority VARCHAR(32), ai_summary TEXT,
    human_requested_at TIMESTAMPTZ, human_assigned_staff_id VARCHAR(64), human_assigned_staff_name VARCHAR(128),
    human_accepted_at TIMESTAMPTZ, human_closed_at TIMESTAMPTZ, handoff_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL, deleted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_chat_session_customer_updated ON chat_session(customer_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_session_handoff ON chat_session(status, updated_at DESC) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS chat_message (
    id BIGSERIAL PRIMARY KEY, session_id BIGINT NOT NULL REFERENCES chat_session(id),
    sender_type VARCHAR(32) NOT NULL, sender_id VARCHAR(64), content TEXT NOT NULL, message_type VARCHAR(32) NOT NULL, extra_data JSONB,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_message_session_created ON chat_message(session_id, created_at, id);

CREATE TABLE IF NOT EXISTS agent_call_log (
    id BIGSERIAL PRIMARY KEY, tool_name VARCHAR(128) NOT NULL, input_data JSONB NOT NULL, output_data JSONB NOT NULL,
    status VARCHAR(32) NOT NULL, error_message TEXT, created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_call_log_created_at ON agent_call_log(created_at DESC);

CREATE TABLE IF NOT EXISTS evaluation_trace (
    trace_id VARCHAR(64) PRIMARY KEY, status VARCHAR(32) NOT NULL, sampling_reason VARCHAR(128) NOT NULL,
    payload JSONB NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, next_retry_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evaluation_trace_status_created ON evaluation_trace(status, created_at);
CREATE TABLE IF NOT EXISTS evaluation_result (
    trace_id VARCHAR(64) PRIMARY KEY REFERENCES evaluation_trace(trace_id) ON DELETE CASCADE,
    result JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluation_job (
    job_id VARCHAR(64) PRIMARY KEY, job_type VARCHAR(32) NOT NULL, status VARCHAR(32) NOT NULL,
    payload JSONB, report JSONB, error TEXT, created_at TIMESTAMPTZ NOT NULL, started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evaluation_job_status_created ON evaluation_job(status, created_at);
