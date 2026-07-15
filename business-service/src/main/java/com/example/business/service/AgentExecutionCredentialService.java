package com.example.business.service;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.web.server.ResponseStatusException;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.HexFormat;

/**
 * 校验 Python API 签发给独立 Worker 的短期执行凭证。
 * 凭证只授权指定客户、指定 request_id 在有效期内调用客户侧业务接口，
 * 从而避免把客户原始 Authorization Token 写入 Redis 可靠队列。
 */
@Service
public class AgentExecutionCredentialService {
    private final String executionSecret;

    public AgentExecutionCredentialService(
            @Value("${agent.execution-secret:${AGENT_EXECUTION_SECRET:${AGENT_INTERNAL_SECRET:enterprise-customer-agent-demo-internal-secret}}}")
            String executionSecret
    ) {
        this.executionSecret = executionSecret;
    }

    /**
     * 校验短期凭证并返回已签名的客户 ID。
     *
     * @param credential API 签发的 v1 凭证
     * @param customerId 队列任务中的客户 ID
     * @param requestId 当前队列任务 request_id
     * @return 通过签名保护的客户 ID
     */
    public Long requireCustomerId(String credential, Long customerId, String requestId) {
        if (credential == null || customerId == null || requestId == null || requestId.isBlank()) {
            throw unauthorized("缺少 Agent 执行身份");
        }
        String[] parts = credential.split("\\.");
        if (parts.length != 3 || !"v1".equals(parts[0])) {
            throw unauthorized("Agent 执行凭证格式无效");
        }
        long expiresAt;
        try {
            expiresAt = Long.parseLong(parts[1]);
        } catch (NumberFormatException ex) {
            throw unauthorized("Agent 执行凭证格式无效");
        }
        if (expiresAt < Instant.now().getEpochSecond()) {
            throw unauthorized("Agent 执行凭证已过期");
        }

        String content = customerId + ":" + requestId + ":" + expiresAt;
        String expected = sign(content).substring(0, 24);
        if (!MessageDigest.isEqual(
                expected.getBytes(StandardCharsets.UTF_8),
                parts[2].getBytes(StandardCharsets.UTF_8)
        )) {
            throw unauthorized("Agent 执行凭证无效");
        }
        return customerId;
    }

    private String sign(String content) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(executionSecret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            return HexFormat.of().formatHex(mac.doFinal(content.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception ex) {
            throw new ResponseStatusException(HttpStatus.INTERNAL_SERVER_ERROR, "Agent 执行凭证校验失败");
        }
    }

    private ResponseStatusException unauthorized(String message) {
        return new ResponseStatusException(HttpStatus.UNAUTHORIZED, message);
    }
}
