-- 会话结构化状态：数据库保存权威快照，Redis 仅用于缓存与短期锁。
CREATE TABLE IF NOT EXISTS conversation_state (
    session_no VARCHAR(64) PRIMARY KEY REFERENCES chat_session(session_no) ON DELETE CASCADE,
    state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL
);

-- 定时复核任务：用于物流到期复查等只读后台动作。
CREATE TABLE IF NOT EXISTS scheduled_followup (
    followup_id VARCHAR(64) PRIMARY KEY,
    session_no VARCHAR(64) NOT NULL REFERENCES chat_session(session_no) ON DELETE CASCADE,
    customer_id BIGINT NOT NULL,
    task_type VARCHAR(32) NOT NULL,
    order_no VARCHAR(64) NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(24) NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    idempotency_key VARCHAR(160) NOT NULL UNIQUE,
    result_summary JSONB,
    error_code VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_followup_due
    ON scheduled_followup(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_followup_customer
    ON scheduled_followup(customer_id, created_at DESC);

-- 站内通知仅保存客户可见摘要，不保存工具原始响应。
CREATE TABLE IF NOT EXISTS customer_notification (
    notification_id VARCHAR(64) PRIMARY KEY,
    customer_id BIGINT NOT NULL,
    session_no VARCHAR(64) NOT NULL REFERENCES chat_session(session_no) ON DELETE CASCADE,
    followup_id VARCHAR(64) REFERENCES scheduled_followup(followup_id) ON DELETE SET NULL,
    notification_type VARCHAR(32) NOT NULL,
    title VARCHAR(160) NOT NULL,
    content TEXT NOT NULL,
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    read_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_customer_notification_unread
    ON customer_notification(customer_id, is_read, created_at DESC);
