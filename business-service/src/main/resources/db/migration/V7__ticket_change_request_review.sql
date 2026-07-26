-- 座席审核履约变更申请的审计字段；客户侧仅展示 customer_message。
ALTER TABLE ticket_change_request
    ADD COLUMN IF NOT EXISTS reviewed_by BIGINT;

ALTER TABLE ticket_change_request
    ADD COLUMN IF NOT EXISTS customer_message VARCHAR(500);

ALTER TABLE ticket_change_request
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;
