package com.example.business.service;

import com.example.business.entity.SupportTicket;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.sqlite.SQLiteDataSource;
import org.springframework.jdbc.core.JdbcTemplate;

import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * 验证人工会话按需建单的幂等与客户隔离规则。
 */
class TicketServiceHandoffTest {

    @TempDir
    Path tempDir;

    /**
     * 同一人工会话重复建单必须返回同一未关闭工单，且其他客户不能复用该会话键。
     */
    @Test
    void shouldCreateOneOpenTicketPerHandoffSession() {
        SQLiteDataSource dataSource = new SQLiteDataSource();
        dataSource.setUrl("jdbc:sqlite:" + tempDir.resolve("handoff-ticket.db"));
        TicketService service = new TicketService(new JdbcTemplate(dataSource), "org.sqlite.JDBC");

        SupportTicket first = service.createHandoffFollowUp(1L, "S100", "异步跟进", "客户问题", "high");
        SupportTicket repeated = service.createHandoffFollowUp(1L, "S100", "重复请求", "重复内容", "low");

        assertEquals(first.ticketNo(), repeated.ticketNo());
        assertEquals("S100", repeated.externalSessionNo());
        assertEquals("HUMAN_HANDOFF", repeated.source());
        assertThrows(
                IllegalArgumentException.class,
                () -> service.createHandoffFollowUp(2L, "S100", "越权", "越权", "medium")
        );
    }
}
