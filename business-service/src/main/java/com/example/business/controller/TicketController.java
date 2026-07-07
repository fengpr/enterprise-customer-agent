package com.example.business.controller;

import com.example.business.entity.SupportTicket;
import com.example.business.dto.TicketUrgeRequest;
import com.example.business.service.AuthService;
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
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

/**
 * 客户侧工单接口控制器，只承接客户查工单和 Agent 代客户建单。
 */
@RestController
@RequestMapping("/api/tickets")
public class TicketController {
    private final TicketService ticketService;
    private final AuthService authService;

    public TicketController(TicketService ticketService, AuthService authService) {
        this.ticketService = ticketService;
        this.authService = authService;
    }

    /**
     * 查询当前登录客户的工单列表。
     *
     * @param authorization Authorization 请求头
     * @return 当前客户的工单记录
     */
    @GetMapping
    public List<SupportTicket> list(@RequestHeader("Authorization") String authorization) {
        Long customerId = authService.currentCustomerId(authorization);
        return ticketService.listByCustomerId(customerId);
    }

    /**
     * 创建客服工单，由服务层统一补充工单号、默认状态和 SLA 截止时间。
     *
     * @param ticket 前端或 Agent 提交的工单草稿
     * @param authorization Authorization 请求头
     * @return 已生成系统字段的工单记录
     */
    @PostMapping
    public SupportTicket create(
            @RequestBody SupportTicket ticket,
            @RequestHeader("Authorization") String authorization
    ) {
        Long customerId = authService.currentCustomerId(authorization);
        return ticketService.createForCustomer(ticket, customerId);
    }

    /**
     * 查询当前登录客户的单个工单详情。
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
        Long customerId = authService.currentCustomerId(authorization);
        return ticketService.detailForCustomer(ticketNo, customerId);
    }

    /**
     * 客户催办自己的工单，催办记录会进入坐席和调度可见的工单信息。
     *
     * @param ticketNo 工单编号
     * @param request 催办原因
     * @param authorization Authorization 请求头
     * @return 更新后的工单详情
     */
    @PostMapping("/{ticketNo}/urge")
    public SupportTicket urge(
            @PathVariable("ticketNo") String ticketNo,
            @RequestBody(required = false) TicketUrgeRequest request,
            @RequestHeader("Authorization") String authorization
    ) {
        Long customerId = authService.currentCustomerId(authorization);
        String reason = request == null ? null : request.reason();
        return ticketService.urgeForCustomer(ticketNo, customerId, reason);
    }

    /**
     * 旧分派接口已废弃，派单必须走 /api/staff/tickets 或 /api/internal/tickets。
     *
     * @param ticketNo 工单编号
     * @return 410 Gone 废弃提示
     */
    @PostMapping("/{ticketNo}/assign")
    public ResponseEntity<Map<String, String>> assign(@PathVariable("ticketNo") String ticketNo) {
        return deprecatedWriteEndpoint();
    }

    /**
     * 旧转派接口已废弃，转派必须走调度受控接口。
     *
     * @param ticketNo 工单编号
     * @return 410 Gone 废弃提示
     */
    @PostMapping("/{ticketNo}/transfer")
    public ResponseEntity<Map<String, String>> transfer(@PathVariable("ticketNo") String ticketNo) {
        return deprecatedWriteEndpoint();
    }

    /**
     * 旧状态更新接口已废弃，坐席处理必须走 /api/staff/tickets。
     *
     * @param ticketNo 工单编号
     * @return 410 Gone 废弃提示
     */
    @PostMapping("/{ticketNo}/status")
    public ResponseEntity<Map<String, String>> updateStatus(@PathVariable("ticketNo") String ticketNo) {
        return deprecatedWriteEndpoint();
    }

    /**
     * 旧关闭接口已废弃，坐席关闭必须走 /api/staff/tickets。
     *
     * @param ticketNo 工单编号
     * @return 410 Gone 废弃提示
     */
    @PostMapping("/{ticketNo}/close")
    public ResponseEntity<Map<String, String>> close(@PathVariable("ticketNo") String ticketNo) {
        return deprecatedWriteEndpoint();
    }

    /**
     * 旧重开接口已废弃，后续重开应进入受控内部工作台接口。
     *
     * @param ticketNo 工单编号
     * @return 410 Gone 废弃提示
     */
    @PostMapping("/{ticketNo}/reopen")
    public ResponseEntity<Map<String, String>> reopen(@PathVariable("ticketNo") String ticketNo) {
        return deprecatedWriteEndpoint();
    }

    /**
     * 将业务校验错误转换为可读响应，避免状态流转失败时直接暴露 500。
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

    private ResponseEntity<Map<String, String>> deprecatedWriteEndpoint() {
        return ResponseEntity.status(HttpStatus.GONE).body(Map.of(
                "status", "failed",
                "error_message", "该工单写接口已废弃，请使用 /api/staff/tickets 或 /api/internal/tickets"
        ));
    }
}
