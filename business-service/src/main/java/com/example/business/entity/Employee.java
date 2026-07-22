package com.example.business.entity;

import java.time.LocalDateTime;
import java.util.Set;

/**
 * 内部员工实体，承载客服坐席账号及系统自动派单所需配置。
 */
public record Employee(
        Long userId,
        String username,
        String password,
        String displayName,
        String role,
        String groupName,
        Set<String> skills,
        boolean online,
        boolean acceptingTickets,
        int maxActiveTickets,
        boolean enabled,
        LocalDateTime createdAt,
        LocalDateTime updatedAt
) {
}
