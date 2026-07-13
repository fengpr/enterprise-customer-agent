package com.example.business.service;

import com.example.business.dto.OrderView;
import com.example.business.entity.OrderInfo;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.math.BigDecimal;
import java.sql.Timestamp;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;

/**
 * 订单服务，使用 SQLite 持久化 Demo 订单，并通过受控查询接口提供客户隔离后的订单数据。
 */
@Service
public class OrderService {
    private final JdbcTemplate jdbcTemplate;
    private final RowMapper<OrderInfo> orderRowMapper = (rs, rowNum) -> new OrderInfo(
            rs.getLong("id"),
            rs.getString("order_no"),
            rs.getLong("customer_id"),
            rs.getLong("product_id"),
            rs.getString("order_status"),
            toLocalDateTime(rs.getTimestamp("pay_time")),
            toLocalDateTime(rs.getTimestamp("ship_time")),
            toLocalDateTime(rs.getTimestamp("sign_time")),
            rs.getBigDecimal("amount"),
            rs.getString("after_sale_status")
    );
    private final RowMapper<OrderView> orderViewRowMapper = (rs, rowNum) -> new OrderView(
            rs.getLong("id"),
            rs.getString("order_no"),
            rs.getLong("customer_id"),
            rs.getLong("product_id"),
            rs.getString("product_name"),
            rs.getString("product_category"),
            rs.getInt("warranty_days"),
            rs.getInt("returnable") == 1,
            rs.getString("order_status"),
            toLocalDateTime(rs.getTimestamp("pay_time")),
            toLocalDateTime(rs.getTimestamp("ship_time")),
            toLocalDateTime(rs.getTimestamp("sign_time")),
            rs.getBigDecimal("amount"),
            rs.getString("after_sale_status")
    );

    public OrderService(JdbcTemplate jdbcTemplate, @Value("${spring.datasource.driver-class-name:org.sqlite.JDBC}") String driverClassName) {
        this.jdbcTemplate = jdbcTemplate;
        // PostgreSQL 结构由 Flyway 创建，避免执行 SQLite 方言 DDL。
        if (!driverClassName.toLowerCase().contains("postgresql")) {
            initTables();
            seedOrders();
        }
    }

    /**
     * 根据订单号查询订单详情。
     *
     * @param orderNo 订单号，来自用户输入或 Agent 抽取
     * @return 可能存在的订单信息
     */
    public Optional<OrderInfo> findByOrderNo(String orderNo) {
        List<OrderInfo> result = jdbcTemplate.query(
                """
                SELECT * FROM order_info
                WHERE lower(order_no) = lower(?)
                """,
                orderRowMapper,
                orderNo
        );
        return result.stream().findFirst();
    }

    /**
     * 根据订单号查询带商品名称的订单展示数据。
     *
     * @param orderNo 订单号，来自用户输入或客户侧选择
     * @return 可能存在的订单展示信息
     */
    public Optional<OrderView> findViewByOrderNo(String orderNo) {
        List<OrderView> result = jdbcTemplate.query(orderViewSql() + " WHERE lower(o.order_no) = lower(?)", orderViewRowMapper, orderNo);
        return result.stream().findFirst();
    }

    /**
     * 查询指定客户的全部订单，用于“我的订单”这类依赖登录客户上下文的场景。
     *
     * @param customerId 客户主键
     * @return 该客户关联的订单列表
     */
    public List<OrderInfo> findByCustomerId(Long customerId) {
        return jdbcTemplate.query(
                """
                SELECT * FROM order_info
                WHERE customer_id = ?
                ORDER BY pay_time DESC, id DESC
                """,
                orderRowMapper,
                customerId
        );
    }

    /**
     * 查询指定客户的订单展示列表，包含商品名称等客户侧选择咨询需要的信息。
     *
     * @param customerId 客户主键
     * @return 该客户关联的订单展示列表
     */
    public List<OrderView> findViewsByCustomerId(Long customerId) {
        return jdbcTemplate.query(
                orderViewSql() + " WHERE o.customer_id = ? ORDER BY o.pay_time DESC, o.id DESC",
                orderViewRowMapper,
                customerId
        );
    }

    private void initTables() {
        jdbcTemplate.execute(
                """
                CREATE TABLE IF NOT EXISTS order_info (
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
                """
        );
        jdbcTemplate.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_order_info_customer
                ON order_info(customer_id, pay_time)
                """
        );
    }

    private void seedOrders() {
        Integer count = jdbcTemplate.queryForObject("SELECT COUNT(*) FROM order_info", Integer.class);
        if (count != null && count > 0) {
            return;
        }
        LocalDateTime now = LocalDateTime.now();
        // Demo 订单覆盖已签收和已发货场景，便于 Agent 演示不同售后分支。
        insertSeed(new OrderInfo(
                1L,
                "EC202606220001",
                1L,
                1001L,
                "SIGNED",
                now.minusDays(8),
                now.minusDays(7),
                now.minusDays(4),
                new BigDecimal("399.00"),
                "NONE"
        ));
        insertSeed(new OrderInfo(
                2L,
                "EC202606220002",
                2L,
                1002L,
                "SHIPPED",
                now.minusDays(2),
                now.minusDays(1),
                null,
                new BigDecimal("699.00"),
                "NONE"
        ));
    }

    private void insertSeed(OrderInfo order) {
        jdbcTemplate.update(
                """
                INSERT INTO order_info (
                    id, order_no, customer_id, product_id, order_status,
                    pay_time, ship_time, sign_time, amount, after_sale_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                order.id(),
                order.orderNo(),
                order.customerId(),
                order.productId(),
                order.orderStatus(),
                toTimestamp(order.payTime()),
                toTimestamp(order.shipTime()),
                toTimestamp(order.signTime()),
                order.amount(),
                order.afterSaleStatus()
        );
    }

    private String orderViewSql() {
        return """
                SELECT
                    o.id,
                    o.order_no,
                    o.customer_id,
                    o.product_id,
                    p.product_name,
                    p.category AS product_category,
                    p.warranty_days,
                    p.returnable,
                    o.order_status,
                    o.pay_time,
                    o.ship_time,
                    o.sign_time,
                    o.amount,
                    o.after_sale_status
                FROM order_info o
                LEFT JOIN product p ON p.id = o.product_id
                """;
    }

    private static Timestamp toTimestamp(LocalDateTime value) {
        return value == null ? null : Timestamp.valueOf(value);
    }

    private static LocalDateTime toLocalDateTime(Timestamp value) {
        return value == null ? null : value.toLocalDateTime();
    }
}
