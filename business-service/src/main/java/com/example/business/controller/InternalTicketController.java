package com.example.business.controller;

import com.example.business.entity.SupportTicket;
import com.example.business.service.TicketAssignmentService;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

/**
 * Agent 内部工单接口，仅允许可信 Agent 通过共享密钥触发自动派单。
 */
@RestController
@RequestMapping("/api/internal/tickets")
public class InternalTicketController {
    private final TicketAssignmentService ticketAssignmentService;
    private final String agentInternalSecret;

    public InternalTicketController(
            TicketAssignmentService ticketAssignmentService,
            @Value("${agent.internal-secret:enterprise-customer-agent-demo-internal-secret}") String agentInternalSecret
    ) {
        this.ticketAssignmentService = ticketAssignmentService;
        this.agentInternalSecret = agentInternalSecret;
    }

    /**
     * Agent 建单后触发自动派单，避免客户 Token 直接调用坐席接口。
     *
     * @param ticketNo 工单编号
     * @param secret Agent 内部共享密钥
     * @return 自动派单后的工单
     */
    @PostMapping("/{ticketNo}/auto-assign")
    public SupportTicket autoAssign(
            @PathVariable("ticketNo") String ticketNo,
            @RequestHeader("X-Agent-Internal-Secret") String secret
    ) {
        if (!agentInternalSecret.equals(secret)) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "Agent 内部密钥无效");
        }
        return ticketAssignmentService.autoAssign(ticketNo);
    }
}
