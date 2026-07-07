package com.example.business.entity;

/**
 * 工单状态枚举，统一约束客服工单从创建、分派、处理中到关闭、重开的生命周期。
 */
public enum TicketStatus {
    PENDING_ASSIGN,
    PENDING_PROCESS,
    PROCESSING,
    TRANSFERRED,
    CLOSED,
    REOPENED
}
