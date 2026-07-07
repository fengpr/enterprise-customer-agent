package com.example.business.controller;

import com.example.business.dto.StaffMemberStatus;
import com.example.business.service.StaffDirectoryService;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import java.util.List;
import java.util.Map;

/**
 * Agent 内部坐席资源接口，仅返回人工接管决策所需的聚合坐席状态。
 */
@RestController
@RequestMapping("/api/internal/staff")
public class InternalStaffController {
    private final StaffDirectoryService staffDirectoryService;
    private final String agentInternalSecret;

    public InternalStaffController(
            StaffDirectoryService staffDirectoryService,
            @Value("${agent.internal-secret:enterprise-customer-agent-demo-internal-secret}") String agentInternalSecret
    ) {
        this.staffDirectoryService = staffDirectoryService;
        this.agentInternalSecret = agentInternalSecret;
    }

    /**
     * 查询当前坐席可用性，供 Agent 判断转人工是立即等待接入还是进入排队。
     *
     * @param secret Agent 内部共享密钥
     * @return 坐席状态摘要
     */
    @GetMapping("/availability")
    public Map<String, Object> availability(@RequestHeader("X-Agent-Internal-Secret") String secret) {
        if (!agentInternalSecret.equals(secret)) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "Agent 内部密钥无效");
        }
        List<StaffMemberStatus> members = staffDirectoryService.listStatuses();
        long availableCount = members.stream()
                // 只统计在线、可接单且当前业务负载未满的坐席，避免误导客户已接通。
                .filter(StaffMemberStatus::online)
                .filter(StaffMemberStatus::acceptingTickets)
                .filter(member -> member.activeTickets() < member.maxActiveTickets())
                .count();
        return Map.of(
                "members", members,
                "staff_count", members.size(),
                "available_staff_count", availableCount
        );
    }
}
