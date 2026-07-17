package com.example.business.service;

import com.example.business.dto.LogisticsTraceView;
import com.example.business.dto.LogisticsView;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.sql.Timestamp;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Optional;
import java.util.stream.Collectors;

/**
 * 物流服务，负责持久化 Demo 物流主数据和轨迹节点，并提供按订单号查询的只读能力。
 */
@Service
public class LogisticsService {
    private final JdbcTemplate jdbcTemplate;

    private final RowMapper<LogisticsInfoRow> logisticsInfoRowMapper = (rs, rowNum) -> new LogisticsInfoRow(
            rs.getLong("id"),
            rs.getString("order_no"),
            rs.getString("carrier_name"),
            rs.getString("tracking_no"),
            rs.getString("logistics_status"),
            rs.getString("latest_location"),
            toLocalDateTime(rs.getTimestamp("estimated_delivery_time"))
    );

    private final RowMapper<LogisticsTraceView> logisticsTraceRowMapper = (rs, rowNum) -> new LogisticsTraceView(
            rs.getString("trace_status"),
            rs.getString("trace_desc"),
            rs.getString("location"),
            rs.getString("station_name"),
            toLocalDateTime(rs.getTimestamp("occurred_at"))
    );

    public LogisticsService(JdbcTemplate jdbcTemplate, @Value("${spring.datasource.driver-class-name:org.sqlite.JDBC}") String driverClassName) {
        this.jdbcTemplate = jdbcTemplate;
        // PostgreSQL profile 下由 Flyway 管理物流表，SQLite profile 保持本地初始化。
        if (!driverClassName.toLowerCase().contains("postgresql")) {
            initTables();
            seedLogistics();
        }
    }

    /**
     * 按订单号查询物流详情，调用方需要先完成订单归属校验。
     *
     * @param orderNo 已确认属于当前客户的订单号
     * @return 物流主信息和完整轨迹，未发货或暂无同步时返回空
     */
    public Optional<LogisticsView> findByOrderNo(String orderNo) {
        List<LogisticsInfoRow> logisticsRows = jdbcTemplate.query(
                """
                SELECT * FROM logistics_info
                WHERE lower(order_no) = lower(?)
                """,
                logisticsInfoRowMapper,
                orderNo
        );
        return logisticsRows.stream().findFirst().map(this::buildView);
    }

    private LogisticsView buildView(LogisticsInfoRow info) {
        List<LogisticsTraceView> traces = jdbcTemplate.query(
                """
                SELECT trace_status, trace_desc, location, station_name, occurred_at
                FROM logistics_trace
                WHERE logistics_id = ?
                ORDER BY occurred_at ASC, id ASC
                """,
                logisticsTraceRowMapper,
                info.id()
        );
        String routeSummary = traces.stream()
                .map(trace -> trace.stationName() == null || trace.stationName().isBlank() ? trace.location() : trace.stationName())
                .filter(value -> value != null && !value.isBlank())
                .distinct()
                .collect(Collectors.joining(" -> "));
        return new LogisticsView(
                info.orderNo(),
                info.carrierName(),
                info.trackingNo(),
                info.logisticsStatus(),
                info.latestLocation(),
                info.estimatedDeliveryTime(),
                routeSummary,
                traces
        );
    }

    private void initTables() {
        jdbcTemplate.execute(
                """
                CREATE TABLE IF NOT EXISTS logistics_info (
                    id BIGINT PRIMARY KEY,
                    order_no VARCHAR(64) UNIQUE NOT NULL,
                    carrier_name VARCHAR(64) NOT NULL,
                    tracking_no VARCHAR(64) NOT NULL,
                    logistics_status VARCHAR(32) NOT NULL,
                    latest_location VARCHAR(128),
                    estimated_delivery_time TIMESTAMP,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP,
                    FOREIGN KEY (order_no) REFERENCES order_info(order_no)
                )
                """
        );
        jdbcTemplate.execute(
                """
                CREATE TABLE IF NOT EXISTS logistics_trace (
                    id BIGINT PRIMARY KEY,
                    logistics_id BIGINT NOT NULL,
                    trace_status VARCHAR(32) NOT NULL,
                    trace_desc VARCHAR(255) NOT NULL,
                    location VARCHAR(128),
                    station_name VARCHAR(128),
                    occurred_at TIMESTAMP,
                    FOREIGN KEY (logistics_id) REFERENCES logistics_info(id)
                )
                """
        );
        jdbcTemplate.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_logistics_info_order
                ON logistics_info(order_no)
                """
        );
        jdbcTemplate.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_logistics_trace_info_time
                ON logistics_trace(logistics_id, occurred_at)
                """
        );
    }

    private void seedLogistics() {
        LocalDateTime now = LocalDateTime.now();
        // Demo 物流数据与现有两笔订单号强关联，覆盖签收完成与转运中两种常见查询场景。
        if (!logisticsExists("EC202606220001")) {
            insertLogistics(1L, "EC202606220001", "顺丰速运", "SF202606220001", "SIGNED", "上海浦东签收点", now.minusDays(4), now);
            insertTrace(1001L, 1L, "SHIPPED", "商家已发货，快件已完成揽收。", "杭州", "杭州仓库", now.minusDays(7).plusHours(2));
            insertTrace(1002L, 1L, "IN_TRANSIT", "快件已到达始发地集散中心。", "杭州", "杭州集散中心", now.minusDays(7).plusHours(7));
            insertTrace(1003L, 1L, "IN_TRANSIT", "快件已到达华东转运中心，准备发往上海。", "苏州", "华东转运中心", now.minusDays(6).plusHours(6));
            insertTrace(1004L, 1L, "OUT_FOR_DELIVERY", "快件已到达派送站，快递员正在派送。", "上海", "上海浦东派送站", now.minusDays(4).minusHours(4));
            insertTrace(1005L, 1L, "SIGNED", "快件已由本人签收。", "上海", "上海浦东签收点", now.minusDays(4));
        }

        if (!logisticsExists("EC202606220002")) {
            insertLogistics(2L, "EC202606220002", "顺丰速运", "SF202606230002", "IN_TRANSIT", "上海转运中心", now.plusDays(1), now);
            insertTrace(2001L, 2L, "SHIPPED", "商家已发货，快件已完成揽收。", "杭州", "杭州仓库", now.minusDays(1).plusHours(2));
            insertTrace(2002L, 2L, "IN_TRANSIT", "快件已离开杭州集散中心。", "杭州", "杭州集散中心", now.minusHours(18));
            insertTrace(2003L, 2L, "IN_TRANSIT", "快件已到达上海转运中心，等待下一站分拨。", "上海", "上海转运中心", now.minusHours(5));
        }

        // 十笔订单均建立主物流记录与可追溯轨迹，覆盖运输中、派送中和已签收状态。
        seedDemoCustomerLogistics(11L, "EC202607160001", "SIGNED", now.minusDays(7));
        seedDemoCustomerLogistics(12L, "EC202607160002", "IN_TRANSIT", now.minusDays(6));
        seedDemoCustomerLogistics(13L, "EC202607160003", "SIGNED", now.minusDays(5));
        seedDemoCustomerLogistics(14L, "EC202607160004", "OUT_FOR_DELIVERY", now.minusDays(4));
        seedDemoCustomerLogistics(15L, "EC202607160005", "SIGNED", now.minusDays(3));
        seedDemoCustomerLogistics(16L, "EC202607160006", "IN_TRANSIT", now.minusDays(2));
        seedDemoCustomerLogistics(17L, "EC202607160007", "SIGNED", now.minusDays(1));
        seedDemoCustomerLogistics(18L, "EC202607160008", "OUT_FOR_DELIVERY", now.minusHours(22));
        seedDemoCustomerLogistics(19L, "EC202607160009", "SIGNED", now.minusHours(12));
        seedDemoCustomerLogistics(20L, "EC202607160010", "IN_TRANSIT", now.minusHours(8));
    }

    /** 为单笔 Demo Customer 订单补齐物流主记录和标准轨迹，重启服务时保持幂等。 */
    private void seedDemoCustomerLogistics(Long logisticsId, String orderNo, String status, LocalDateTime startTime) {
        String trackingNo = "SF" + orderNo.substring(2);
        String latestLocation = switch (status) {
            case "SIGNED" -> "上海浦东签收点";
            case "OUT_FOR_DELIVERY" -> "上海浦东派送站";
            default -> "上海转运中心";
        };
        if (!logisticsExists(orderNo)) {
            insertLogistics(logisticsId, orderNo, "顺丰速运", trackingNo, status, latestLocation,
                    "SIGNED".equals(status) ? startTime : startTime.plusDays(1), LocalDateTime.now());
        }
        // 已有完整轨迹的订单不重复插入；若服务在中途启动失败，后续启动可补齐缺失轨迹。
        if (logisticsTraceExists(logisticsId)) return;
        Long traceBaseId = 10_000L + logisticsId * 10L;
        insertTrace(traceBaseId + 1, logisticsId, "SHIPPED", "商家已发货，快件已完成揽收。", "杭州", "杭州仓库", startTime.minusHours(8));
        insertTrace(traceBaseId + 2, logisticsId, "IN_TRANSIT", "快件已离开杭州集散中心，正在发往上海。", "杭州", "杭州集散中心", startTime.minusHours(4));
        if ("IN_TRANSIT".equals(status)) {
            insertTrace(traceBaseId + 3, logisticsId, "IN_TRANSIT", "快件已到达上海转运中心，等待下一站分拨。", "上海", "上海转运中心", startTime);
            return;
        }
        insertTrace(traceBaseId + 3, logisticsId, "OUT_FOR_DELIVERY", "快件已到达派送站，快递员正在派送。", "上海", "上海浦东派送站", startTime);
        if ("SIGNED".equals(status)) {
            insertTrace(traceBaseId + 4, logisticsId, "SIGNED", "快件已由本人签收。", "上海", "上海浦东签收点", startTime.plusHours(2));
        }
    }

    private boolean logisticsExists(String orderNo) {
        Integer count = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM logistics_info WHERE order_no = ?", Integer.class, orderNo
        );
        return count != null && count > 0;
    }

    private boolean logisticsTraceExists(Long logisticsId) {
        Integer count = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM logistics_trace WHERE logistics_id = ?", Integer.class, logisticsId
        );
        return count != null && count > 0;
    }

    private void insertLogistics(
            Long id,
            String orderNo,
            String carrierName,
            String trackingNo,
            String logisticsStatus,
            String latestLocation,
            LocalDateTime estimatedDeliveryTime,
            LocalDateTime now
    ) {
        jdbcTemplate.update(
                """
                INSERT INTO logistics_info (
                    id, order_no, carrier_name, tracking_no, logistics_status,
                    latest_location, estimated_delivery_time, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                id,
                orderNo,
                carrierName,
                trackingNo,
                logisticsStatus,
                latestLocation,
                toTimestamp(estimatedDeliveryTime),
                toTimestamp(now),
                toTimestamp(now)
        );
    }

    private void insertTrace(
            Long id,
            Long logisticsId,
            String traceStatus,
            String traceDesc,
            String location,
            String stationName,
            LocalDateTime occurredAt
    ) {
        jdbcTemplate.update(
                """
                INSERT INTO logistics_trace (
                    id, logistics_id, trace_status, trace_desc, location, station_name, occurred_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                id,
                logisticsId,
                traceStatus,
                traceDesc,
                location,
                stationName,
                toTimestamp(occurredAt)
        );
    }

    private record LogisticsInfoRow(
            Long id,
            String orderNo,
            String carrierName,
            String trackingNo,
            String logisticsStatus,
            String latestLocation,
            LocalDateTime estimatedDeliveryTime
    ) {
    }

    private static Timestamp toTimestamp(LocalDateTime value) {
        return value == null ? null : Timestamp.valueOf(value);
    }

    private static LocalDateTime toLocalDateTime(Timestamp value) {
        return value == null ? null : value.toLocalDateTime();
    }
}
