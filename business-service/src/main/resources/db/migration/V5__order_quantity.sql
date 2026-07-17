-- 订单商品数量用于客户侧真实件数统计；旧订单按一件兼容回填。
ALTER TABLE order_info ADD COLUMN IF NOT EXISTS quantity INTEGER NOT NULL DEFAULT 1;
UPDATE order_info SET quantity = 1 WHERE quantity IS NULL OR quantity < 1;
