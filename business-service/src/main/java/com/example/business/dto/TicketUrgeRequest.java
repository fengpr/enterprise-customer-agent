package com.example.business.dto;

/**
 * 客户催办工单请求，记录客户希望加快处理的原因或补充说明。
 */
public record TicketUrgeRequest(
        String reason
) {
}
