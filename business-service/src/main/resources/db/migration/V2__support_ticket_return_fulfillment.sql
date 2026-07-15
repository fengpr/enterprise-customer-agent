-- 退货履约信息由工单统一持久化；取件时间为客户偏好，不表示承运方已经确认预约。
ALTER TABLE support_ticket ADD COLUMN IF NOT EXISTS return_method VARCHAR(32);
ALTER TABLE support_ticket ADD COLUMN IF NOT EXISTS pickup_time_window VARCHAR(128);
ALTER TABLE support_ticket ADD COLUMN IF NOT EXISTS pickup_status VARCHAR(32);

CREATE INDEX IF NOT EXISTS idx_support_ticket_pickup_status
    ON support_ticket(pickup_status)
    WHERE pickup_status IS NOT NULL;
