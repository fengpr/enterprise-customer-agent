-- 在途工单补充信息采用审计追加；取件履约字段是否直接更新由 TicketService 状态规则决定。
CREATE TABLE IF NOT EXISTS ticket_supplement (
    id BIGSERIAL PRIMARY KEY,
    ticket_no VARCHAR(64) NOT NULL REFERENCES support_ticket(ticket_no),
    customer_id BIGINT NOT NULL REFERENCES customer(id),
    idempotency_key VARCHAR(128) NOT NULL,
    content TEXT,
    after_sale_reason VARCHAR(500),
    requested_return_method VARCHAR(32),
    requested_pickup_time_window VARCHAR(128),
    update_mode VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(ticket_no, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_ticket_supplement_ticket_created
    ON ticket_supplement(ticket_no, created_at DESC);
