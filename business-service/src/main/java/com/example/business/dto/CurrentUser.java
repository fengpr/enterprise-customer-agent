package com.example.business.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * 当前登录用户信息，作为前端、Agent 和业务接口之间传递身份上下文的标准结构。
 */
public record CurrentUser(
        @JsonProperty("user_id") Long userId,
        @JsonProperty("customer_id") Long customerId,
        @JsonProperty("display_name") String displayName,
        String role
) {
}
