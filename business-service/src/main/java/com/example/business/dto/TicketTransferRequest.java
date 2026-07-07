package com.example.business.dto;

/**
 * 工单转派请求，承载工单跨客服组或更换处理人的目标信息。
 */
public record TicketTransferRequest(
        String assignedGroup,
        Long handlerId,
        String reason
) {
}
