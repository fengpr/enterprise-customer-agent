package com.example.business.dto;

/**
 * 工单分派请求，承载主管或系统自动分派时指定的处理组和处理人。
 */
public record TicketAssignRequest(
        Long handlerId,
        String assignedGroup
) {
}
