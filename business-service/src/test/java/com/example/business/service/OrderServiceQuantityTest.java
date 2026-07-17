package com.example.business.service;

import com.example.business.entity.OrderInfo;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.sqlite.SQLiteDataSource;
import org.springframework.jdbc.core.JdbcTemplate;

import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 验证订单商品数量能够持久化返回，并兼容没有 quantity 字段的历史 SQLite 数据库。
 */
class OrderServiceQuantityTest {

    @TempDir
    Path tempDir;

    /** 新建数据库中的演示订单必须显式返回合法商品数量。 */
    @Test
    void shouldExposePersistedQuantity() {
        JdbcTemplate jdbcTemplate = new JdbcTemplate(dataSource("order-quantity.db"));
        jdbcTemplate.execute("CREATE TABLE product (id BIGINT PRIMARY KEY, product_name VARCHAR(128), category VARCHAR(64), warranty_days INTEGER, returnable INTEGER)");
        jdbcTemplate.update("INSERT INTO product VALUES (?, ?, ?, ?, ?)", 1001L, "路由器", "网络设备", 365, 1);
        jdbcTemplate.update("INSERT INTO product VALUES (?, ?, ?, ?, ?)", 1002L, "耳机", "音频设备", 365, 1);
        OrderService service = new OrderService(jdbcTemplate, "org.sqlite.JDBC");

        List<OrderInfo> orders = service.findByCustomerId(1L);

        assertTrue(orders.size() >= 11);
        assertTrue(orders.stream().allMatch(order -> order.quantity() != null && order.quantity() >= 1));
        assertTrue(service.findViewsByCustomerId(1L).stream().allMatch(order -> order.quantity() >= 1));
    }

    /** 历史订单表升级时，缺失的商品数量应安全回填为一件。 */
    @Test
    void shouldBackfillLegacyOrderQuantity() {
        JdbcTemplate jdbcTemplate = new JdbcTemplate(dataSource("legacy-order.db"));
        jdbcTemplate.execute("""
                CREATE TABLE order_info (
                    id BIGINT PRIMARY KEY,
                    order_no VARCHAR(64) UNIQUE NOT NULL,
                    customer_id BIGINT NOT NULL,
                    product_id BIGINT NOT NULL,
                    order_status VARCHAR(32) NOT NULL,
                    pay_time TIMESTAMP,
                    ship_time TIMESTAMP,
                    sign_time TIMESTAMP,
                    amount DECIMAL(10,2),
                    after_sale_status VARCHAR(32)
                )
                """);
        jdbcTemplate.update(
                "INSERT INTO order_info (id, order_no, customer_id, product_id, order_status, amount) VALUES (?, ?, ?, ?, ?, ?)",
                100L, "EC-LEGACY-001", 99L, 1001L, "SIGNED", 199
        );

        OrderService service = new OrderService(jdbcTemplate, "org.sqlite.JDBC");

        assertEquals(1, service.findByCustomerId(99L).get(0).quantity());
    }

    /** 为每个测试创建独立 SQLite 数据源，避免数据相互污染。 */
    private SQLiteDataSource dataSource(String fileName) {
        SQLiteDataSource dataSource = new SQLiteDataSource();
        dataSource.setUrl("jdbc:sqlite:" + tempDir.resolve(fileName));
        return dataSource;
    }
}
