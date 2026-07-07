package com.example.business.dto;

/**
 * 工单关闭请求，记录关闭操作者和关闭原因，便于后续审计与复盘。
 */
public record TicketCloseRequest(
        Long operatorId,
        String closeReason
) {
}
