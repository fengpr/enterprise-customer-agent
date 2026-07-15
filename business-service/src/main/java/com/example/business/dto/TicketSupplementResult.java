package com.example.business.dto;

import com.example.business.entity.SupportTicket;

/**
 * 工单信息追加结果，明确区分“已直接更新”和“仅登记变更申请”，防止客户端误报取件预约已修改。
 */
public record TicketSupplementResult(
        SupportTicket ticket,
        String updateMode,
        boolean fulfillmentUpdated,
        boolean deduplicated
) {
}
