package com.example.business.controller;

import com.example.business.dto.TicketAssignRequest;
import com.example.business.dto.TicketCloseRequest;
import com.example.business.dto.TicketStatusUpdateRequest;
import com.example.business.dto.TicketChangeDecisionRequest;
import com.example.business.dto.CurrentUser;
import com.example.business.entity.SupportTicket;
import com.example.business.entity.TicketStatus;
import com.example.business.entity.TicketChangeRequest;
import com.example.business.service.AuthService;
import com.example.business.service.TicketService;
import com.example.business.service.TicketConversationNotifier;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * 客服坐席工单控制器，提供内部工作台查看待处理工单和推进状态的受控入口。
 */
@RestController
@RequestMapping("/api/staff/tickets")
public class StaffTicketController {
    private final TicketService ticketService;
    private final AuthService authService;
    private final TicketConversationNotifier ticketConversationNotifier;

    public StaffTicketController(
            TicketService ticketService,
            AuthService authService,
            TicketConversationNotifier ticketConversationNotifier
    ) {
        this.ticketService = ticketService;
        this.authService = authService;
        this.ticketConversationNotifier = ticketConversationNotifier;
    }

    /**
     * 查询当前坐席可领取或本人正在处理的工单，可通过 status 逗号分隔筛选。
     *
     * @param status 工单状态筛选，示例：PENDING_ASSIGN,PENDING_PROCESS
     * @param authorization Authorization 请求头
     * @return 坐席可见工单列表
     */
    @GetMapping
    public List<SupportTicket> list(
            @RequestParam(value = "status", required = false) String status,
            @RequestHeader("Authorization") String authorization
    ) {
        CurrentUser user = authService.requireStaff(authorization);
        Set<String> statuses = parseStatuses(status);
        return ticketService.listVisibleForStaff(user.userId(), statuses);
    }

    /**
     * 查询坐席视角的工单详情，用于工作台展示客户问题、AI 摘要和订单关联信息。
     *
     * @param ticketNo 工单编号
     * @param authorization Authorization 请求头
     * @return 工单详情
     */
    @GetMapping("/{ticketNo}")
    public SupportTicket detail(
            @PathVariable("ticketNo") String ticketNo,
            @RequestHeader("Authorization") String authorization
    ) {
        CurrentUser user = authService.requireStaff(authorization);
        SupportTicket ticket = ticketService.detail(ticketNo);
        assertTicketVisibleToUser(ticket, user);
        return ticket;
    }

    /**
     * 坐席只能领取公共待分派工单，状态会进入待处理。
     *
     * @param ticketNo 工单编号
     * @param request 分派请求
     * @param authorization Authorization 请求头
     * @return 分派后的工单
     */
    @PostMapping("/{ticketNo}/assign")
    public SupportTicket assign(
            @PathVariable("ticketNo") String ticketNo,
            @RequestBody TicketAssignRequest request,
            @RequestHeader("Authorization") String authorization
    ) {
        CurrentUser user = authService.requireStaff(authorization);
        SupportTicket ticket = ticketService.detail(ticketNo);
        if (ticket.handlerId() != null || !TicketStatus.PENDING_ASSIGN.name().equals(ticket.status())) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "坐席只能领取未分配工单");
        }
        Long targetHandlerId = request.handlerId() == null ? user.userId() : request.handlerId();
        if (!user.userId().equals(targetHandlerId)) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "坐席不能把工单分派给其他人");
        }
        return ticketService.assign(ticketNo, user.userId(), request.assignedGroup());
    }

    /**
     * 坐席推进普通处理状态，例如从待处理进入处理中。
     *
     * @param ticketNo 工单编号
     * @param request 状态更新请求
     * @param authorization Authorization 请求头
     * @return 更新后的工单
     */
    @PostMapping("/{ticketNo}/status")
    public SupportTicket updateStatus(
            @PathVariable("ticketNo") String ticketNo,
            @RequestBody TicketStatusUpdateRequest request,
            @RequestHeader("Authorization") String authorization
    ) {
        CurrentUser user = authService.requireStaff(authorization);
        assertTicketOwnedByStaff(ticketService.detail(ticketNo), user);
        SupportTicket updated = ticketService.updateStatus(ticketNo, request.status());
        ticketConversationNotifier.notifyStatusChanged(updated);
        return updated;
    }

    /**
     * 坐席关闭已处理完成的工单，客户侧随后可刷新看到 CLOSED 状态。
     *
     * @param ticketNo 工单编号
     * @param request 关闭请求
     * @param authorization Authorization 请求头
     * @return 关闭后的工单
     */
    @PostMapping("/{ticketNo}/close")
    public SupportTicket close(
            @PathVariable("ticketNo") String ticketNo,
            @RequestBody TicketCloseRequest request,
            @RequestHeader("Authorization") String authorization
    ) {
        CurrentUser user = authService.requireStaff(authorization);
        assertTicketOwnedByStaff(ticketService.detail(ticketNo), user);
        SupportTicket closed = ticketService.close(ticketNo);
        ticketConversationNotifier.notifyStatusChanged(closed);
        return closed;
    }

    /** 查询本人负责工单下待审核或历史履约变更申请。 */
    @GetMapping("/{ticketNo}/change-requests")
    public List<TicketChangeRequest> changeRequests(
            @PathVariable("ticketNo") String ticketNo,
            @RequestHeader("Authorization") String authorization
    ) {
        CurrentUser user = authService.requireStaff(authorization);
        assertTicketOwnedByStaff(ticketService.detail(ticketNo), user);
        return ticketService.listChangeRequests(ticketNo);
    }

    /**
     * 座席审核履约变更申请。
     * <p>审核同意才会恢复原工单并应用新的取件偏好，不能由页面直接修改原工单字段。</p>
     */
    @PostMapping("/{ticketNo}/change-requests/{requestNo}/decision")
    public TicketChangeRequest decideChangeRequest(
            @PathVariable("ticketNo") String ticketNo,
            @PathVariable("requestNo") String requestNo,
            @RequestBody TicketChangeDecisionRequest request,
            @RequestHeader("Authorization") String authorization
    ) {
        CurrentUser user = authService.requireStaff(authorization);
        assertTicketOwnedByStaff(ticketService.detail(ticketNo), user);
        if (request == null || request.decision() == null || request.decision().isBlank()) {
            throw new IllegalArgumentException("请选择履约变更审核结果");
        }
        return ticketService.decideChangeRequest(
                ticketNo,
                requestNo,
                user.userId(),
                request.decision(),
                request.customerMessage()
        );
    }

    /**
     * 将业务校验错误转换为坐席端可读响应。
     *
     * @param ex 业务校验异常
     * @return 标准错误响应
     */
    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<Map<String, String>> handleIllegalArgument(IllegalArgumentException ex) {
        return ResponseEntity.badRequest().body(Map.of(
                "status", "failed",
                "error_message", ex.getMessage()
        ));
    }

    private Set<String> parseStatuses(String status) {
        if (status == null || status.isBlank()) {
            return Set.of();
        }
        // 坐席端支持逗号分隔状态筛选，便于一个队列展示多个待处理状态。
        return Arrays.stream(status.split(","))
                .map(String::trim)
                .filter(item -> !item.isBlank())
                .map(String::toUpperCase)
                .collect(Collectors.toSet());
    }

    private void assertTicketVisibleToUser(SupportTicket ticket, CurrentUser user) {
        if (user.userId().equals(ticket.handlerId())
                || ticket.handlerId() == null
                || TicketStatus.PENDING_ASSIGN.name().equals(ticket.status())) {
            return;
        }
        throw new ResponseStatusException(HttpStatus.FORBIDDEN, "无权访问该工单");
    }

    private void assertTicketOwnedByStaff(SupportTicket ticket, CurrentUser user) {
        if (!user.userId().equals(ticket.handlerId())) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "坐席只能处理自己名下的工单");
        }
    }
}
