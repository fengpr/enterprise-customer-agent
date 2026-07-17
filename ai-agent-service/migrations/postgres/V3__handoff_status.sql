-- 将人工接管生命周期从通用会话状态中拆分，避免 AI 回复覆盖排队或接入状态。
ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS handoff_status VARCHAR(16) NOT NULL DEFAULT 'NONE';

UPDATE chat_session
SET handoff_status = CASE status
    WHEN 'HUMAN_PENDING' THEN 'PENDING'
    WHEN 'HUMAN_ACTIVE' THEN 'ACTIVE'
    WHEN 'HUMAN_CLOSED' THEN 'CLOSED'
    ELSE handoff_status
END
WHERE status IN ('HUMAN_PENDING', 'HUMAN_ACTIVE', 'HUMAN_CLOSED');

UPDATE chat_session SET status = 'AI_ONLY'
WHERE status IN ('HUMAN_PENDING', 'HUMAN_ACTIVE', 'HUMAN_CLOSED');

CREATE INDEX IF NOT EXISTS idx_chat_session_handoff_status
ON chat_session(handoff_status, updated_at DESC) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS handoff_ticket_link (
    session_no VARCHAR(64) PRIMARY KEY,
    ticket_no VARCHAR(64) NOT NULL,
    created_at TIMESTAMP NOT NULL
);
