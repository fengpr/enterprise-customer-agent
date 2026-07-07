package com.example.business.dto;

/**
 * 工单通用状态更新请求，用于客服工作台推进处理中等非高风险状态。
 */
public record TicketStatusUpdateRequest(
        String status,
        Long operatorId,
        String reason
) {
}
