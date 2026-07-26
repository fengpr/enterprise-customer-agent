package com.example.business.entity;

import java.time.LocalDateTime;

/**
 * 工单履约变更申请。
 *
 * <p>已进入处理、取件已锁定或已经关闭的售后工单，不能由客户消息直接覆盖原取件安排。
 * 此实体作为原工单的受审计子申请，等待坐席或后续履约系统明确处理。</p>
 */
public record TicketChangeRequest(
        Long id,
        String requestNo,
        String parentTicketNo,
        Long customerId,
        String externalSessionNo,
        String changeType,
        String previousReturnMethod,
        String previousPickupTimeWindow,
        String requestedReturnMethod,
        String requestedPickupTimeWindow,
        String parentTicketStatus,
        String status,
        String idempotencyKey,
        Long reviewedBy,
        String customerMessage,
        LocalDateTime reviewedAt,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
}
