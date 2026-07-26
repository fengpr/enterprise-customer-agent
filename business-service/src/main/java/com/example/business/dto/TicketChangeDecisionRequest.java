package com.example.business.dto;

/**
 * 座席审核履约变更申请的受控入参。
 * <p>customerMessage 是面向客户的说明，禁止填写内部风控、排班或个人信息。</p>
 */
public record TicketChangeDecisionRequest(
        String decision,
        String customerMessage
) {
}
