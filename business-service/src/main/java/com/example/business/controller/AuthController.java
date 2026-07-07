package com.example.business.controller;

import com.example.business.dto.CurrentUser;
import com.example.business.dto.LoginRequest;
import com.example.business.dto.LoginResponse;
import com.example.business.service.AuthService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 登录认证控制器，提供 Demo 登录和当前用户查询能力，作为业务系统身份边界。
 */
@RestController
@RequestMapping("/api/auth")
public class AuthController {
    private final AuthService authService;

    public AuthController(AuthService authService) {
        this.authService = authService;
    }

    /**
     * 用户登录接口，成功后返回 Bearer Token。
     *
     * @param request 登录账号密码
     * @return Token 和当前用户信息
     */
    @PostMapping("/login")
    public LoginResponse login(@RequestBody LoginRequest request) {
        return authService.login(request.username(), request.password());
    }

    /**
     * 查询当前登录用户，供前端和 Agent 使用 Token 换取可信客户身份。
     *
     * @param authorization Authorization 请求头
     * @return 当前登录用户
     */
    @GetMapping("/current-user")
    public CurrentUser currentUser(@RequestHeader("Authorization") String authorization) {
        return authService.currentUser(authorization);
    }
}
