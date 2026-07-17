package com.example.business.dto;

/**
 * 人工会话跟进工单请求。客户与会话身份由 Agent 服务端读取，前端不得直接调用该接口。
 */
public record HandoffTicketRequest(
        Long customerId,
        String externalSessionNo,
        String title,
        String content,
        String priority
) {
}
