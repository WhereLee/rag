package com.rag.gateway.controller;

import com.rag.gateway.service.AuditService;
import com.rag.gateway.service.UserService;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.util.LinkedMultiValueMap;
import org.springframework.util.MultiValueMap;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;
import org.springframework.web.reactive.function.BodyInserters;
import org.springframework.web.reactive.function.client.ClientResponse;
import org.springframework.web.reactive.function.client.WebClient;

import java.time.Duration;
import java.util.List;
import java.util.Map;

/**
 * 管理 API 代理：prompt/审批/诊断/评估/文档/上传/任务，转发 Python 服务。
 * 仅 admin 角色可访问（审批/变更类）；查询类放开给所有登录用户。
 * 多租户：透传 X-User-Id 请求头给 Python（X-Gateway-Sign 由 WebClient 过滤器自动注入）。
 *
 * 安全：/proxy/** 通配转发改为前缀白名单校验，防止把 Python 任意端点
 * （如 /api/ingest/ingest-path 的任意本机路径入库）暴露给调用方。
 *
 * 透传语义（第一轮修复）：统一使用 exchange() 获取 ClientResponse，
 * 原样透传 Python 的状态码 / body / Content-Type —— 业务错误（403/400/404）
 * 不再被统一包成 502；网络/超时等网关侧错误才回 502 并携带 trace_id。
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
    private final UserService userService;

    public AdminProxyController(WebClient pythonWebClient, AuditService audit,
                                UserService userService) {
        this.python = pythonWebClient;
        this.audit = audit;
        this.userService = userService;
    }

    private static boolean allowed(String path, List<String> prefixes) {
        return prefixes.stream().anyMatch(path::startsWith);
    }

    /** 按客户端的 X-Request-ID 生成错误响应（网关侧错误统一携带 trace_id）。 */
    private ResponseEntity<?> gatewayError(HttpServletRequest req, int status, String err) {
        String rid = (String) req.getAttribute("X-Request-ID");
        return ResponseEntity.status(status).body(Map.of(
                "error", err, "trace_id", rid != null ? rid : ""));
    }

    /** 透传 Python 响应：状态码/body/Content-Type 原样转发（含 4xx 业务错误）。 */
    private ResponseEntity<byte[]> forward(ClientResponse resp, long t0,
                                          String username, String action, String target) {
        byte[] raw = resp.bodyToMono(byte[].class).block();
        audit.record(username, action, target, resp.statusCode().value(),
                (int) (System.currentTimeMillis() - t0));
        return ResponseEntity.status(resp.statusCode())
                .contentType(resp.headers().contentType().orElse(null))
                .body(raw);
    }

    /** 通用 GET 代理（白名单前缀）。 */
    @GetMapping("/proxy/**")
    public ResponseEntity<?> proxyGet(HttpServletRequest req) {
        String path = req.getRequestURI().substring("/api/admin/proxy".length());
        if (!allowed(path, GET_ALLOWED)) {
            return ResponseEntity.status(403).body(Map.of("error", "路径不在代理白名单内"));
        }
        String username = (String) req.getAttribute("username");
        Long userId = userService.getUserIdByUsername(username);
        String requestId = (String) req.getAttribute("X-Request-ID");
        long t0 = System.currentTimeMillis();
        try {
            ClientResponse resp = python.get().uri(path)
                    .header("X-User-Id", String.valueOf(userId))
                    .header("X-Request-ID", requestId != null ? requestId : "")
                    .exchange()
                    .timeout(Duration.ofSeconds(60)).block();
            return forward(resp, t0, username, "admin.get", path);
        } catch (Exception e) {
            return gatewayError(req, 502, String.valueOf(e.getMessage()));
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
        Long userId = userService.getUserIdByUsername(username);
        String requestId = (String) req.getAttribute("X-Request-ID");
        long t0 = System.currentTimeMillis();
        try {
            ClientResponse resp = python.post().uri(path)
                    .contentType(MediaType.APPLICATION_JSON)
                    .header("X-User-Id", String.valueOf(userId))
                    .header("X-Request-ID", requestId != null ? requestId : "")
                    .bodyValue(body == null ? Map.of() : body)
                    .exchange()
                    .timeout(Duration.ofMinutes(30))   // 审批回归对比可能较长
                    .block();
            return forward(resp, t0, username, "admin.post", path);
        } catch (Exception e) {
            return gatewayError(req, 502, String.valueOf(e.getMessage()));
        }
    }

    /**
     * 文件上传代理：验证 JWT 后转发 multipart 请求到 Python 服务。
     * 安全：从 JWT token 提取 user_id（不信任前端传入的 X-User-Id）。
     * 透传 Python 状态码：202=异步任务已入队，200=去重快路径。
     */
    @PostMapping(value = "/proxy/api/ingest/upload", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ResponseEntity<?> proxyUpload(
            @RequestPart("file") MultipartFile file,
            @RequestParam(value = "replace", required = false, defaultValue = "false") Boolean replace,
            HttpServletRequest req) {

        String username = (String) req.getAttribute("username");
        Long userId = userService.getUserIdByUsername(username);
        String requestId = (String) req.getAttribute("X-Request-ID");

        long t0 = System.currentTimeMillis();
        try {
            // 构建 multipart 请求体转发给 Python 服务
            MultiValueMap<String, Object> body = new LinkedMultiValueMap<>();
            body.add("file", file.getResource());
            if (replace != null && replace) {
                body.add("replace", "true");
            }
            ClientResponse resp = python.post()
                    .uri("/api/ingest/upload")
                    .contentType(MediaType.MULTIPART_FORM_DATA)
                    .header("X-User-Id", String.valueOf(userId))
                    .header("X-Request-ID", requestId != null ? requestId : "")
                    .body(BodyInserters.fromMultipartData(body))
                    .exchange()
                    .timeout(Duration.ofMinutes(5))
                    .block();
            return forward(resp, t0, username, "upload", "/api/ingest/upload");
        } catch (Exception e) {
            return gatewayError(req, 502, "上传失败: " + e.getMessage());
        }
    }

    /**
     * 删除文档代理：前端删除文档走网关，user_id 从 JWT 提取不可伪造。
     * 显式端点（不用通配 DELETE 透传，防暴露 Python 侧任意 DELETE 路径）。
     */
    @DeleteMapping("/proxy/api/ingest/documents/{docId}")
    public ResponseEntity<?> proxyDeleteDocument(@PathVariable long docId, HttpServletRequest req) {
        String username = (String) req.getAttribute("username");
        Long userId = userService.getUserIdByUsername(username);
        String requestId = (String) req.getAttribute("X-Request-ID");
        long t0 = System.currentTimeMillis();
        try {
            ClientResponse resp = python.delete().uri("/api/ingest/documents/{id}", docId)
                    .header("X-User-Id", String.valueOf(userId))
                    .header("X-Request-ID", requestId != null ? requestId : "")
                    .exchange()
                    .timeout(Duration.ofSeconds(30)).block();
            return forward(resp, t0, username, "doc.delete", String.valueOf(docId));
        } catch (Exception e) {
            return gatewayError(req, 502, String.valueOf(e.getMessage()));
        }
    }

    /** 入库任务重试代理：所有登录用户可重试自己的失败任务（不受 admin 限定）。 */
    @PostMapping("/proxy/api/ingest/jobs/{jobId}/retry")
    public ResponseEntity<?> proxyRetryJob(@PathVariable long jobId, HttpServletRequest req) {
        String username = (String) req.getAttribute("username");
        Long userId = userService.getUserIdByUsername(username);
        String requestId = (String) req.getAttribute("X-Request-ID");
        long t0 = System.currentTimeMillis();
        try {
            ClientResponse resp = python.post().uri("/api/ingest/jobs/{id}/retry", jobId)
                    .contentType(MediaType.APPLICATION_JSON)
                    .header("X-User-Id", String.valueOf(userId))
                    .header("X-Request-ID", requestId != null ? requestId : "")
                    .bodyValue(Map.of())
                    .exchange()
                    .timeout(Duration.ofSeconds(30)).block();
            return forward(resp, t0, username, "job.retry", String.valueOf(jobId));
        } catch (Exception e) {
            return gatewayError(req, 502, String.valueOf(e.getMessage()));
        }
    }

    /** 第三轮：失败块闭环代理（重试 / 替代图 / 补充说明）。 */
    @PostMapping("/proxy/api/ingest/issues/{issueId}/retry")
    public ResponseEntity<?> proxyRetryIssue(@PathVariable long issueId, HttpServletRequest req) {
        String username = (String) req.getAttribute("username");
        Long userId = userService.getUserIdByUsername(username);
        String requestId = (String) req.getAttribute("X-Request-ID");
        long t0 = System.currentTimeMillis();
        try {
            ClientResponse resp = python.post().uri("/api/ingest/issues/{id}/retry", issueId)
                    .contentType(MediaType.APPLICATION_JSON)
                    .header("X-User-Id", String.valueOf(userId))
                    .header("X-Request-ID", requestId != null ? requestId : "")
                    .bodyValue(Map.of())
                    .exchange()
                    .timeout(Duration.ofSeconds(30)).block();
            return forward(resp, t0, username, "issue.retry", String.valueOf(issueId));
        } catch (Exception e) {
            return gatewayError(req, 502, String.valueOf(e.getMessage()));
        }
    }

    @PostMapping("/proxy/api/ingest/issues/{issueId}/describe")
    public ResponseEntity<?> proxyDescribeIssue(@PathVariable long issueId,
                                                @RequestBody Map<String, Object> body,
                                                HttpServletRequest req) {
        String username = (String) req.getAttribute("username");
        Long userId = userService.getUserIdByUsername(username);
        String requestId = (String) req.getAttribute("X-Request-ID");
        long t0 = System.currentTimeMillis();
        try {
            ClientResponse resp = python.post().uri("/api/ingest/issues/{id}/describe", issueId)
                    .contentType(MediaType.APPLICATION_JSON)
                    .header("X-User-Id", String.valueOf(userId))
                    .header("X-Request-ID", requestId != null ? requestId : "")
                    .bodyValue(body)
                    .exchange()
                    .timeout(Duration.ofSeconds(30)).block();
            return forward(resp, t0, username, "issue.describe", String.valueOf(issueId));
        } catch (Exception e) {
            return gatewayError(req, 502, String.valueOf(e.getMessage()));
        }
    }

    @PostMapping("/proxy/api/ingest/issues/{issueId}/replace")
    public ResponseEntity<?> proxyReplaceIssue(@PathVariable long issueId,
                                               @RequestPart("file") MultipartFile file,
                                               HttpServletRequest req) {
        String username = (String) req.getAttribute("username");
        Long userId = userService.getUserIdByUsername(username);
        String requestId = (String) req.getAttribute("X-Request-ID");
        long t0 = System.currentTimeMillis();
        try {
            MultiValueMap<String, Object> body = new LinkedMultiValueMap<>();
            body.add("file", file.getResource());
            ClientResponse resp = python.post().uri("/api/ingest/issues/{id}/replace", issueId)
                    .contentType(MediaType.MULTIPART_FORM_DATA)
                    .header("X-User-Id", String.valueOf(userId))
                    .header("X-Request-ID", requestId != null ? requestId : "")
                    .body(BodyInserters.fromMultipartData(body))
                    .exchange()
                    .timeout(Duration.ofMinutes(5)).block();
            return forward(resp, t0, username, "issue.replace", String.valueOf(issueId));
        } catch (Exception e) {
            return gatewayError(req, 502, String.valueOf(e.getMessage()));
        }
    }

    /** 反馈提交代理：所有登录用户可提交（不受 admin 限定）。 */
    @PostMapping("/proxy/api/feedback")
    public ResponseEntity<?> proxyFeedback(@RequestBody Map<String, Object> body,
                                           HttpServletRequest req) {
        String username = (String) req.getAttribute("username");
        Long userId = userService.getUserIdByUsername(username);
        String requestId = (String) req.getAttribute("X-Request-ID");
        long t0 = System.currentTimeMillis();
        try {
            ClientResponse resp = python.post().uri("/api/feedback")
                    .contentType(MediaType.APPLICATION_JSON)
                    .header("X-User-Id", String.valueOf(userId))
                    .header("X-Request-ID", requestId != null ? requestId : "")
                    .bodyValue(body)
                    .exchange()
                    .timeout(Duration.ofSeconds(30)).block();
            return forward(resp, t0, username, "feedback.submit",
                    String.valueOf(body.get("qa_log_id")));
        } catch (Exception e) {
            return gatewayError(req, 502, String.valueOf(e.getMessage()));
        }
    }
}