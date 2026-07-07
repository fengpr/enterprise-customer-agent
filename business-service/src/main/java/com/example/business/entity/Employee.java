package com.example.business.entity;

import java.time.LocalDateTime;
import java.util.Set;

/**
 * 内部员工实体，统一承载坐席和调度等员工角色及派单相关配置。
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
