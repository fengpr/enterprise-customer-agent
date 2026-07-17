-- 为客户订单详情补充支付、配送和收货字段；手机号只保存脱敏展示值。
ALTER TABLE order_info ADD COLUMN IF NOT EXISTS receiver_name VARCHAR(64);
ALTER TABLE order_info ADD COLUMN IF NOT EXISTS receiver_phone_masked VARCHAR(32);
ALTER TABLE order_info ADD COLUMN IF NOT EXISTS shipping_address VARCHAR(255);
ALTER TABLE order_info ADD COLUMN IF NOT EXISTS payment_method VARCHAR(64);
ALTER TABLE order_info ADD COLUMN IF NOT EXISTS delivery_method VARCHAR(64);
ALTER TABLE order_info ADD COLUMN IF NOT EXISTS freight_amount NUMERIC(10,2);
