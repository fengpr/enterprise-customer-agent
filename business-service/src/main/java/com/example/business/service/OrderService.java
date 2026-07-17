package com.example.business.service;

import com.example.business.dto.OrderDetailView;
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
            rs.getInt("quantity"),
            rs.getString("order_status"),
            toLocalDateTime(rs.getTimestamp("pay_time")),
            toLocalDateTime(rs.getTimestamp("ship_time")),
            toLocalDateTime(rs.getTimestamp("sign_time")),
            rs.getBigDecimal("amount"),
            rs.getString("after_sale_status"),
            rs.getString("receiver_name"),
            rs.getString("receiver_phone_masked"),
            rs.getString("shipping_address"),
            rs.getString("payment_method"),
            rs.getString("delivery_method"),
            rs.getBigDecimal("freight_amount")
    );
    private final RowMapper<OrderView> orderViewRowMapper = (rs, rowNum) -> new OrderView(
            rs.getLong("id"),
            rs.getString("order_no"),
            rs.getLong("customer_id"),
            rs.getLong("product_id"),
            rs.getString("product_name"),
            rs.getString("product_category"),
            rs.getInt("quantity"),
            rs.getInt("warranty_days"),
            rs.getInt("returnable") == 1,
            rs.getString("order_status"),
            toLocalDateTime(rs.getTimestamp("pay_time")),
            toLocalDateTime(rs.getTimestamp("ship_time")),
            toLocalDateTime(rs.getTimestamp("sign_time")),
            rs.getBigDecimal("amount"),
            rs.getString("after_sale_status")
    );
    private final RowMapper<OrderDetailView> orderDetailRowMapper = (rs, rowNum) -> new OrderDetailView(
            rs.getLong("id"), rs.getString("order_no"), rs.getLong("customer_id"), rs.getLong("product_id"),
            rs.getString("product_name"), rs.getString("product_category"), rs.getInt("quantity"), rs.getInt("warranty_days"),
            rs.getInt("returnable") == 1, rs.getString("order_status"), toLocalDateTime(rs.getTimestamp("pay_time")),
            toLocalDateTime(rs.getTimestamp("ship_time")), toLocalDateTime(rs.getTimestamp("sign_time")),
            rs.getBigDecimal("amount"), rs.getString("after_sale_status"), rs.getString("receiver_name"),
            rs.getString("receiver_phone_masked"), rs.getString("shipping_address"), rs.getString("payment_method"),
            rs.getString("delivery_method"), rs.getBigDecimal("freight_amount")
    );

    public OrderService(JdbcTemplate jdbcTemplate, @Value("${spring.datasource.driver-class-name:org.sqlite.JDBC}") String driverClassName) {
        this.jdbcTemplate = jdbcTemplate;
        // PostgreSQL 结构由 Flyway 创建，避免执行 SQLite 方言 DDL。
        if (!driverClassName.toLowerCase().contains("postgresql")) {
            initTables();
            ensureDetailColumns();
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

    /** 查询订单页所需完整详情；控制器负责按当前登录客户进行归属过滤。 */
    public Optional<OrderDetailView> findDetailViewByOrderNo(String orderNo) {
        List<OrderDetailView> result = jdbcTemplate.query(
                orderViewSql() + " WHERE lower(o.order_no) = lower(?)", orderDetailRowMapper, orderNo
        );
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
                    quantity INTEGER NOT NULL DEFAULT 1,
                    order_status VARCHAR(32) NOT NULL,
                    pay_time TIMESTAMP,
                    ship_time TIMESTAMP,
                    sign_time TIMESTAMP,
                    amount DECIMAL(10,2),
                    after_sale_status VARCHAR(32),
                    receiver_name VARCHAR(64),
                    receiver_phone_masked VARCHAR(32),
                    shipping_address VARCHAR(255),
                    payment_method VARCHAR(64),
                    delivery_method VARCHAR(64),
                    freight_amount DECIMAL(10,2)
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

    /** SQLite 旧库需要在启动时幂等补齐订单详情字段。 */
    private void ensureDetailColumns() {
        ensureColumn("receiver_name", "VARCHAR(64)");
        ensureColumn("receiver_phone_masked", "VARCHAR(32)");
        ensureColumn("shipping_address", "VARCHAR(255)");
        ensureColumn("payment_method", "VARCHAR(64)");
        ensureColumn("delivery_method", "VARCHAR(64)");
        ensureColumn("freight_amount", "DECIMAL(10,2)");
        // 旧库没有商品数量时按一件回填，确保统计口径稳定且兼容历史订单。
        ensureColumn("quantity", "INTEGER NOT NULL DEFAULT 1");
        jdbcTemplate.update("UPDATE order_info SET quantity = 1 WHERE quantity IS NULL OR quantity < 1");
    }

    /** 仅在列不存在时更新 SQLite 表结构，避免重复启动失败。 */
    private void ensureColumn(String columnName, String columnType) {
        boolean exists = jdbcTemplate.queryForList("PRAGMA table_info(order_info)").stream()
                .anyMatch(column -> columnName.equalsIgnoreCase(String.valueOf(column.get("name"))));
        if (!exists) jdbcTemplate.execute("ALTER TABLE order_info ADD COLUMN " + columnName + " " + columnType);
    }

    private void seedOrders() {
        LocalDateTime now = LocalDateTime.now();
        // Demo 订单覆盖已签收和已发货场景，便于 Agent 演示不同售后分支。
        insertSeedIfAbsent(new OrderInfo(
                1L,
                "EC202606220001",
                1L,
                1001L,
                1,
                "SIGNED",
                now.minusDays(8),
                now.minusDays(7),
                now.minusDays(4),
                new BigDecimal("399.00"),
                "NONE",
                "张先生", "138****8888", "广东省深圳市南山区科技园南区 科苑路15号科技大厦B座1205", "支付宝支付", "顺丰速运", BigDecimal.ZERO
        ));
        insertSeedIfAbsent(new OrderInfo(
                2L,
                "EC202606220002",
                2L,
                1002L,
                1,
                "SHIPPED",
                now.minusDays(2),
                now.minusDays(1),
                null,
                new BigDecimal("699.00"),
                "NONE",
                "李女士", "139****6677", "浙江省杭州市西湖区文三路90号创新中心806", "微信支付", "顺丰速运", BigDecimal.ZERO
        ));

        // 为 Demo Customer 补充可覆盖签收与运输中物流场景的订单，前端和 Agent 可直接查询使用。
        insertDemoCustomerOrder(11L, "EC202607160001", 1001L, 1, "SIGNED", now.minusDays(10), now.minusDays(9), now.minusDays(7), "399.00");
        insertDemoCustomerOrder(12L, "EC202607160002", 1002L, 1, "SHIPPED", now.minusDays(9), now.minusDays(8), null, "699.00");
        insertDemoCustomerOrder(13L, "EC202607160003", 1001L, 1, "SIGNED", now.minusDays(8), now.minusDays(7), now.minusDays(5), "399.00");
        insertDemoCustomerOrder(14L, "EC202607160004", 1002L, 1, "SHIPPED", now.minusDays(7), now.minusDays(6), null, "699.00");
        insertDemoCustomerOrder(15L, "EC202607160005", 1001L, 1, "SIGNED", now.minusDays(6), now.minusDays(5), now.minusDays(3), "399.00");
        insertDemoCustomerOrder(16L, "EC202607160006", 1002L, 1, "SHIPPED", now.minusDays(5), now.minusDays(4), null, "699.00");
        insertDemoCustomerOrder(17L, "EC202607160007", 1001L, 1, "SIGNED", now.minusDays(4), now.minusDays(3), now.minusDays(1), "399.00");
        insertDemoCustomerOrder(18L, "EC202607160008", 1002L, 1, "SHIPPED", now.minusDays(3), now.minusDays(2), null, "699.00");
        insertDemoCustomerOrder(19L, "EC202607160009", 1001L, 1, "SIGNED", now.minusDays(2), now.minusDays(1), now.minusHours(12), "399.00");
        insertDemoCustomerOrder(20L, "EC202607160010", 1002L, 1, "SHIPPED", now.minusDays(1), now.minusHours(12), null, "699.00");
        seedExistingDemoOrderDetails();
    }

    /** 仅在订单号尚未存在时写入 Seed，服务重启不会重复创建演示订单。 */
    private void insertSeedIfAbsent(OrderInfo order) {
        jdbcTemplate.update(
                """
                INSERT INTO order_info (
                    id, order_no, customer_id, product_id, quantity, order_status,
                    pay_time, ship_time, sign_time, amount, after_sale_status,
                    receiver_name, receiver_phone_masked, shipping_address, payment_method, delivery_method, freight_amount
                )
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM order_info WHERE order_no = ?)
                """,
                order.id(),
                order.orderNo(),
                order.customerId(),
                order.productId(),
                order.quantity(),
                order.orderStatus(),
                toTimestamp(order.payTime()),
                toTimestamp(order.shipTime()),
                toTimestamp(order.signTime()),
                order.amount(),
                order.afterSaleStatus(),
                order.receiverName(),
                order.receiverPhoneMasked(),
                order.shippingAddress(),
                order.paymentMethod(),
                order.deliveryMethod(),
                order.freightAmount(),
                order.orderNo()
        );
    }

    /** 构造归属 Demo Customer 的标准订单，确保订单与物流 Seed 使用固定订单号关联。 */
    private void insertDemoCustomerOrder(
            Long id, String orderNo, Long productId, Integer quantity, String status,
            LocalDateTime payTime, LocalDateTime shipTime, LocalDateTime signTime, String amount
    ) {
        insertSeedIfAbsent(new OrderInfo(
                id, orderNo, 1L, productId, quantity, status, payTime, shipTime, signTime,
                new BigDecimal(amount), "NONE",
                "张先生", "138****8888", "广东省深圳市南山区科技园南区 科苑路15号科技大厦B座1205", "支付宝支付", "顺丰速运", BigDecimal.ZERO
        ));
    }

    /** 为旧库中已存在的 Demo Customer 订单补齐真实接口可读取的详情字段。 */
    private void seedExistingDemoOrderDetails() {
        jdbcTemplate.update(
                "UPDATE order_info SET receiver_name = ?, receiver_phone_masked = ?, shipping_address = ?, payment_method = ?, delivery_method = ?, freight_amount = ? WHERE customer_id = ? AND (receiver_name IS NULL OR receiver_name = '')",
                "张先生", "138****8888", "广东省深圳市南山区科技园南区 科苑路15号科技大厦B座1205", "支付宝支付", "顺丰速运", BigDecimal.ZERO, 1L
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
                    o.quantity,
                    p.warranty_days,
                    p.returnable,
                    o.order_status,
                    o.pay_time,
                    o.ship_time,
                    o.sign_time,
                    o.amount,
                    o.after_sale_status,
                    o.receiver_name,
                    o.receiver_phone_masked,
                    o.shipping_address,
                    o.payment_method,
                    o.delivery_method,
                    o.freight_amount
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
