package com.example.business.entity;

import java.time.LocalDateTime;

/**
 * 工单实体，记录用户问题、AI 摘要、处理人、状态和 SLA 等流转信息。
 */
public record SupportTicket(
        Long id,
        String ticketNo,
        String title,
        String ticketType,
        String priority,
        Long customerId,
        String orderNo,
        Long sessionId,
        String externalSessionNo,
        String content,
        String aiSummary,
        String returnMethod,
        String pickupTimeWindow,
        String pickupStatus,
        String assignedGroup,
        Long handlerId,
        String assignedBy,
        String assignmentReason,
        Integer assignmentScore,
        LocalDateTime assignedAt,
        String status,
        LocalDateTime slaDeadline,
        String source,
        Integer urgeCount,
        LocalDateTime lastUrgedAt,
        String lastUrgeReason,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
}
