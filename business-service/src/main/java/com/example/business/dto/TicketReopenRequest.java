package com.example.business.dto;

/**
 * 工单重开请求，记录重开操作者和重开原因，避免已关闭问题被静默恢复。
 */
public record TicketReopenRequest(
        Long operatorId,
        String reopenReason
) {
}
