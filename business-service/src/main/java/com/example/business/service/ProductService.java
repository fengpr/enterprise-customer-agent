package com.example.business.service;

import com.example.business.entity.Product;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Optional;

/**
 * 商品服务，使用 SQLite 持久化 Demo 商品资料，模拟商品中心的基础资料查询能力。
 */
@Service
public class ProductService {
    private final JdbcTemplate jdbcTemplate;
    private final RowMapper<Product> productRowMapper = (rs, rowNum) -> new Product(
            rs.getLong("id"),
            rs.getString("product_name"),
            rs.getString("category"),
            rs.getInt("warranty_days"),
            rs.getInt("returnable") == 1,
            rs.getInt("status")
    );

    public ProductService(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
        initTables();
        seedProducts();
    }

    /**
     * 根据商品 ID 查询商品详情。
     *
     * @param id 商品主键
     * @return 可能存在的商品信息
     */
    public Optional<Product> findById(Long id) {
        List<Product> result = jdbcTemplate.query(
                """
                SELECT * FROM product
                WHERE id = ?
                """,
                productRowMapper,
                id
        );
        return result.stream().findFirst();
    }

    /**
     * 查询全部 Demo 商品，供客服工作台或调试接口使用。
     *
     * @return 商品列表
     */
    public List<Product> list() {
        return jdbcTemplate.query(
                """
                SELECT * FROM product
                ORDER BY id
                """,
                productRowMapper
        );
    }

    private void initTables() {
        jdbcTemplate.execute(
                """
                CREATE TABLE IF NOT EXISTS product (
                    id BIGINT PRIMARY KEY,
                    product_name VARCHAR(128) NOT NULL,
                    category VARCHAR(64),
                    warranty_days INTEGER,
                    returnable INTEGER NOT NULL DEFAULT 1,
                    status INTEGER NOT NULL DEFAULT 1
                )
                """
        );
    }

    private void seedProducts() {
        Integer count = jdbcTemplate.queryForObject("SELECT COUNT(*) FROM product", Integer.class);
        if (count != null && count > 0) {
            return;
        }
        // 商品属性包含保修期和是否支持退货，为售后规则判断和客户侧订单展示预留数据。
        insertSeed(1001L, "Smart Router AX3000", "network", 365, true, 1);
        insertSeed(1002L, "Noise Cancelling Headset", "audio", 180, true, 1);
    }

    private void insertSeed(
            Long id,
            String productName,
            String category,
            Integer warrantyDays,
            Boolean returnable,
            Integer status
    ) {
        jdbcTemplate.update(
                """
                INSERT INTO product (id, product_name, category, warranty_days, returnable, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                id,
                productName,
                category,
                warrantyDays,
                Boolean.TRUE.equals(returnable) ? 1 : 0,
                status
        );
    }
}
