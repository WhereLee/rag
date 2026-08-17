package com.rag.gateway.controller;

import com.rag.gateway.security.JwtUtil;
import com.rag.gateway.service.UserService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

@RestController
@RequestMapping("/api/auth")
public class AuthController {

    private final UserService userService;
    private final JwtUtil jwtUtil;

    public AuthController(UserService userService, JwtUtil jwtUtil) {
        this.userService = userService;
        this.jwtUtil = jwtUtil;
    }

    public record Cred(String username, String password, String role) {}

    @PostMapping("/register")
    public ResponseEntity<?> register(@RequestBody Cred c) {
        try {
            return ResponseEntity.ok(userService.register(c.username(), c.password(), c.role()));
        } catch (IllegalArgumentException e) {
            return ResponseEntity.badRequest().body(Map.of("error", e.getMessage()));
        }
    }

    @PostMapping("/login")
    public ResponseEntity<?> login(@RequestBody Cred c) {
        try {
            Map<String, Object> u = userService.login(c.username(), c.password());
            String token = jwtUtil.issue((String) u.get("username"), (String) u.get("role"));
            return ResponseEntity.ok(Map.of("token", token, "username", u.get("username"),
                    "role", u.get("role")));
        } catch (IllegalArgumentException e) {
            return ResponseEntity.status(401).body(Map.of("error", e.getMessage()));
        }
    }
}
