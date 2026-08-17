package com.rag.gateway.controller;

import com.rag.gateway.service.AuditService;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.reactive.function.client.WebClient;

import java.time.Duration;
import java.util.List;
import java.util.Map;

/**
 * 管理 API 代理：prompt/审批/诊断/评估/文档，转发 Python 服务。
 * 仅 admin 角色可访问（审批/变更类）；查询类放开给所有登录用户。
 *
 * 安全：/proxy/** 通配转发改为前缀白名单校验，防止把 Python 任意端点
 * （如 /api/ingest/ingest-path 的任意本机路径入库）暴露给调用方。
 */
@RestController
@RequestMapping("/api/admin")
public class AdminProxyController {

    /** GET 代理白名单前缀（查询类）。 */
    private static final List<String> GET_ALLOWED = List.of(
            "/api/eval", "/api/diagnosis", "/api/feedback", "/api/ingest",
            "/api/admin", "/api/rag");
    /** POST 代理白名单前缀（管理操作类；不含 /api/ingest 写路径）。 */
    private static final List<String> POST_ALLOWED = List.of(
            "/api/eval", "/api/diagnosis", "/api/feedback", "/api/admin");

    private final WebClient python;
    private final AuditService audit;

    public AdminProxyController(WebClient pythonWebClient, AuditService audit) {
        this.python = pythonWebClient;
        this.audit = audit;
    }

    private static boolean allowed(String path, List<String> prefixes) {
        return prefixes.stream().anyMatch(path::startsWith);
    }

    /** 通用 GET 代理（白名单前缀）。 */
    @GetMapping("/proxy/**")
    public ResponseEntity<?> proxyGet(HttpServletRequest req) {
        String path = req.getRequestURI().substring("/api/admin/proxy".length());
        if (!allowed(path, GET_ALLOWED)) {
            return ResponseEntity.status(403).body(Map.of("error", "路径不在代理白名单内"));
        }
        String username = (String) req.getAttribute("username");
        try {
            Object resp = python.get().uri(path)
                    .retrieve().bodyToMono(Object.class)
                    .timeout(Duration.ofSeconds(60)).block();
            audit.record(username, "admin.get", path, 200, 0);
            return ResponseEntity.ok(resp);
        } catch (Exception e) {
            return ResponseEntity.status(502).body(Map.of("error", String.valueOf(e.getMessage())));
        }
    }

    /** 通用 POST 代理（仅 admin + 白名单）。 */
    @PostMapping(value = "/proxy/**", consumes = MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<?> proxyPost(@RequestBody(required = false) Map<String, Object> body,
                                       HttpServletRequest req) {
        String role = (String) req.getAttribute("role");
        if (!"admin".equals(role)) {
            return ResponseEntity.status(403).body(Map.of("error", "需要 admin 权限"));
        }
        String path = req.getRequestURI().substring("/api/admin/proxy".length());
        if (!allowed(path, POST_ALLOWED)) {
            return ResponseEntity.status(403).body(Map.of("error", "路径不在代理白名单内"));
        }
        String username = (String) req.getAttribute("username");
        long t0 = System.currentTimeMillis();
        try {
            Object resp = python.post().uri(path)
                    .contentType(MediaType.APPLICATION_JSON)
                    .bodyValue(body == null ? Map.of() : body)
                    .retrieve().bodyToMono(Object.class)
                    .timeout(Duration.ofMinutes(30))   // 审批回归对比可能较长
                    .block();
            audit.record(username, "admin.post", path, 200, System.currentTimeMillis() - t0);
            return ResponseEntity.ok(resp);
        } catch (Exception e) {
            audit.record(username, "admin.post", path, 502, System.currentTimeMillis() - t0);
            return ResponseEntity.status(502).body(Map.of("error", String.valueOf(e.getMessage())));
        }
    }
}
