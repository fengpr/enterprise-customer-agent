package com.example.business.service;

import com.example.business.entity.Customer;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Optional;

/**
 * 客户服务，使用 SQLite 持久化 Demo 客户资料，模拟存量用户系统的只读查询能力。
 */
@Service
public class CustomerService {
    private final JdbcTemplate jdbcTemplate;
    private final RowMapper<Customer> customerRowMapper = (rs, rowNum) -> new Customer(
            rs.getLong("id"),
            rs.getString("customer_name"),
            rs.getString("phone_masked"),
            rs.getString("level"),
            rs.getInt("status")
    );

    public CustomerService(JdbcTemplate jdbcTemplate, @Value("${spring.datasource.driver-class-name:org.sqlite.JDBC}") String driverClassName) {
        this.jdbcTemplate = jdbcTemplate;
        // PostgreSQL profile 的表结构由 Flyway 管理，SQLite 开发模式保留自动建表。
        if (!driverClassName.toLowerCase().contains("postgresql")) {
            initTables();
            seedCustomers();
        }
    }

    /**
     * 根据客户 ID 查询用户资料。
     *
     * @param id 客户主键
     * @return 可能存在的客户信息
     */
    public Optional<Customer> findById(Long id) {
        List<Customer> result = jdbcTemplate.query(
                """
                SELECT * FROM customer
                WHERE id = ?
                """,
                customerRowMapper,
                id
        );
        return result.stream().findFirst();
    }

    /**
     * 根据手机号后缀查询客户，用于后续扩展访客身份匹配。
     *
     * @param suffix 手机号后几位
     * @return 可能匹配的客户信息
     */
    public Optional<Customer> findByPhoneSuffix(String suffix) {
        List<Customer> result = jdbcTemplate.query(
                """
                SELECT * FROM customer
                WHERE phone_masked LIKE ?
                """,
                customerRowMapper,
                "%" + suffix
        );
        return result.stream().findFirst();
    }

    private void initTables() {
        jdbcTemplate.execute(
                """
                CREATE TABLE IF NOT EXISTS customer (
                    id BIGINT PRIMARY KEY,
                    customer_name VARCHAR(64) NOT NULL,
                    phone_masked VARCHAR(32) NOT NULL,
                    level VARCHAR(32),
                    status INTEGER NOT NULL DEFAULT 1
                )
                """
        );
    }

    private void seedCustomers() {
        Integer count = jdbcTemplate.queryForObject("SELECT COUNT(*) FROM customer", Integer.class);
        if (count != null && count > 0) {
            return;
        }
        // Demo 数据只保存脱敏手机号，避免客户侧和 Agent 日志暴露完整隐私信息。
        insertSeed(1L, "Demo Customer", "138****8001", "GOLD", 1);
        insertSeed(2L, "Test Buyer", "139****8002", "SILVER", 1);
    }

    private void insertSeed(Long id, String customerName, String phoneMasked, String level, Integer status) {
        jdbcTemplate.update(
                """
                INSERT INTO customer (id, customer_name, phone_masked, level, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                id,
                customerName,
                phoneMasked,
                level,
                status
        );
    }
}
