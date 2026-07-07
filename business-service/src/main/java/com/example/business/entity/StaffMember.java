package com.example.business.entity;

import java.util.Set;

/**
 * Demo 坐席资源实体，用于自动派单时描述坐席技能、在线状态和最大并发量。
 */
public record StaffMember(
        Long userId,
        String displayName,
        String role,
        String groupName,
        Set<String> skills,
        boolean online,
        int maxActiveTickets,
        boolean acceptingTickets
) {
}
