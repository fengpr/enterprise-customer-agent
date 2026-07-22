-- 座席主动展开会话历史的审计记录，不保存聊天正文或客户凭证。
CREATE TABLE IF NOT EXISTS staff_handoff_audit_log (
    id BIGSERIAL PRIMARY KEY,
    session_no VARCHAR(64) NOT NULL REFERENCES chat_session(session_no) ON DELETE CASCADE,
    staff_id VARCHAR(64) NOT NULL,
    action VARCHAR(64) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_staff_handoff_audit_session_created
    ON staff_handoff_audit_log(session_no, created_at DESC);
