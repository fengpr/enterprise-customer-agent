-- 客户可自行置顶会话；置顶只影响当前客户列表的展示顺序。
ALTER TABLE chat_session ADD COLUMN IF NOT EXISTS pinned_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_chat_session_customer_pinned_updated
ON chat_session(customer_id, pinned_at DESC NULLS LAST, updated_at DESC)
WHERE deleted_at IS NULL;
