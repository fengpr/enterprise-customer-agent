package com.example.business.dto;

/**
 * 登录请求，承载 Demo 账号密码；正式环境应接入账号体系和密码哈希校验。
 */
public record LoginRequest(
        String username,
        String password
) {
}
