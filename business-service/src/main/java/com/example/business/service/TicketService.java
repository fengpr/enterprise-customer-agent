package com.example.business.service;

import com.example.business.entity.SupportTicket;
import com.example.business.entity.TicketStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.List;
import java.util.Set;

/**
 * 工单服务，负责工单创建、持久化、派单元数据和状态流转约束。
 */
@Service
public class TicketService {
    private final JdbcTemplate jdbcTemplate;
    private final RowMapper<SupportTicket> ticketRowMapper = (rs, rowNum) -> new SupportTicket(
            nullableLong(rs, "id"),
            rs.getString("ticket_no"),
            rs.getString("title"),
            rs.getString("ticket_type"),
            rs.getString("priority"),
            nullableLong(rs, "customer_id"),
            rs.getString("order_no"),
            nullableLong(rs, "session_id"),
            rs.getString("external_session_no"),
            rs.getString("content"),
            rs.getString("ai_summary"),
            rs.getString("assigned_group"),
            nullableLong(rs, "handler_id"),
            rs.getString("assigned_by"),
            rs.getString("assignment_reason"),
            nullableInteger(rs, "assignment_score"),
            toLocalDateTime(rs.getTimestamp("assigned_at")),
            rs.getString("status"),
            toLocalDateTime(rs.getTimestamp("sla_deadline")),
            rs.getString("source"),
            nullableInteger(rs, "urge_count"),
            toLocalDateTime(rs.getTimestamp("last_urged_at")),
            rs.getString("last_urge_reason"),
            toLocalDateTime(rs.getTimestamp("created_at")),
            toLocalDateTime(rs.getTimestamp("updated_at"))
    );

    public TicketService(
            JdbcTemplate jdbcTemplate,
            @Value("${spring.datasource.driver-class-name:org.sqlite.JDBC}") String driverClassName
    ) {
        this.jdbcTemplate = jdbcTemplate;
        // PostgreSQL 由 Flyway 管理结构，禁止执行 SQLite 的 AUTOINCREMENT/PRAGMA 兼容逻辑。
        if (!driverClassName.toLowerCase().contains("postgresql")) {
            initTables();
        }
    }

    /**
     * 查询全部工单，供内部排查和坐席工作台使用。
     *
     * @return 全部工单记录
     */
    public List<SupportTicket> list() {
        return jdbcTemplate.query(
                """
                SELECT * FROM support_ticket
                ORDER BY updated_at DESC, id DESC
                """,
                ticketRowMapper
        );
    }

    /**
     * 按状态查询工单，供坐席工作台队列筛选。
     *
     * @param statuses 目标状态集合，为空时返回全部工单
     * @return 匹配状态的工单记录
     */
    public List<SupportTicket> listByStatuses(Set<String> statuses) {
        return list().stream()
                .filter(ticket -> statuses == null || statuses.isEmpty() || statuses.contains(ticket.status()))
                .toList();
    }

    /**
     * 查询普通坐席可见工单：自己的工单，以及仍未分配的待分派工单。
     *
     * @param handlerId 当前坐席 ID
     * @param statuses 状态筛选
     * @return 坐席可见工单
     */
    public List<SupportTicket> listVisibleForStaff(Long handlerId, Set<String> statuses) {
        return listByStatuses(statuses).stream()
                .filter(ticket -> handlerId.equals(ticket.handlerId())
                        || ticket.handlerId() == null
                        || TicketStatus.PENDING_ASSIGN.name().equals(ticket.status()))
                .toList();
    }

    /**
     * 查询坐席当前活跃工单，供调度台展示坐席工作内容。
     *
     * @param handlerId 坐席 ID
     * @return 坐席当前未完成工单
     */
    public List<SupportTicket> listActiveTicketsByHandler(Long handlerId) {
        return jdbcTemplate.query(
                """
                SELECT * FROM support_ticket
                WHERE handler_id = ?
                  AND status IN ('PENDING_PROCESS', 'PROCESSING')
                ORDER BY updated_at DESC, id DESC
                """,
                ticketRowMapper,
                handlerId
        );
    }

    /**
     * 查询指定客户的工单列表，客户侧只能看到自己的工单。
     *
     * @param customerId 当前登录客户 ID
     * @return 客户名下工单
     */
    public List<SupportTicket> listByCustomerId(Long customerId) {
        return jdbcTemplate.query(
                """
                SELECT * FROM support_ticket
                WHERE customer_id = ?
                ORDER BY updated_at DESC, id DESC
                """,
                ticketRowMapper,
                customerId
        );
    }

    /**
     * 创建工单并补齐系统字段。
     *
     * @param ticket Agent 或前端提交的工单草稿
     * @return 新建工单
     */
    public SupportTicket create(SupportTicket ticket) {
        return createForCustomer(ticket, ticket.customerId());
    }

    /**
     * 为当前登录客户创建工单，忽略请求体中可能伪造的 customerId。
     *
     * @param ticket Agent 提交的工单草稿
     * @param customerId Token 中解析出的客户 ID
     * @return 新建工单
     */
    public SupportTicket createForCustomer(SupportTicket ticket, Long customerId) {
        LocalDateTime now = LocalDateTime.now();
        String ticketNo = "T" + now.format(DateTimeFormatter.ofPattern("yyyyMMddHHmmss"))
                + String.format("%06d", Math.abs(System.nanoTime() % 1_000_000));
        SupportTicket draft = new SupportTicket(
                null,
                ticketNo,
                ticket.title(),
                ticket.ticketType(),
                ticket.priority(),
                customerId,
                ticket.orderNo(),
                ticket.sessionId(),
                ticket.externalSessionNo(),
                ticket.content(),
                ticket.aiSummary(),
                ticket.assignedGroup(),
                ticket.handlerId(),
                ticket.assignedBy(),
                ticket.assignmentReason(),
                ticket.assignmentScore(),
                ticket.assignedAt(),
                TicketStatus.PENDING_ASSIGN.name(),
                now.plusHours(24),
                ticket.source(),
                0,
                null,
                null,
                now,
                now
        );

        insertDraft(draft);
        return detail(ticketNo);
    }

    /**
     * 按工单号查询工单详情。
     *
     * @param ticketNo 工单编号
     * @return 工单详情
     */
    public SupportTicket detail(String ticketNo) {
        return mustFind(ticketNo);
    }

    /**
     * 查询客户自己的工单详情，防止越权读取。
     *
     * @param ticketNo 工单编号
     * @param customerId 当前客户 ID
     * @return 工单详情
     */
    public SupportTicket detailForCustomer(String ticketNo, Long customerId) {
        SupportTicket ticket = mustFind(ticketNo);
        if (!customerId.equals(ticket.customerId())) {
            throw new IllegalArgumentException("无权访问该工单：" + ticketNo);
        }
        return ticket;
    }

    /**
     * 客户催办自己的工单，写入催办日志并更新工单最近催办摘要。
     *
     * @param ticketNo 工单编号
     * @param customerId 当前客户 ID
     * @param reason 客户催办原因或补充说明
     * @return 更新后的工单
     */
    public SupportTicket urgeForCustomer(String ticketNo, Long customerId, String reason) {
        SupportTicket ticket = detailForCustomer(ticketNo, customerId);
        if (TicketStatus.CLOSED.name().equals(ticket.status())) {
            throw new IllegalArgumentException("工单已关闭，暂不支持催办：" + ticketNo);
        }
        LocalDateTime now = LocalDateTime.now();
        String cleanedReason = reason == null || reason.isBlank() ? "客户催办处理进度" : reason.trim();
        jdbcTemplate.update(
                """
                INSERT INTO ticket_urge_log (ticket_no, customer_id, reason, created_at)
                VALUES (?, ?, ?, ?)
                """,
                ticketNo,
                customerId,
                cleanedReason,
                Timestamp.valueOf(now)
        );
        int rows = jdbcTemplate.update(
                """
                UPDATE support_ticket
                SET urge_count = COALESCE(urge_count, 0) + 1,
                    last_urged_at = ?,
                    last_urge_reason = ?,
                    updated_at = ?
                WHERE ticket_no = ?
                """,
                Timestamp.valueOf(now),
                cleanedReason,
                Timestamp.valueOf(now),
                ticketNo
        );
        if (rows == 0) {
            throw new IllegalArgumentException("工单不存在：" + ticketNo);
        }
        return detailForCustomer(ticketNo, customerId);
    }

    /**
     * 坐席手动领取或分派工单。
     *
     * @param ticketNo 工单编号
     * @param handlerId 处理人 ID
     * @param assignedGroup 处理组
     * @return 分派后的工单
     */
    public SupportTicket assign(String ticketNo, Long handlerId, String assignedGroup) {
        SupportTicket ticket = mustFind(ticketNo);
        assertStatusIn(ticket, "分派", Set.of(
                TicketStatus.PENDING_ASSIGN.name(),
                TicketStatus.PENDING_PROCESS.name(),
                TicketStatus.TRANSFERRED.name(),
                TicketStatus.REOPENED.name()
        ));
        return replace(rebuild(
                ticket,
                assignedGroup,
                handlerId,
                "MANUAL",
                "坐席手动领取或分派工单",
                null,
                LocalDateTime.now(),
                TicketStatus.PENDING_PROCESS.name()
        ));
    }

    /**
     * 根据自动派单决策写入处理人和派单说明。
     *
     * @param ticketNo 工单编号
     * @param handlerId 推荐处理人，为空时只记录未派出原因
     * @param assignedGroup 推荐处理组
     * @param assignedBy 派单来源
     * @param assignmentReason 派单原因
     * @param assignmentScore 派单分
     * @return 更新后的工单
     */
    public SupportTicket assignAutomatically(
            String ticketNo,
            Long handlerId,
            String assignedGroup,
            String assignedBy,
            String assignmentReason,
            Integer assignmentScore
    ) {
        SupportTicket ticket = mustFind(ticketNo);
        if (!Set.of(
                TicketStatus.PENDING_ASSIGN.name(),
                TicketStatus.TRANSFERRED.name(),
                TicketStatus.REOPENED.name()
        ).contains(ticket.status())) {
            return ticket;
        }

        if (handlerId == null) {
            return replace(rebuild(
                    ticket,
                    assignedGroup,
                    null,
                    assignedBy,
                    assignmentReason,
                    assignmentScore,
                    null,
                    ticket.status()
            ));
        }

        return replace(rebuild(
                ticket,
                assignedGroup,
                handlerId,
                assignedBy,
                assignmentReason,
                assignmentScore,
                LocalDateTime.now(),
                TicketStatus.PENDING_PROCESS.name()
        ));
    }

    /**
     * 手动转派工单到其他处理组或处理人。
     *
     * @param ticketNo 工单编号
     * @param assignedGroup 新处理组
     * @param handlerId 新处理人
     * @return 转派后的工单
     */
    public SupportTicket transfer(String ticketNo, String assignedGroup, Long handlerId) {
        SupportTicket ticket = mustFind(ticketNo);
        assertStatusIn(ticket, "转派", Set.of(
                TicketStatus.PENDING_PROCESS.name(),
                TicketStatus.PROCESSING.name(),
                TicketStatus.REOPENED.name()
        ));
        return replace(rebuild(
                ticket,
                assignedGroup,
                handlerId,
                "MANUAL",
                "坐席手动转派工单",
                null,
                LocalDateTime.now(),
                TicketStatus.TRANSFERRED.name()
        ));
    }

    /**
     * 推进工单状态，例如开始处理。
     *
     * @param ticketNo 工单编号
     * @param targetStatus 目标状态
     * @return 更新后的工单
     */
    public SupportTicket updateStatus(String ticketNo, String targetStatus) {
        SupportTicket ticket = mustFind(ticketNo);
        TicketStatus status = parseStatus(targetStatus);
        validateTransition(ticket.status(), status.name());
        return replace(rebuildWithExistingAssignment(ticket, status.name()));
    }

    /**
     * 关闭已处理完成的工单。
     *
     * @param ticketNo 工单编号
     * @return 关闭后的工单
     */
    public SupportTicket close(String ticketNo) {
        SupportTicket ticket = mustFind(ticketNo);
        assertStatusIn(ticket, "关闭", Set.of(
                TicketStatus.PENDING_PROCESS.name(),
                TicketStatus.PROCESSING.name(),
                TicketStatus.TRANSFERRED.name(),
                TicketStatus.REOPENED.name()
        ));
        return replace(rebuildWithExistingAssignment(ticket, TicketStatus.CLOSED.name()));
    }

    /**
     * 重开已关闭工单。
     *
     * @param ticketNo 工单编号
     * @return 重开后的工单
     */
    public SupportTicket reopen(String ticketNo) {
        SupportTicket ticket = mustFind(ticketNo);
        assertStatusIn(ticket, "重开", Set.of(TicketStatus.CLOSED.name()));
        return replace(rebuildWithExistingAssignment(ticket, TicketStatus.REOPENED.name()));
    }

    /**
     * 统计坐席当前待处理和处理中工单数，用于最少负载派单。
     *
     * @param handlerId 坐席 ID
     * @return 当前活跃负载
     */
    public int countActiveTicketsByHandler(Long handlerId) {
        Integer count = jdbcTemplate.queryForObject(
                """
                SELECT COUNT(*) FROM support_ticket
                WHERE handler_id = ?
                  AND status IN ('PENDING_PROCESS', 'PROCESSING')
                """,
                Integer.class,
                handlerId
        );
        return count == null ? 0 : count;
    }

    /**
     * 查询同一客户最近由坐席处理过的工单，用于熟客优先。
     *
     * @param customerId 客户 ID
     * @param currentTicketNo 当前工单号
     * @return 最近历史工单，没有则返回 null
     */
    public SupportTicket findRecentHandledTicket(Long customerId, String currentTicketNo) {
        List<SupportTicket> result = jdbcTemplate.query(
                """
                SELECT * FROM support_ticket
                WHERE customer_id = ?
                  AND ticket_no <> ?
                  AND handler_id IS NOT NULL
                  AND status IN ('PENDING_PROCESS', 'PROCESSING', 'CLOSED')
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                ticketRowMapper,
                customerId,
                currentTicketNo
        );
        return result.isEmpty() ? null : result.get(0);
    }

    private void initTables() {
        jdbcTemplate.execute(
                """
                CREATE TABLE IF NOT EXISTS support_ticket (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_no VARCHAR(64) UNIQUE,
                    title VARCHAR(128),
                    ticket_type VARCHAR(64),
                    priority VARCHAR(32),
                    customer_id BIGINT NOT NULL,
                    order_no VARCHAR(64),
                    session_id BIGINT,
                    external_session_no VARCHAR(64),
                    content CLOB,
                    ai_summary CLOB,
                    assigned_group VARCHAR(128),
                    handler_id BIGINT,
                    assigned_by VARCHAR(32),
                    assignment_reason CLOB,
                    assignment_score INTEGER,
                    assigned_at TIMESTAMP,
                    status VARCHAR(32) NOT NULL,
                    sla_deadline TIMESTAMP,
                    source VARCHAR(64),
                    urge_count INTEGER DEFAULT 0,
                    last_urged_at TIMESTAMP,
                    last_urge_reason CLOB,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                )
                """
        );
        jdbcTemplate.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_urge_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_no VARCHAR(64) NOT NULL,
                    customer_id BIGINT NOT NULL,
                    reason CLOB,
                    created_at TIMESTAMP NOT NULL
                )
                """
        );
        jdbcTemplate.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_ticket_urge_log_ticket_created
                ON ticket_urge_log(ticket_no, created_at)
                """
        );
        jdbcTemplate.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_support_ticket_customer_updated
                ON support_ticket(customer_id, updated_at)
                """
        );
        jdbcTemplate.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_support_ticket_status_updated
                ON support_ticket(status, updated_at)
                """
        );
        jdbcTemplate.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_support_ticket_external_session
                ON support_ticket(external_session_no)
                """
        );
        ensureColumn("assigned_by", "VARCHAR(32)");
        ensureColumn("assignment_reason", "CLOB");
        ensureColumn("assignment_score", "INTEGER");
        ensureColumn("assigned_at", "TIMESTAMP");
        ensureColumn("urge_count", "INTEGER DEFAULT 0");
        ensureColumn("last_urged_at", "TIMESTAMP");
        ensureColumn("last_urge_reason", "CLOB");
    }

    private void insertDraft(SupportTicket ticket) {
        jdbcTemplate.update(connection -> {
            PreparedStatement ps = connection.prepareStatement(
                    """
                    INSERT INTO support_ticket (
                        ticket_no, title, ticket_type, priority, customer_id, order_no,
                        session_id, external_session_no, content, ai_summary, assigned_group,
                        handler_id, assigned_by, assignment_reason, assignment_score, assigned_at,
                        status, sla_deadline, source, urge_count, last_urged_at, last_urge_reason, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
            );
            ps.setString(1, ticket.ticketNo());
            ps.setString(2, ticket.title());
            ps.setString(3, ticket.ticketType());
            ps.setString(4, ticket.priority());
            ps.setObject(5, ticket.customerId());
            ps.setString(6, ticket.orderNo());
            ps.setObject(7, ticket.sessionId());
            ps.setString(8, ticket.externalSessionNo());
            ps.setString(9, ticket.content());
            ps.setString(10, ticket.aiSummary());
            ps.setString(11, ticket.assignedGroup());
            ps.setObject(12, ticket.handlerId());
            ps.setString(13, ticket.assignedBy());
            ps.setString(14, ticket.assignmentReason());
            ps.setObject(15, ticket.assignmentScore());
            ps.setTimestamp(16, toTimestamp(ticket.assignedAt()));
            ps.setString(17, ticket.status());
            ps.setTimestamp(18, toTimestamp(ticket.slaDeadline()));
            ps.setString(19, ticket.source());
            ps.setObject(20, ticket.urgeCount());
            ps.setTimestamp(21, toTimestamp(ticket.lastUrgedAt()));
            ps.setString(22, ticket.lastUrgeReason());
            ps.setTimestamp(23, toTimestamp(ticket.createdAt()));
            ps.setTimestamp(24, toTimestamp(ticket.updatedAt()));
            return ps;
        });
    }

    private SupportTicket mustFind(String ticketNo) {
        List<SupportTicket> result = jdbcTemplate.query(
                "SELECT * FROM support_ticket WHERE ticket_no = ?",
                ticketRowMapper,
                ticketNo
        );
        if (result.isEmpty()) {
            throw new IllegalArgumentException("工单不存在：" + ticketNo);
        }
        return result.get(0);
    }

    private SupportTicket replace(SupportTicket updated) {
        int rows = jdbcTemplate.update(
                """
                UPDATE support_ticket
                SET title = ?, ticket_type = ?, priority = ?, customer_id = ?, order_no = ?,
                    session_id = ?, external_session_no = ?, content = ?, ai_summary = ?,
                    assigned_group = ?, handler_id = ?, assigned_by = ?, assignment_reason = ?,
                    assignment_score = ?, assigned_at = ?, status = ?, sla_deadline = ?,
                    source = ?, urge_count = ?, last_urged_at = ?, last_urge_reason = ?,
                    created_at = ?, updated_at = ?
                WHERE ticket_no = ?
                """,
                updated.title(),
                updated.ticketType(),
                updated.priority(),
                updated.customerId(),
                updated.orderNo(),
                updated.sessionId(),
                updated.externalSessionNo(),
                updated.content(),
                updated.aiSummary(),
                updated.assignedGroup(),
                updated.handlerId(),
                updated.assignedBy(),
                updated.assignmentReason(),
                updated.assignmentScore(),
                toTimestamp(updated.assignedAt()),
                updated.status(),
                toTimestamp(updated.slaDeadline()),
                updated.source(),
                updated.urgeCount(),
                toTimestamp(updated.lastUrgedAt()),
                updated.lastUrgeReason(),
                toTimestamp(updated.createdAt()),
                toTimestamp(updated.updatedAt()),
                updated.ticketNo()
        );
        if (rows == 0) {
            throw new IllegalArgumentException("工单不存在：" + updated.ticketNo());
        }
        return mustFind(updated.ticketNo());
    }

    private SupportTicket rebuildWithExistingAssignment(SupportTicket ticket, String status) {
        return rebuild(
                ticket,
                ticket.assignedGroup(),
                ticket.handlerId(),
                ticket.assignedBy(),
                ticket.assignmentReason(),
                ticket.assignmentScore(),
                ticket.assignedAt(),
                status
        );
    }

    private SupportTicket rebuild(
            SupportTicket ticket,
            String assignedGroup,
            Long handlerId,
            String assignedBy,
            String assignmentReason,
            Integer assignmentScore,
            LocalDateTime assignedAt,
            String status
    ) {
        return new SupportTicket(
                ticket.id(),
                ticket.ticketNo(),
                ticket.title(),
                ticket.ticketType(),
                ticket.priority(),
                ticket.customerId(),
                ticket.orderNo(),
                ticket.sessionId(),
                ticket.externalSessionNo(),
                ticket.content(),
                ticket.aiSummary(),
                assignedGroup == null || assignedGroup.isBlank() ? ticket.assignedGroup() : assignedGroup,
                handlerId == null ? ticket.handlerId() : handlerId,
                assignedBy,
                assignmentReason,
                assignmentScore,
                assignedAt,
                status,
                ticket.slaDeadline(),
                ticket.source(),
                ticket.urgeCount(),
                ticket.lastUrgedAt(),
                ticket.lastUrgeReason(),
                ticket.createdAt(),
                LocalDateTime.now()
        );
    }

    private TicketStatus parseStatus(String status) {
        if (status == null || status.isBlank()) {
            throw new IllegalArgumentException("工单状态不能为空");
        }
        try {
            return TicketStatus.valueOf(status.trim().toUpperCase());
        } catch (IllegalArgumentException ex) {
            throw new IllegalArgumentException("不支持的工单状态：" + status);
        }
    }

    private void validateTransition(String currentStatus, String targetStatus) {
        if (currentStatus.equals(targetStatus)) {
            return;
        }
        if (TicketStatus.PENDING_PROCESS.name().equals(currentStatus)
                && TicketStatus.PROCESSING.name().equals(targetStatus)) {
            return;
        }
        if (TicketStatus.TRANSFERRED.name().equals(currentStatus)
                && TicketStatus.PENDING_PROCESS.name().equals(targetStatus)) {
            return;
        }
        if (TicketStatus.REOPENED.name().equals(currentStatus)
                && TicketStatus.PROCESSING.name().equals(targetStatus)) {
            return;
        }
        throw new IllegalArgumentException("非法工单状态流转：" + currentStatus + " -> " + targetStatus);
    }

    private void assertStatusIn(SupportTicket ticket, String action, Set<String> allowedStatuses) {
        if (!allowedStatuses.contains(ticket.status())) {
            throw new IllegalArgumentException("当前状态不允许" + action + "：" + ticket.status());
        }
    }

    private void ensureColumn(String columnName, String columnType) {
        Boolean exists = jdbcTemplate.query(
                "PRAGMA table_info(support_ticket)",
                rs -> {
                    while (rs.next()) {
                        if (columnName.equalsIgnoreCase(rs.getString("name"))) {
                            return true;
                        }
                    }
                    return false;
                }
        );
        if (!Boolean.TRUE.equals(exists)) {
            jdbcTemplate.execute("ALTER TABLE support_ticket ADD COLUMN " + columnName + " " + columnType);
        }
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

    private static Integer nullableInteger(ResultSet rs, String columnName) throws SQLException {
        int value = rs.getInt(columnName);
        return rs.wasNull() ? null : value;
    }
}
