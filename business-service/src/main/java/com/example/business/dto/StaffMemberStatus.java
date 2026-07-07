package com.example.business.dto;

import java.util.List;
import java.util.Set;

/**
 * 坐席资源状态响应，供调度台查看坐席技能、负载、当前工作内容和客户反馈摘要。
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
