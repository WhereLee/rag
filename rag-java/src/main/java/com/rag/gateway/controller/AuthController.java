package com.rag.gateway.controller;

import com.rag.gateway.security.AuthRateLimiter;
import com.rag.gateway.security.JwtUtil;
import com.rag.gateway.service.UserService;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

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

    public record Cred(String username, String password, String role) {}

    @PostMapping("/register")
    public ResponseEntity<?> register(@RequestBody Cred c, HttpServletRequest req) {
        // IP 限流：每 IP 每分钟 3 次
        if (!authRateLimiter.tryRegister(req)) {
            return ResponseEntity.status(429).body(Map.of("error", "注册过于频繁，请稍后再试"));
        }
        try {
            return ResponseEntity.ok(userService.register(c.username(), c.password(), c.role()));
        } catch (IllegalArgumentException e) {
            return ResponseEntity.badRequest().body(Map.of("error", e.getMessage()));
        }
    }

    @PostMapping("/login")
    public ResponseEntity<?> login(@RequestBody Cred c, HttpServletRequest req) {
        // IP 限流 + 账号锁定检查
        AuthRateLimiter.LoginResult result = authRateLimiter.tryLogin(req, c.username());
        if (!result.isAllowed()) {
            if (result == AuthRateLimiter.LoginResult.RATE_LIMITED) {
                return ResponseEntity.status(429).body(Map.of("error", "登录过于频繁，请稍后再试"));
            }
            return ResponseEntity.status(429).body(Map.of(
                    "error", "账号已锁定，请" + result.getRemainSeconds() + "秒后重试"));
        }

        try {
            Map<String, Object> u = userService.login(c.username(), c.password());
            authRateLimiter.recordLoginSuccess(c.username());
            String token = jwtUtil.issue((String) u.get("username"), (String) u.get("role"));
            return ResponseEntity.ok(Map.of("token", token, "username", u.get("username"),
                    "role", u.get("role")));
        } catch (IllegalArgumentException e) {
            authRateLimiter.recordLoginFailure(c.username());
            return ResponseEntity.status(401).body(Map.of("error", e.getMessage()));
        }
    }
}
