package com.example.business.controller;

import com.example.business.dto.TicketAssignRequest;
import com.example.business.dto.TicketCloseRequest;
import com.example.business.dto.TicketStatusUpdateRequest;
import com.example.business.dto.TicketTransferRequest;
import com.example.business.dto.CurrentUser;
import com.example.business.entity.SupportTicket;
import com.example.business.entity.TicketStatus;
import com.example.business.service.AuthService;
import com.example.business.service.TicketAssignmentService;
import com.example.business.service.TicketService;
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
    private final TicketAssignmentService ticketAssignmentService;
    private final AuthService authService;

    public StaffTicketController(
            TicketService ticketService,
            TicketAssignmentService ticketAssignmentService,
            AuthService authService
    ) {
        this.ticketService = ticketService;
        this.ticketAssignmentService = ticketAssignmentService;
        this.authService = authService;
    }

    /**
     * 查询坐席可处理的工单列表，默认返回全部工单，可通过 status 逗号分隔筛选。
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
        CurrentUser user = authService.requireStaffOrDispatcher(authorization);
        Set<String> statuses = parseStatuses(status);
        if ("dispatcher".equals(user.role())) {
            return ticketService.listByStatuses(statuses);
        }
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
        CurrentUser user = authService.requireStaffOrDispatcher(authorization);
        SupportTicket ticket = ticketService.detail(ticketNo);
        assertTicketVisibleToUser(ticket, user);
        return ticket;
    }

    /**
     * 坐席领取或分派工单，状态会进入待处理。
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
        CurrentUser user = authService.requireStaffOrDispatcher(authorization);
        if ("staff".equals(user.role())) {
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
        return ticketService.assign(ticketNo, request.handlerId(), request.assignedGroup());
    }

    /**
     * 坐席触发智能派单，由 Java 规则服务完成最终处理人选择和落库。
     *
     * @param ticketNo 工单编号
     * @param authorization Authorization 请求头
     * @return 自动派单后的工单
     */
    @PostMapping("/{ticketNo}/auto-assign")
    public SupportTicket autoAssign(
            @PathVariable("ticketNo") String ticketNo,
            @RequestHeader("Authorization") String authorization
    ) {
        authService.requireDispatcher(authorization);
        return ticketAssignmentService.autoAssign(ticketNo);
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
        return ticketService.updateStatus(ticketNo, request.status());
    }

    /**
     * 坐席转派工单给其他处理组或处理人。
     *
     * @param ticketNo 工单编号
     * @param request 转派请求
     * @param authorization Authorization 请求头
     * @return 转派后的工单
     */
    @PostMapping("/{ticketNo}/transfer")
    public SupportTicket transfer(
            @PathVariable("ticketNo") String ticketNo,
            @RequestBody TicketTransferRequest request,
            @RequestHeader("Authorization") String authorization
    ) {
        authService.requireDispatcher(authorization);
        return ticketService.transfer(ticketNo, request.assignedGroup(), request.handlerId());
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
        return ticketService.close(ticketNo);
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
        if ("dispatcher".equals(user.role())) {
            return;
        }
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
