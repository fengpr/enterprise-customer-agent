package com.example.business.service;

import com.example.business.entity.StaffMember;
import com.example.business.entity.SupportTicket;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.util.Comparator;
import java.util.List;
import java.util.Objects;

/**
 * 工单自动派单服务，使用可解释的确定性规则完成坐席推荐和落库。
 */
@Service
public class TicketAssignmentService {
    private final TicketService ticketService;
    private final StaffDirectoryService staffDirectoryService;

    public TicketAssignmentService(TicketService ticketService, StaffDirectoryService staffDirectoryService) {
        this.ticketService = ticketService;
        this.staffDirectoryService = staffDirectoryService;
    }

    /**
     * 自动派单入口，按熟客优先、技能匹配、最少负载和优先级计算推荐坐席。
     *
     * @param ticketNo 工单编号
     * @return 自动派单后的工单
     */
    public SupportTicket autoAssign(String ticketNo) {
        SupportTicket ticket = ticketService.detail(ticketNo);
        if (ticket.handlerId() != null && "MANUAL".equals(ticket.assignedBy())) {
            // 人工分派优先级最高，避免 Agent 或规则服务覆盖坐席显式选择。
            return ticket;
        }

        List<StaffCandidate> availableCandidates = staffDirectoryService.listAll().stream()
                .filter(StaffMember::online)
                .filter(StaffMember::acceptingTickets)
                .map(staff -> buildCandidate(ticket, staff))
                .filter(candidate -> candidate.activeTickets() < candidate.staff().maxActiveTickets())
                .toList();

        if (availableCandidates.isEmpty()) {
            return ticketService.assignAutomatically(
                    ticketNo,
                    null,
                    ticket.assignedGroup(),
                    "AI_ASSISTED",
                    "当前没有在线且未超负载的可接单坐席，工单继续停留在待分派队列。",
                    0
            );
        }

        List<StaffCandidate> familiarCandidates = availableCandidates.stream()
                .filter(StaffCandidate::familiar)
                .toList();
        List<StaffCandidate> skillMatchedCandidates = availableCandidates.stream()
                .filter(StaffCandidate::skillMatched)
                .toList();
        List<StaffCandidate> candidates = !familiarCandidates.isEmpty()
                ? familiarCandidates
                : (!skillMatchedCandidates.isEmpty() ? skillMatchedCandidates : availableCandidates);

        StaffCandidate best = candidates.stream()
                .max(Comparator.comparingInt(StaffCandidate::score)
                        .thenComparing(candidate -> -candidate.activeTickets())
                        .thenComparing(candidate -> candidate.staff().userId(), Comparator.reverseOrder()))
                .orElseThrow();

        return ticketService.assignAutomatically(
                ticketNo,
                best.staff().userId(),
                best.staff().groupName(),
                "AI_ASSISTED",
                best.reason(),
                best.score()
        );
    }

    private StaffCandidate buildCandidate(SupportTicket ticket, StaffMember staff) {
        int activeTickets = ticketService.countActiveTicketsByHandler(staff.userId());
        boolean familiar = isFamiliarStaff(ticket, staff);
        boolean skillMatched = isSkillMatched(ticket, staff);
        boolean groupMatched = Objects.equals(normalize(ticket.assignedGroup()), normalize(staff.groupName()));

        int score = 0;
        if (familiar) {
            score += 100;
        }
        if (skillMatched) {
            score += 50;
        }
        if (groupMatched) {
            score += 30;
        }
        score += Math.max(0, 30 - activeTickets * 10);
        if (isHighPriority(ticket)) {
            score += Math.max(0, staff.maxActiveTickets() - activeTickets) * 5;
        }
        if (isSlaUrgent(ticket)) {
            score += 10;
        }

        return new StaffCandidate(
                staff,
                activeTickets,
                score,
                familiar,
                skillMatched,
                buildReason(ticket, staff, familiar, skillMatched, groupMatched, activeTickets, score)
        );
    }

    private boolean isFamiliarStaff(SupportTicket ticket, StaffMember staff) {
        if (ticket.customerId() == null) {
            return false;
        }
        SupportTicket recentTicket = ticketService.findRecentHandledTicket(ticket.customerId(), ticket.ticketNo());
        return recentTicket != null && staff.userId().equals(recentTicket.handlerId());
    }

    private boolean isSkillMatched(SupportTicket ticket, StaffMember staff) {
        String ticketType = normalize(ticket.ticketType());
        return ticketType != null && staff.skills().contains(ticketType);
    }

    private boolean isHighPriority(SupportTicket ticket) {
        String priority = normalize(ticket.priority());
        return "high".equals(priority) || "urgent".equals(priority);
    }

    private boolean isSlaUrgent(SupportTicket ticket) {
        return ticket.slaDeadline() != null && ticket.slaDeadline().isBefore(LocalDateTime.now().plusHours(4));
    }

    private String buildReason(
            SupportTicket ticket,
            StaffMember staff,
            boolean familiar,
            boolean skillMatched,
            boolean groupMatched,
            int activeTickets,
            int score
    ) {
        String familiarText = familiar ? "命中熟客优先；" : "未命中熟客；";
        String skillText = skillMatched ? "技能匹配工单类型；" : "技能未直接匹配；";
        String groupText = groupMatched ? "处理组匹配；" : "按候选坐席组承接；";
        String priorityText = isHighPriority(ticket) ? "高优先级已计入负载余量；" : "普通优先级；";
        return familiarText
                + skillText
                + groupText
                + priorityText
                + "当前活跃工单 " + activeTickets
                + " 单，推荐 " + staff.displayName()
                + "，派单分 " + score + "。";
    }

    private String normalize(String value) {
        return value == null || value.isBlank() ? null : value.trim().toLowerCase();
    }

    private record StaffCandidate(
            StaffMember staff,
            int activeTickets,
            int score,
            boolean familiar,
            boolean skillMatched,
            String reason
    ) {
    }
}
