package com.example.business.controller;

import com.example.business.dto.StaffMemberAvailabilityRequest;
import com.example.business.dto.StaffMemberStatus;
import com.example.business.service.AuthService;
import com.example.business.service.StaffDirectoryService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

/**
 * 坐席资源控制器，供调度台查看坐席状态、负载和当前工作内容。
 */
@RestController
@RequestMapping("/api/staff/members")
public class StaffMemberController {
    private final StaffDirectoryService staffDirectoryService;
    private final AuthService authService;

    public StaffMemberController(StaffDirectoryService staffDirectoryService, AuthService authService) {
        this.staffDirectoryService = staffDirectoryService;
        this.authService = authService;
    }

    /**
     * 查询坐席状态列表，只有调度角色可查看全员工作状态和统计。
     *
     * @param authorization Authorization 请求头
     * @return 坐席状态列表
     */
    @GetMapping
    public List<StaffMemberStatus> list(@RequestHeader("Authorization") String authorization) {
        authService.requireDispatcher(authorization);
        return staffDirectoryService.listStatuses();
    }

    /**
     * 更新坐席在线、接单和最大并发配置，只有调度角色可以操作。
     *
     * @param userId 坐席用户 ID
     * @param request 可用状态配置
     * @param authorization Authorization 请求头
     * @return 更新后的坐席状态
     */
    @PatchMapping("/{userId}/availability")
    public StaffMemberStatus updateAvailability(
            @PathVariable("userId") Long userId,
            @RequestBody StaffMemberAvailabilityRequest request,
            @RequestHeader("Authorization") String authorization
    ) {
        authService.requireDispatcher(authorization);
        return staffDirectoryService.updateAvailability(
                userId,
                request.online(),
                request.acceptingTickets(),
                request.maxActiveTickets()
        );
    }

    /**
     * 将坐席配置校验错误转换为调度台可读响应。
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
}
