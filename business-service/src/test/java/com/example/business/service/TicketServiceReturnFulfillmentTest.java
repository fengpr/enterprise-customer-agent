package com.example.business.service;

import com.example.business.dto.TicketSupplementRequest;
import com.example.business.dto.TicketSupplementResult;
import com.example.business.entity.SupportTicket;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.sqlite.SQLiteDataSource;
import org.springframework.jdbc.core.JdbcTemplate;

import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 验证退货履约字段可以随工单写入并完整读取，防止 Agent 已收集信息但业务库丢失。
 */
class TicketServiceReturnFulfillmentTest {

    @TempDir
    Path tempDir;

    /**
     * 创建退货工单后应持久化退回方式、取件时间偏好和取件状态。
     */
    @Test
    void shouldPersistReturnFulfillmentFields() {
        SQLiteDataSource dataSource = new SQLiteDataSource();
        dataSource.setUrl("jdbc:sqlite:" + tempDir.resolve("ticket-service-test.db"));
        TicketService service = new TicketService(new JdbcTemplate(dataSource), "org.sqlite.JDBC");
        SupportTicket created = service.createForCustomer(returnTicketRequest(), 1L);

        assertNotNull(created.ticketNo());
        assertEquals(1L, created.customerId());
        assertEquals("pickup", created.returnMethod());
        assertEquals("明天上午九点", created.pickupTimeWindow());
        assertEquals("PREFERENCE_RECORDED", created.pickupStatus());
    }

    /**
     * 早期工单允许更新取件偏好，同一幂等键重试时不得重复追加审计记录。
     */
    @Test
    void shouldUpdateEarlyFulfillmentAndDeduplicateSupplement() {
        SQLiteDataSource dataSource = new SQLiteDataSource();
        dataSource.setUrl("jdbc:sqlite:" + tempDir.resolve("ticket-supplement-early.db"));
        JdbcTemplate jdbcTemplate = new JdbcTemplate(dataSource);
        TicketService service = new TicketService(jdbcTemplate, "org.sqlite.JDBC");
        SupportTicket created = service.createForCustomer(returnTicketRequest(), 1L);
        TicketSupplementRequest request = new TicketSupplementRequest(
                "补充新的取件偏好",
                "商品存在质量问题",
                "pickup",
                "后天下午三点"
        );

        TicketSupplementResult first = service.appendSupplementForCustomer(
                created.ticketNo(), 1L, request, "supplement-early-1"
        );
        TicketSupplementResult repeated = service.appendSupplementForCustomer(
                created.ticketNo(), 1L, request, "supplement-early-1"
        );

        assertEquals("APPLIED", first.updateMode());
        assertTrue(first.fulfillmentUpdated());
        assertEquals("后天下午三点", first.ticket().pickupTimeWindow());
        assertTrue(first.ticket().content().contains("商品存在质量问题"));
        assertTrue(repeated.deduplicated());
        assertEquals(1, jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM ticket_supplement WHERE ticket_no = ?",
                Integer.class,
                created.ticketNo()
        ));
    }

    /**
     * 工单进入处理中后，取件变更只登记申请，不覆盖原取件时间；这也覆盖临近取件时的保守保护。
     */
    @Test
    void shouldRequireReviewAfterTicketStartsProcessing() {
        SQLiteDataSource dataSource = new SQLiteDataSource();
        dataSource.setUrl("jdbc:sqlite:" + tempDir.resolve("ticket-supplement-processing.db"));
        TicketService service = new TicketService(new JdbcTemplate(dataSource), "org.sqlite.JDBC");
        SupportTicket created = service.createForCustomer(returnTicketRequest(), 1L);
        service.assign(created.ticketNo(), 101L, "售后组");
        service.updateStatus(created.ticketNo(), "PROCESSING");

        TicketSupplementResult result = service.appendSupplementForCustomer(
                created.ticketNo(),
                1L,
                new TicketSupplementRequest("希望临时改时间", "补充商品问题", "pickup", "十分钟后"),
                "supplement-processing-1"
        );

        assertEquals("REVIEW_REQUIRED", result.updateMode());
        assertEquals(false, result.fulfillmentUpdated());
        assertEquals("明天上午九点", result.ticket().pickupTimeWindow());
        assertTrue(result.ticket().content().contains("履约变更需人工确认"));
    }

    /**
     * 已关闭工单不接受继续追加，客户需要重新发起受控售后流程。
     */
    @Test
    void shouldRejectSupplementForClosedTicket() {
        SQLiteDataSource dataSource = new SQLiteDataSource();
        dataSource.setUrl("jdbc:sqlite:" + tempDir.resolve("ticket-supplement-closed.db"));
        TicketService service = new TicketService(new JdbcTemplate(dataSource), "org.sqlite.JDBC");
        SupportTicket created = service.createForCustomer(returnTicketRequest(), 1L);
        service.assign(created.ticketNo(), 101L, "售后组");
        service.close(created.ticketNo());

        assertThrows(
                IllegalArgumentException.class,
                () -> service.appendSupplementForCustomer(
                        created.ticketNo(),
                        1L,
                        new TicketSupplementRequest("补充", "新原因", null, null),
                        "supplement-closed-1"
                )
        );
    }

    /**
     * 已关闭工单的取件改约必须创建独立变更申请，不能覆盖原取件时间或重新打开工单。
     */
    @Test
    void shouldCreateAuditableChangeRequestForClosedTicketPickupChange() {
        SQLiteDataSource dataSource = new SQLiteDataSource();
        dataSource.setUrl("jdbc:sqlite:" + tempDir.resolve("ticket-change-request-closed.db"));
        JdbcTemplate jdbcTemplate = new JdbcTemplate(dataSource);
        TicketService service = new TicketService(jdbcTemplate, "org.sqlite.JDBC");
        SupportTicket created = service.createForCustomer(returnTicketRequest(), 1L);
        service.assign(created.ticketNo(), 101L, "售后组");
        service.close(created.ticketNo());

        TicketSupplementResult result = service.appendSupplementForCustomer(
                created.ticketNo(),
                1L,
                new TicketSupplementRequest(null, null, "pickup", "明天下午三点"),
                "closed-pickup-change-1"
        );
        TicketSupplementResult repeated = service.appendSupplementForCustomer(
                created.ticketNo(),
                1L,
                new TicketSupplementRequest(null, null, "pickup", "明天下午三点"),
                "closed-pickup-change-1"
        );

        assertEquals("REVIEW_REQUIRED", result.updateMode());
        assertEquals(false, result.fulfillmentUpdated());
        assertEquals("CLOSED", result.ticket().status());
        assertEquals("明天上午九点", result.ticket().pickupTimeWindow());
        assertNotNull(result.changeRequest());
        assertEquals("PENDING_REVIEW", result.changeRequest().status());
        assertEquals("明天下午三点", result.changeRequest().requestedPickupTimeWindow());
        assertTrue(repeated.deduplicated());
        assertEquals(result.changeRequest().requestNo(), repeated.changeRequest().requestNo());
        assertEquals(1, jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM ticket_change_request WHERE parent_ticket_no = ?",
                Integer.class,
                created.ticketNo()
        ));
    }

    /** 审核同意后才恢复已关闭工单并写入新取件偏好，客户补充本身不能直接改写原单。 */
    @Test
    void shouldApproveChangeRequestAndReopenClosedTicket() {
        SQLiteDataSource dataSource = new SQLiteDataSource();
        dataSource.setUrl("jdbc:sqlite:" + tempDir.resolve("ticket-change-request-approve.db"));
        TicketService service = new TicketService(new JdbcTemplate(dataSource), "org.sqlite.JDBC");
        SupportTicket created = service.createForCustomer(returnTicketRequest(), 1L);
        service.assign(created.ticketNo(), 101L, "售后组");
        service.close(created.ticketNo());
        TicketSupplementResult submitted = service.appendSupplementForCustomer(
                created.ticketNo(),
                1L,
                new TicketSupplementRequest(null, null, "pickup", "后天上午十点"),
                "closed-pickup-change-approve-1"
        );

        var approved = service.decideChangeRequest(
                created.ticketNo(),
                submitted.changeRequest().requestNo(),
                101L,
                "APPROVE",
                "已为您登记新的上门取件时间偏好，后续请以承运方确认结果为准。"
        );
        SupportTicket updated = service.detail(created.ticketNo());

        assertEquals("APPROVED", approved.status());
        assertEquals(101L, approved.reviewedBy());
        assertEquals("后天上午十点", updated.pickupTimeWindow());
        assertEquals("REOPENED", updated.status());
    }

    /** 拒绝申请不能暗中改变原工单的取件时间或状态。 */
    @Test
    void shouldRejectChangeRequestWithoutMutatingParentTicket() {
        SQLiteDataSource dataSource = new SQLiteDataSource();
        dataSource.setUrl("jdbc:sqlite:" + tempDir.resolve("ticket-change-request-reject.db"));
        TicketService service = new TicketService(new JdbcTemplate(dataSource), "org.sqlite.JDBC");
        SupportTicket created = service.createForCustomer(returnTicketRequest(), 1L);
        service.assign(created.ticketNo(), 101L, "售后组");
        service.close(created.ticketNo());
        TicketSupplementResult submitted = service.appendSupplementForCustomer(
                created.ticketNo(),
                1L,
                new TicketSupplementRequest(null, null, "pickup", "十分钟后"),
                "closed-pickup-change-reject-1"
        );

        var rejected = service.decideChangeRequest(
                created.ticketNo(),
                submitted.changeRequest().requestNo(),
                101L,
                "REJECT",
                "当前安排暂无法调整，如仍需协助请联系人工客服。"
        );
        SupportTicket unchanged = service.detail(created.ticketNo());

        assertEquals("REJECTED", rejected.status());
        assertEquals("CLOSED", unchanged.status());
        assertEquals("明天上午九点", unchanged.pickupTimeWindow());
    }

    private static SupportTicket returnTicketRequest() {
        return new SupportTicket(
                null,
                null,
                "退货申请",
                "refund",
                "medium",
                999L,
                "EC202606220001",
                null,
                "S-DYNAMIC-RETURN",
                "业务动作：return_goods；after_sale_reason: 商品有问题",
                "客户申请退货",
                "pickup",
                "明天上午九点",
                "PREFERENCE_RECORDED",
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                "agent",
                null,
                null,
                null,
                null,
                null
        );
    }
}
