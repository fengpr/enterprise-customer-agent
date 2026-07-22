package com.example.business.dto;

import java.util.List;
import java.util.Set;

/**
 * 坐席资源状态响应，仅供系统内部判断人工接管和自动派单可用性。
 */
public record StaffMemberStatus(
        Long userId,
        String displayName,
        String role,
        String groupName,
        Set<String> skills,
        boolean online,
        int maxActiveTickets,
        boolean acceptingTickets,
        int activeTickets,
        List<String> currentWork,
        List<String> recentFeedback
) {
}
