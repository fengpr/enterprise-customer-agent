package com.example.business.service;

import com.example.business.dto.StaffMemberStatus;
import com.example.business.entity.Employee;
import com.example.business.entity.StaffMember;
import com.example.business.entity.SupportTicket;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Objects;
import java.util.Optional;

/**
 * 坐席资源池服务，从统一 employee 表读取坐席员工并叠加工单负载数据。
 */
@Service
public class StaffDirectoryService {
    private final TicketService ticketService;
    private final EmployeeService employeeService;

    public StaffDirectoryService(TicketService ticketService, EmployeeService employeeService) {
        this.ticketService = ticketService;
        this.employeeService = employeeService;
    }

    /**
     * 查询所有启用坐席，调度角色不会进入派单候选池。
     *
     * @return 坐席资源列表
     */
    public List<StaffMember> listAll() {
        return employeeService.listEnabledStaff().stream()
                .map(this::toStaffMember)
                .toList();
    }

    /**
     * 按用户 ID 查找启用坐席。
     *
     * @param userId 坐席用户 ID
     * @return 坐席信息
     */
    public Optional<StaffMember> findByUserId(Long userId) {
        return listAll().stream()
                .filter(staff -> Objects.equals(staff.userId(), userId))
                .findFirst();
    }

    /**
     * 查询坐席运行状态，包含负载、当前工作内容和 Demo 客户反馈摘要。
     *
     * @return 坐席状态列表
     */
    public List<StaffMemberStatus> listStatuses() {
        return employeeService.listEnabledStaff().stream()
                .map(this::toStatus)
                .toList();
    }

    /**
     * 更新坐席可用状态并返回最新状态。
     *
     * @param userId 坐席 ID
     * @param online 在线状态
     * @param acceptingTickets 接单状态
     * @param maxActiveTickets 最大并发量
     * @return 更新后的坐席状态
     */
    public StaffMemberStatus updateAvailability(
            Long userId,
            Boolean online,
            Boolean acceptingTickets,
            Integer maxActiveTickets
    ) {
        Employee employee = employeeService.updateStaffAvailability(userId, online, acceptingTickets, maxActiveTickets);
        return toStatus(employee);
    }

    private StaffMember toStaffMember(Employee employee) {
        return new StaffMember(
                employee.userId(),
                employee.displayName(),
                employee.role(),
                employee.groupName(),
                employee.skills(),
                employee.online(),
                employee.maxActiveTickets(),
                employee.acceptingTickets()
        );
    }

    private StaffMemberStatus toStatus(Employee employee) {
        List<SupportTicket> activeTickets = ticketService.listActiveTicketsByHandler(employee.userId());
        List<String> currentWork = activeTickets.stream()
                .limit(5)
                .map(ticket -> ticket.ticketNo() + " - " + safeText(ticket.title(), ticket.content()))
                .toList();
        return new StaffMemberStatus(
                employee.userId(),
                employee.displayName(),
                employee.role(),
                employee.groupName(),
                employee.skills(),
                employee.online(),
                employee.maxActiveTickets(),
                employee.acceptingTickets(),
                activeTickets.size(),
                currentWork,
                demoFeedback(employee.userId())
        );
    }

    private List<String> demoFeedback(Long userId) {
        if (Objects.equals(10002L, userId)) {
            return List.of("售后解释清楚，响应及时", "换货问题处理较专业");
        }
        if (Objects.equals(10003L, userId)) {
            return List.of("投诉跟进较快", "复杂问题需要补充凭证");
        }
        return List.of("沟通态度稳定", "普通咨询处理效率较高");
    }

    private String safeText(String title, String content) {
        String value = title == null || title.isBlank() ? content : title;
        if (value == null || value.isBlank()) {
            return "暂无标题";
        }
        return value.length() > 36 ? value.substring(0, 36) + "..." : value;
    }
}
