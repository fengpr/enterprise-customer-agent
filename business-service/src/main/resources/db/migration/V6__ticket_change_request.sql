-- 已锁定或关闭的售后工单不能被客户消息直接改写；履约变更以独立申请进入可审计的人工处理队列。
CREATE TABLE IF NOT EXISTS ticket_change_request (
    id BIGSERIAL PRIMARY KEY,
    request_no VARCHAR(64) NOT NULL UNIQUE,
    parent_ticket_no VARCHAR(64) NOT NULL REFERENCES support_ticket(ticket_no),
    customer_id BIGINT NOT NULL REFERENCES customer(id),
    external_session_no VARCHAR(128),
    change_type VARCHAR(32) NOT NULL,
    previous_return_method VARCHAR(32),
    previous_pickup_time_window VARCHAR(128),
    requested_return_method VARCHAR(32),
    requested_pickup_time_window VARCHAR(128),
    parent_ticket_status VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(parent_ticket_no, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_ticket_change_request_parent_updated
    ON ticket_change_request(parent_ticket_no, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_ticket_change_request_customer_status
    ON ticket_change_request(customer_id, status, updated_at DESC);
