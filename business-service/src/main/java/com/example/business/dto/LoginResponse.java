package com.example.business.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * 登录响应，返回 Token 和当前用户信息，供前端保存登录态并透传给 Agent。
 */
public record LoginResponse(
        String token,
        @JsonProperty("token_type") String tokenType,
        CurrentUser user
) {
}
