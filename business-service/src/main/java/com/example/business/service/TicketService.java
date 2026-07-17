package com.example.business.service;

import com.example.business.dto.TicketSupplementRequest;
import com.example.business.dto.TicketSupplementResult;
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

import org.springframework.transaction.annotation.Transactional;

/**
 * 工单服务，负责工单创建、持久化、派单元数据和状态流转约束。
 */
@Service
public class TicketService {
    private static final Set<String> DIRECT_FULFILLMENT_UPDATE_STATUSES = Set.of(
            TicketStatus.PENDING_ASSIGN.name()
    );
    private static final Set<String> LOCKED_PICKUP_STATUSES = Set.of(
            "CONFIRMED", "SCHEDULED", "COURIER_ASSIGNED", "PICKED_UP", "COMPLETED"
    );
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
            rs.getString("return_method"),
            rs.getString("pickup_time_window"),
            rs.getString("pickup_status"),
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
                ticket.returnMethod(),
                ticket.pickupTimeWindow(),
                ticket.pickupStatus(),
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
     * 为人工会话按需创建异步跟进工单。同一会话存在未关闭工单时直接返回原工单，避免重复建单。
     *
     * @param customerId 由 Agent 会话归属读取的客户 ID
     * @param externalSessionNo Agent 会话编号，作为幂等业务键
     * @param title 工单标题
     * @param content 客户问题摘要
     * @param priority 工单优先级
     * @return 已存在或新创建的真实业务工单
     */
    @Transactional
    public synchronized SupportTicket createHandoffFollowUp(
            Long customerId,
            String externalSessionNo,
            String title,
            String content,
            String priority
    ) {
        if (customerId == null || externalSessionNo == null || externalSessionNo.isBlank()) {
            throw new IllegalArgumentException("人工会话客户和会话编号不能为空");
        }
        List<SupportTicket> existing = jdbcTemplate.query(
                """
                SELECT * FROM support_ticket
                WHERE external_session_no = ?
                  AND status NOT IN ('CLOSED', 'CANCELLED', 'REJECTED')
                ORDER BY id DESC
                LIMIT 1
                """,
                ticketRowMapper,
                externalSessionNo.trim()
        );
        if (!existing.isEmpty()) {
            SupportTicket ticket = existing.get(0);
            if (!customerId.equals(ticket.customerId())) {
                throw new IllegalArgumentException("人工会话工单归属不一致");
            }
            return ticket;
        }
        SupportTicket draft = new SupportTicket(
                null, null,
                title == null || title.isBlank() ? "人工客服异步跟进" : title.trim(),
                "人工跟进", priority == null || priority.isBlank() ? "medium" : priority.trim(),
                customerId, null, null, externalSessionNo.trim(),
                content == null || content.isBlank() ? "客户请求人工客服异步跟进" : content.trim(),
                null, null, null, null, "customer-service", null, null,
                null, null, null, null, null, "HUMAN_HANDOFF", 0, null, null, null, null
        );
        return createForCustomer(draft, customerId);
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
     * 向客户自己的在途工单追加退货信息，并依据履约阶段决定是否允许直接修改取件偏好。
     *
     * <p>退货原因等说明始终以审计记录追加；取件方式和取件时间只有在尚未分派且未锁定
     * 履约安排时才直接更新。工单一旦分派给工作人员或进入承运阶段，就只登记变更申请，
     * 避免临近取件时覆盖原安排。</p>
     *
     * @param ticketNo 工单编号
     * @param customerId 登录态客户 ID
     * @param request 客户补充内容
     * @param idempotencyKey 本次追加幂等键
     * @return 追加结果和最新工单
     */
    @Transactional
    public TicketSupplementResult appendSupplementForCustomer(
            String ticketNo,
            Long customerId,
            TicketSupplementRequest request,
            String idempotencyKey
    ) {
        if (idempotencyKey == null || idempotencyKey.isBlank()) {
            throw new IllegalArgumentException("追加工单信息必须提供 Idempotency-Key");
        }
        SupportTicket ticket = detailForCustomer(ticketNo, customerId);
        if (TicketStatus.CLOSED.name().equals(ticket.status())) {
            throw new IllegalArgumentException("工单已关闭，不能继续追加信息：" + ticketNo);
        }

        String content = cleanSupplementValue(request == null ? null : request.content(), 1000);
        String reason = cleanSupplementValue(request == null ? null : request.afterSaleReason(), 500);
        String returnMethod = cleanSupplementValue(request == null ? null : request.returnMethod(), 32);
        String pickupTimeWindow = cleanSupplementValue(request == null ? null : request.pickupTimeWindow(), 128);
        if (content == null && reason == null && returnMethod == null && pickupTimeWindow == null) {
            throw new IllegalArgumentException("没有可追加的工单信息");
        }

        String existingContent = (ticket.content() == null ? "" : ticket.content())
                + "\n" + (ticket.aiSummary() == null ? "" : ticket.aiSummary());
        boolean reasonIsNew = reason != null
                && !existingContent.contains("after_sale_reason: " + reason)
                && !existingContent.contains("退货原因：" + reason);
        boolean contentIsNew = content != null && !existingContent.contains(content);
        boolean returnMethodChanged = returnMethod != null && !returnMethod.equalsIgnoreCase(
                ticket.returnMethod() == null ? "" : ticket.returnMethod()
        );
        boolean pickupTimeChanged = pickupTimeWindow != null && !pickupTimeWindow.equals(
                ticket.pickupTimeWindow() == null ? "" : ticket.pickupTimeWindow()
        );
        boolean fulfillmentChangeRequested = returnMethodChanged || pickupTimeChanged;
        boolean fulfillmentLocked = ticket.pickupStatus() != null
                && LOCKED_PICKUP_STATUSES.contains(ticket.pickupStatus().trim().toUpperCase());
        boolean fulfillmentUpdated = fulfillmentChangeRequested
                && DIRECT_FULFILLMENT_UPDATE_STATUSES.contains(ticket.status())
                && !fulfillmentLocked;
        boolean hasNewInformation = reasonIsNew || contentIsNew || fulfillmentChangeRequested;
        String updateMode = !hasNewInformation
                ? "UNCHANGED"
                : (fulfillmentChangeRequested && !fulfillmentUpdated ? "REVIEW_REQUIRED" : "APPLIED");

        LocalDateTime now = LocalDateTime.now();
        String normalizedKey = idempotencyKey.trim();
        int inserted = jdbcTemplate.update(
                """
                INSERT INTO ticket_supplement (
                    ticket_no, customer_id, idempotency_key, content, after_sale_reason,
                    requested_return_method, requested_pickup_time_window, update_mode, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticket_no, idempotency_key) DO NOTHING
                """,
                ticketNo,
                customerId,
                normalizedKey,
                content,
                reason,
                returnMethod,
                pickupTimeWindow,
                updateMode,
                toTimestamp(now)
        );
        if (inserted == 0) {
            // Worker 重试命中同一幂等键时只返回已有结果，不重复追加正文或重复修改履约字段。
            String existingMode = jdbcTemplate.queryForObject(
                    "SELECT update_mode FROM ticket_supplement WHERE ticket_no = ? AND idempotency_key = ?",
                    String.class,
                    ticketNo,
                    normalizedKey
            );
            return new TicketSupplementResult(
                    detailForCustomer(ticketNo, customerId),
                    existingMode == null ? "APPLIED" : existingMode,
                    "APPLIED".equals(existingMode) && fulfillmentChangeRequested,
                    true
            );
        }

        if ("UNCHANGED".equals(updateMode)) {
            // 完全重复的业务信息只留下幂等审计，不改写工单正文和更新时间。
            return new TicketSupplementResult(ticket, updateMode, false, false);
        }

        String supplementSummary = buildSupplementSummary(
                contentIsNew ? content : null,
                reasonIsNew ? reason : null,
                returnMethodChanged ? returnMethod : null,
                pickupTimeChanged ? pickupTimeWindow : null,
                updateMode
        );
        String marker = "\n\n[客户补充 " + now.format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")) + "] ";
        String updatedContent = appendText(ticket.content(), marker + supplementSummary);
        String updatedAiSummary = appendText(ticket.aiSummary(), marker + supplementSummary);

        String updatedReturnMethod = ticket.returnMethod();
        String updatedPickupTimeWindow = ticket.pickupTimeWindow();
        String updatedPickupStatus = ticket.pickupStatus();
        if (fulfillmentUpdated) {
            if (returnMethod != null) {
                updatedReturnMethod = returnMethod;
            }
            if ("self_ship".equalsIgnoreCase(updatedReturnMethod)) {
                // 改为自行寄回后清除旧取件时间，避免坐席继续按已取消的上门偏好处理。
                updatedPickupTimeWindow = null;
                updatedPickupStatus = "NOT_REQUIRED";
            } else {
                if (pickupTimeWindow != null) {
                    updatedPickupTimeWindow = pickupTimeWindow;
                }
                updatedPickupStatus = "PREFERENCE_RECORDED";
            }
        }

        jdbcTemplate.update(
                """
                UPDATE support_ticket
                SET content = ?, ai_summary = ?, return_method = ?, pickup_time_window = ?,
                    pickup_status = ?, updated_at = ?
                WHERE ticket_no = ? AND customer_id = ?
                """,
                updatedContent,
                updatedAiSummary,
                updatedReturnMethod,
                updatedPickupTimeWindow,
                updatedPickupStatus,
                toTimestamp(now),
                ticketNo,
                customerId
        );
        return new TicketSupplementResult(
                detailForCustomer(ticketNo, customerId),
                updateMode,
                fulfillmentUpdated,
                false
        );
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
                    return_method VARCHAR(32),
                    pickup_time_window VARCHAR(128),
                    pickup_status VARCHAR(32),
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
                CREATE TABLE IF NOT EXISTS ticket_supplement (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_no VARCHAR(64) NOT NULL,
                    customer_id BIGINT NOT NULL,
                    idempotency_key VARCHAR(128) NOT NULL,
                    content CLOB,
                    after_sale_reason VARCHAR(500),
                    requested_return_method VARCHAR(32),
                    requested_pickup_time_window VARCHAR(128),
                    update_mode VARCHAR(32) NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    UNIQUE(ticket_no, idempotency_key)
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
                CREATE INDEX IF NOT EXISTS idx_ticket_supplement_ticket_created
                ON ticket_supplement(ticket_no, created_at)
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
        // 旧版 SQLite 数据库启动时补齐退货履约字段，避免要求开发者手工重建数据库。
        ensureColumn("return_method", "VARCHAR(32)");
        ensureColumn("pickup_time_window", "VARCHAR(128)");
        ensureColumn("pickup_status", "VARCHAR(32)");
    }

    private void insertDraft(SupportTicket ticket) {
        jdbcTemplate.update(connection -> {
            PreparedStatement ps = connection.prepareStatement(
                    """
                    INSERT INTO support_ticket (
                        ticket_no, title, ticket_type, priority, customer_id, order_no,
                        session_id, external_session_no, content, ai_summary,
                        return_method, pickup_time_window, pickup_status, assigned_group,
                        handler_id, assigned_by, assignment_reason, assignment_score, assigned_at,
                        status, sla_deadline, source, urge_count, last_urged_at, last_urge_reason, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ps.setString(11, ticket.returnMethod());
            ps.setString(12, ticket.pickupTimeWindow());
            ps.setString(13, ticket.pickupStatus());
            ps.setString(14, ticket.assignedGroup());
            ps.setObject(15, ticket.handlerId());
            ps.setString(16, ticket.assignedBy());
            ps.setString(17, ticket.assignmentReason());
            ps.setObject(18, ticket.assignmentScore());
            ps.setTimestamp(19, toTimestamp(ticket.assignedAt()));
            ps.setString(20, ticket.status());
            ps.setTimestamp(21, toTimestamp(ticket.slaDeadline()));
            ps.setString(22, ticket.source());
            ps.setObject(23, ticket.urgeCount());
            ps.setTimestamp(24, toTimestamp(ticket.lastUrgedAt()));
            ps.setString(25, ticket.lastUrgeReason());
            ps.setTimestamp(26, toTimestamp(ticket.createdAt()));
            ps.setTimestamp(27, toTimestamp(ticket.updatedAt()));
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
                    return_method = ?, pickup_time_window = ?, pickup_status = ?,
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
                updated.returnMethod(),
                updated.pickupTimeWindow(),
                updated.pickupStatus(),
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
                ticket.returnMethod(),
                ticket.pickupTimeWindow(),
                ticket.pickupStatus(),
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

    private static String cleanSupplementValue(String value, int maxLength) {
        if (value == null || value.isBlank()) {
            return null;
        }
        String cleaned = value.trim();
        return cleaned.length() <= maxLength ? cleaned : cleaned.substring(0, maxLength);
    }

    private static String appendText(String original, String addition) {
        return (original == null ? "" : original) + addition;
    }

    private static String buildSupplementSummary(
            String content,
            String reason,
            String returnMethod,
            String pickupTimeWindow,
            String updateMode
    ) {
        StringBuilder summary = new StringBuilder();
        if (reason != null) {
            summary.append("退货原因：").append(reason).append("；");
        }
        if (content != null) {
            summary.append("客户说明：").append(content).append("；");
        }
        if (returnMethod != null) {
            summary.append("申请退回方式：").append(returnMethod).append("；");
        }
        if (pickupTimeWindow != null) {
            summary.append("申请取件时间：").append(pickupTimeWindow).append("；");
        }
        if ("REVIEW_REQUIRED".equals(updateMode)) {
            summary.append("履约变更需人工确认，原安排暂不覆盖；");
        }
        return summary.toString();
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
