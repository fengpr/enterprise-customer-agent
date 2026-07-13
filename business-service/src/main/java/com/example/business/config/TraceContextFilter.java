package com.example.business.config;

import jakarta.servlet.Filter;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.ServletRequest;
import jakarta.servlet.ServletResponse;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.UUID;
import org.slf4j.MDC;
import org.springframework.stereotype.Component;

/**
 * 透传 Agent 请求关联标识，确保 Java 业务接口日志可以与 Python、队列任务关联。
 * 不读取或记录 Authorization、订单号等敏感内容。
 */
@Component
public class TraceContextFilter implements Filter {
    @Override
    public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain)
            throws IOException, ServletException {
        HttpServletRequest httpRequest = (HttpServletRequest) request;
        HttpServletResponse httpResponse = (HttpServletResponse) response;
        String requestId = headerOrGenerated(httpRequest, "X-Request-ID", "req-");
        String traceId = headerOrGenerated(httpRequest, "X-Trace-ID", "");
        MDC.put("request_id", requestId);
        MDC.put("trace_id", traceId);
        httpResponse.setHeader("X-Request-ID", requestId);
        httpResponse.setHeader("X-Trace-ID", traceId);
        try {
            chain.doFilter(request, response);
        } finally {
            MDC.remove("request_id");
            MDC.remove("trace_id");
        }
    }

    private String headerOrGenerated(HttpServletRequest request, String name, String prefix) {
        String value = request.getHeader(name);
        return value == null || value.isBlank() ? prefix + UUID.randomUUID().toString().replace("-", "") : value;
    }
}
