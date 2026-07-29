package com.example.business.service;

import com.example.business.entity.SupportTicket;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;

import java.util.Map;

/** 工单状态成功变更后，以最佳努力通知关联的 Agent 客户会话。 */
@Service
public class TicketConversationNotifier {
    private final RestClient restClient = RestClient.create();
    private final String agentUrl;
    private final String internalSecret;

    public TicketConversationNotifier(
            @Value("${agent.service-url:http://127.0.0.1:8000}") String agentUrl,
            @Value("${agent.internal-secret:enterprise-customer-agent-demo-internal-secret}") String internalSecret) {
        this.agentUrl = agentUrl.replaceAll("/$", "");
        this.internalSecret = internalSecret;
    }

    /** 通知失败不回滚 Java 已落库的真实工单状态，客户仍可通过列表回源查看。 */
    public void notifyStatusChanged(SupportTicket ticket) {
        if (ticket.externalSessionNo() == null || ticket.externalSessionNo().isBlank()) return;
        try {
            restClient.post().uri(agentUrl + "/api/internal/tickets/status-sync")
                    .header("X-Agent-Internal-Secret", internalSecret)
                    .body(Map.of("ticketNo", ticket.ticketNo(), "customerId", ticket.customerId(), "externalSessionNo", ticket.externalSessionNo(), "status", ticket.status()))
                    .retrieve().toBodilessEntity();
        } catch (Exception ignored) {
            // 会话通知为增强链路，不能阻塞工单主流程。
        }
    }
}
