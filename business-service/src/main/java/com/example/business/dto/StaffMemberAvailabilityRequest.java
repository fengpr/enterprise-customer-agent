package com.example.business.dto;

/**
 * 坐席可用状态配置请求，供调度角色调整在线、接单和最大并发量。
 */
public record StaffMemberAvailabilityRequest(
        Boolean online,
        Boolean acceptingTickets,
        Integer maxActiveTickets
) {
}
