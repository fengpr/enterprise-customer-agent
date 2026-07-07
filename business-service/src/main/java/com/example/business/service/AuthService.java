package com.example.business.service;

import com.example.business.dto.CurrentUser;
import com.example.business.dto.LoginResponse;
import com.example.business.entity.Employee;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.web.server.ResponseStatusException;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Base64;
import java.util.List;
import java.util.Map;

/**
 * 认证服务，客户 Demo 账号保留在内存中，内部员工账号统一从 employee 表读取。
 */
@Service
public class AuthService {
    private static final String JWT_SECRET = "enterprise-customer-agent-demo-secret";
    private static final long TOKEN_TTL_SECONDS = 12 * 60 * 60;
    private static final ObjectMapper OBJECT_MAPPER = new ObjectMapper();

    private final EmployeeService employeeService;
    private final List<DemoAccount> customerAccounts = List.of(
            new DemoAccount("demo", "123456", 1L, 1L, "Demo Customer", "customer"),
            new DemoAccount("buyer", "123456", 2L, 2L, "Test Buyer", "customer")
    );

    public AuthService(EmployeeService employeeService) {
        this.employeeService = employeeService;
    }

    /**
     * 校验登录账号并签发 Demo JWT。
     *
     * @param username 登录名
     * @param password 明文密码，Demo 阶段使用
     * @return 登录响应
     */
    public LoginResponse login(String username, String password) {
        CurrentUser user = customerAccounts.stream()
                .filter(item -> item.username().equals(username) && item.password().equals(password))
                .findFirst()
                .map(DemoAccount::toCurrentUser)
                .orElseGet(() -> loginEmployee(username, password));
        return new LoginResponse(createToken(user), "Bearer", user);
    }

    /**
     * 根据 Authorization 请求头解析当前用户。
     *
     * @param authorization Authorization 请求头，格式为 Bearer token
     * @return 当前用户
     */
    public CurrentUser currentUser(String authorization) {
        Map<String, Object> payload = parsePayload(extractToken(authorization));
        return new CurrentUser(
                toLong(payload.get("user_id")),
                toLong(payload.get("customer_id")),
                String.valueOf(payload.get("display_name")),
                String.valueOf(payload.get("role"))
        );
    }

    /**
     * 从 Token 中解析当前客户 ID，客户侧业务接口以该值为准。
     *
     * @param authorization Authorization 请求头
     * @return 当前客户 ID
     */
    public Long currentCustomerId(String authorization) {
        return currentUser(authorization).customerId();
    }

    /**
     * 校验当前用户是否为坐席。
     *
     * @param authorization Authorization 请求头
     * @return 当前坐席用户
     */
    public CurrentUser requireStaff(String authorization) {
        CurrentUser user = currentUser(authorization);
        if (!"staff".equals(user.role())) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "仅客服坐席可操作");
        }
        return user;
    }

    /**
     * 校验当前用户是否为调度角色。
     *
     * @param authorization Authorization 请求头
     * @return 当前调度用户
     */
    public CurrentUser requireDispatcher(String authorization) {
        CurrentUser user = currentUser(authorization);
        if (!"dispatcher".equals(user.role())) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "仅调度角色可操作");
        }
        return user;
    }

    /**
     * 校验当前用户是否为坐席或调度。
     *
     * @param authorization Authorization 请求头
     * @return 当前内部用户
     */
    public CurrentUser requireStaffOrDispatcher(String authorization) {
        CurrentUser user = currentUser(authorization);
        if (!"staff".equals(user.role()) && !"dispatcher".equals(user.role())) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "仅内部客服角色可访问");
        }
        return user;
    }

    private CurrentUser loginEmployee(String username, String password) {
        Employee employee = employeeService.findEnabledByCredentials(username, password)
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.UNAUTHORIZED, "用户名或密码错误"));
        return new CurrentUser(employee.userId(), 0L, employee.displayName(), employee.role());
    }

    private String createToken(CurrentUser user) {
        try {
            String header = toBase64Url(OBJECT_MAPPER.writeValueAsBytes(Map.of("alg", "HS256", "typ", "JWT")));
            String payload = toBase64Url(OBJECT_MAPPER.writeValueAsBytes(Map.of(
                    "user_id", user.userId(),
                    "customer_id", user.customerId(),
                    "display_name", user.displayName(),
                    "role", user.role(),
                    "exp", Instant.now().getEpochSecond() + TOKEN_TTL_SECONDS
            )));
            String signature = sign(header + "." + payload);
            return header + "." + payload + "." + signature;
        } catch (Exception ex) {
            throw new ResponseStatusException(HttpStatus.INTERNAL_SERVER_ERROR, "Token 签发失败");
        }
    }

    private Map<String, Object> parsePayload(String token) {
        try {
            String[] parts = token.split("\\.");
            if (parts.length != 3 || !sign(parts[0] + "." + parts[1]).equals(parts[2])) {
                throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "Token 无效");
            }
            Map<String, Object> payload = OBJECT_MAPPER.readValue(
                    Base64.getUrlDecoder().decode(parts[1]),
                    new TypeReference<>() {
                    }
            );
            if (toLong(payload.get("exp")) < Instant.now().getEpochSecond()) {
                throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "Token 已过期");
            }
            return payload;
        } catch (ResponseStatusException ex) {
            throw ex;
        } catch (Exception ex) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "Token 解析失败");
        }
    }

    private String extractToken(String authorization) {
        if (authorization == null || !authorization.startsWith("Bearer ")) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "请先登录");
        }
        return authorization.substring("Bearer ".length()).trim();
    }

    private String sign(String content) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(JWT_SECRET.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        return toBase64Url(mac.doFinal(content.getBytes(StandardCharsets.UTF_8)));
    }

    private String toBase64Url(byte[] bytes) {
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
    }

    private Long toLong(Object value) {
        if (value instanceof Number number) {
            return number.longValue();
        }
        return Long.parseLong(String.valueOf(value));
    }

    private record DemoAccount(
            String username,
            String password,
            Long userId,
            Long customerId,
            String displayName,
            String role
    ) {
        private CurrentUser toCurrentUser() {
            return new CurrentUser(userId, customerId, displayName, role);
        }
    }
}
