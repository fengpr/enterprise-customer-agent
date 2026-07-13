package com.example.business.service;

import com.example.business.entity.Employee;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.LocalDateTime;
import java.util.Arrays;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * 员工服务，负责统一持久化内部员工账号、角色和坐席派单配置。
 */
@Service
public class EmployeeService {
    private final JdbcTemplate jdbcTemplate;
    private final RowMapper<Employee> employeeRowMapper = (rs, rowNum) -> new Employee(
            nullableLong(rs, "user_id"),
            rs.getString("username"),
            rs.getString("password"),
            rs.getString("display_name"),
            rs.getString("role"),
            rs.getString("group_name"),
            parseSkills(rs.getString("skills")),
            rs.getInt("online") == 1,
            rs.getInt("accepting_tickets") == 1,
            rs.getInt("max_active_tickets"),
            rs.getInt("enabled") == 1,
            toLocalDateTime(rs.getTimestamp("created_at")),
            toLocalDateTime(rs.getTimestamp("updated_at"))
    );

    public EmployeeService(JdbcTemplate jdbcTemplate, @Value("${spring.datasource.driver-class-name:org.sqlite.JDBC}") String driverClassName) {
        this.jdbcTemplate = jdbcTemplate;
        // PostgreSQL profile 禁止服务层 DDL，统一由 Flyway migration 建表。
        if (!driverClassName.toLowerCase().contains("postgresql")) {
            initTables();
            seedEmployees();
        }
    }

    /**
     * 按用户名和明文密码查找 Demo 员工账号。
     *
     * @param username 登录用户名
     * @param password 登录密码
     * @return 匹配且启用的员工
     */
    public Optional<Employee> findEnabledByCredentials(String username, String password) {
        List<Employee> result = jdbcTemplate.query(
                """
                SELECT * FROM employee
                WHERE username = ? AND password = ? AND enabled = 1
                """,
                employeeRowMapper,
                username,
                password
        );
        return result.stream().findFirst();
    }

    /**
     * 查询所有启用员工。
     *
     * @return 员工列表
     */
    public List<Employee> listEnabled() {
        return jdbcTemplate.query(
                """
                SELECT * FROM employee
                WHERE enabled = 1
                ORDER BY user_id
                """,
                employeeRowMapper
        );
    }

    /**
     * 查询全部启用坐席，调度和其他角色不会进入派单候选池。
     *
     * @return 启用坐席列表
     */
    public List<Employee> listEnabledStaff() {
        return jdbcTemplate.query(
                """
                SELECT * FROM employee
                WHERE enabled = 1 AND role = 'staff'
                ORDER BY user_id
                """,
                employeeRowMapper
        );
    }

    /**
     * 按员工 ID 查找启用员工。
     *
     * @param userId 员工 ID
     * @return 员工信息
     */
    public Optional<Employee> findEnabledByUserId(Long userId) {
        List<Employee> result = jdbcTemplate.query(
                """
                SELECT * FROM employee
                WHERE user_id = ? AND enabled = 1
                """,
                employeeRowMapper,
                userId
        );
        return result.stream().findFirst();
    }

    /**
     * 更新坐席在线、接单和最大并发量配置。
     *
     * @param userId 目标坐席 ID
     * @param online 是否在线
     * @param acceptingTickets 是否接单
     * @param maxActiveTickets 最大并发量
     * @return 更新后的坐席员工
     */
    public Employee updateStaffAvailability(
            Long userId,
            Boolean online,
            Boolean acceptingTickets,
            Integer maxActiveTickets
    ) {
        Employee employee = findEnabledByUserId(userId)
                .orElseThrow(() -> new IllegalArgumentException("员工不存在：" + userId));
        if (!"staff".equals(employee.role())) {
            throw new IllegalArgumentException("只能配置坐席员工：" + userId);
        }
        int nextMaxActiveTickets = maxActiveTickets == null ? employee.maxActiveTickets() : maxActiveTickets;
        if (nextMaxActiveTickets < 1) {
            throw new IllegalArgumentException("最大并发量必须大于等于 1");
        }

        jdbcTemplate.update(
                """
                UPDATE employee
                SET online = ?, accepting_tickets = ?, max_active_tickets = ?, updated_at = ?
                WHERE user_id = ? AND enabled = 1 AND role = 'staff'
                """,
                toInt(online == null ? employee.online() : online),
                toInt(acceptingTickets == null ? employee.acceptingTickets() : acceptingTickets),
                nextMaxActiveTickets,
                toTimestamp(LocalDateTime.now()),
                userId
        );
        return findEnabledByUserId(userId)
                .orElseThrow(() -> new IllegalArgumentException("员工不存在：" + userId));
    }

    private void initTables() {
        jdbcTemplate.execute(
                """
                CREATE TABLE IF NOT EXISTS employee (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(64) UNIQUE NOT NULL,
                    password VARCHAR(128) NOT NULL,
                    display_name VARCHAR(128) NOT NULL,
                    role VARCHAR(32) NOT NULL,
                    group_name VARCHAR(128),
                    skills CLOB,
                    online INTEGER NOT NULL DEFAULT 1,
                    accepting_tickets INTEGER NOT NULL DEFAULT 1,
                    max_active_tickets INTEGER NOT NULL DEFAULT 5,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                )
                """
        );
        jdbcTemplate.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_employee_role_enabled
                ON employee(role, enabled)
                """
        );
    }

    private void seedEmployees() {
        Integer count = jdbcTemplate.queryForObject("SELECT COUNT(*) FROM employee", Integer.class);
        if (count != null && count > 0) {
            return;
        }
        LocalDateTime now = LocalDateTime.now();
        insertSeed(10001L, "staff", "123456", "Demo Staff", "staff", "客服组", "other,logistics,exchange", true, true, 5, now);
        insertSeed(10002L, "staff2", "123456", "售后坐席", "staff", "售后组", "refund,exchange,repair", true, true, 3, now);
        insertSeed(10003L, "staff3", "123456", "投诉专员", "staff", "投诉处理组", "complaint,refund,exchange", true, true, 2, now);
        insertSeed(20001L, "dispatcher", "123456", "调度主管", "dispatcher", "客服调度组", "", true, false, 1, now);
    }

    private void insertSeed(
            Long userId,
            String username,
            String password,
            String displayName,
            String role,
            String groupName,
            String skills,
            boolean online,
            boolean acceptingTickets,
            int maxActiveTickets,
            LocalDateTime now
    ) {
        jdbcTemplate.update(
                """
                INSERT INTO employee (
                    user_id, username, password, display_name, role, group_name, skills,
                    online, accepting_tickets, max_active_tickets, enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                userId,
                username,
                password,
                displayName,
                role,
                groupName,
                skills,
                toInt(online),
                toInt(acceptingTickets),
                maxActiveTickets,
                toTimestamp(now),
                toTimestamp(now)
        );
    }

    private static Set<String> parseSkills(String skills) {
        if (skills == null || skills.isBlank()) {
            return Set.of();
        }
        return Arrays.stream(skills.split(","))
                .map(String::trim)
                .filter(item -> !item.isBlank())
                .collect(Collectors.toSet());
    }

    private static int toInt(boolean value) {
        return value ? 1 : 0;
    }

    private static Timestamp toTimestamp(LocalDateTime value) {
        return value == null ? null : Timestamp.valueOf(value);
    }

    private static LocalDateTime toLocalDateTime(Timestamp value) {
        return value == null ? null : value.toLocalDateTime();
    }

    private static Long nullableLong(ResultSet rs, String columnName) throws SQLException {
        long value = rs.getLong(columnName);
        return rs.wasNull() ? null : value;
    }
}
