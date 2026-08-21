package com.rag.gateway.controller;

import com.rag.gateway.dto.LoginRequest;
import com.rag.gateway.dto.RegisterRequest;
import com.rag.gateway.security.AuthRateLimiter;
import com.rag.gateway.security.JwtUtil;
import com.rag.gateway.service.UserService;
import com.rag.gateway.vo.LoginVO;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/**
 * 认证接口：注册 / 登录。
 *
 * 安全设计：
 * - 登录失败统一"用户名或密码错误"（不区分账号不存在/密码错误，防账号枚举）
 * - 注册/登录均限流（IP 级）+ 连续失败锁定（AuthRateLimiter）
 * - 注册不接受 role：管理员仅通过初始化脚本创建
 * - 参数校验在 DTO 层（@Valid），业务错误（用户名已存在等）在 Service 层
 */
@RestController
@RequestMapping("/api/auth")
public class AuthController {

    private final UserService userService;
    private final JwtUtil jwtUtil;
    private final AuthRateLimiter authRateLimiter;

    public AuthController(UserService userService, JwtUtil jwtUtil,
                          AuthRateLimiter authRateLimiter) {
        this.userService = userService;
        this.jwtUtil = jwtUtil;
        this.authRateLimiter = authRateLimiter;
    }

    @PostMapping("/register")
    public ResponseEntity<?> register(@Valid @RequestBody RegisterRequest req,
                                      HttpServletRequest httpReq) {
        // IP 限流：每 IP 每分钟 3 次
        if (!authRateLimiter.tryRegister(httpReq)) {
            return ResponseEntity.status(429).body(Map.of("error", "注册过于频繁，请稍后再试"));
        }
        try {
            return ResponseEntity.ok(userService.register(req.username(), req.password()));
        } catch (IllegalArgumentException e) {
            return ResponseEntity.badRequest().body(Map.of("error", e.getMessage()));
        }
    }

    @PostMapping("/login")
    public ResponseEntity<?> login(@Valid @RequestBody LoginRequest req,
                                   HttpServletRequest httpReq) {
        // IP 限流 + 账号锁定检查
        AuthRateLimiter.LoginResult result = authRateLimiter.tryLogin(httpReq, req.username());
        if (!result.allowed()) {
            if (result.rateLimited()) {
                return ResponseEntity.status(429).body(Map.of("error", "登录过于频繁，请稍后再试"));
            }
            return ResponseEntity.status(429).body(Map.of(
                    "error", "账号已锁定，请" + result.remainSeconds() + "秒后重试"));
        }

        try {
            Map<String, Object> u = userService.login(req.username(), req.password());
            authRateLimiter.recordLoginSuccess(req.username());
            String token = jwtUtil.issue((String) u.get("username"), (String) u.get("role"));
            return ResponseEntity.ok(new LoginVO(token, (Long) u.get("id"),
                    (String) u.get("username"), (String) u.get("role")));
        } catch (IllegalArgumentException e) {
            authRateLimiter.recordLoginFailure(req.username());
            return ResponseEntity.status(401).body(Map.of("error", e.getMessage()));
        }
    }
}
