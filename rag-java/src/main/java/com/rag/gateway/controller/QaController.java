package com.rag.gateway.controller;

import com.rag.gateway.service.AuditService;
import com.rag.gateway.service.UserService;
import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 问答 API：/api/qa/ask → Python qa 服务（:8091）SSE 流式透传。
 *
 * 安全设计：
 * - user_id 一律从 JWT 会话解析（AuthFilter 注入 username → 反查 id），透传 X-User-Id
 *   （Python 侧不信任前端，检索按 user_id 隔离）
 * - 透传层只做字节搬运：Python 的 SSE 事件原样回写前端，网关不解析内容
 * - 前端断开（emitter 异常）→ 终止转发线程，不泄漏生成进程
 */
@RestController
@RequestMapping("/api/qa")
public class QaController {

    private static final Logger log = LoggerFactory.getLogger(QaController.class);

    private final UserService userService;
    private final AuditService audit;
    private final String qaBaseUrl;
    private final ExecutorService forwardPool = Executors.newCachedThreadPool();

    public QaController(UserService userService, AuditService audit,
                        @Value("${gateway.qa.url:http://127.0.0.1:8091}") String qaBaseUrl) {
        this.userService = userService;
        this.audit = audit;
        this.qaBaseUrl = qaBaseUrl;
    }

    @PostMapping(value = "/ask", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter ask(@RequestBody Map<String, Object> body, HttpServletRequest req) {
        String query = body.get("query") == null ? "" : String.valueOf(body.get("query")).trim();
        if (query.isEmpty()) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "query 不能为空");
        }
        if (query.length() > 500) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "问题过长（上限 500 字）");
        }
        String sessionId = body.get("session_id") == null ? "" : String.valueOf(body.get("session_id")).trim();
        // 思考开关：默认开；前端可传 thinking=false 关闭（省 reasoning token）
        boolean thinking = !"false".equalsIgnoreCase(String.valueOf(body.get("thinking")));
        String username = (String) req.getAttribute("username");
        Long userId = userService.getUserIdByUsername(username);
        // 流式接口：异步审计（耗时在转发结束时记录）
        audit.record(username, "qa.ask", query.length() > 100 ? query.substring(0, 100) : query, 200, 0);
        log.info("qa.ask user={} session={} thinking={} query={}", username, sessionId.isEmpty() ? "-" : sessionId,
                thinking, query.length() > 50 ? query.substring(0, 50) + "..." : query);

        SseEmitter emitter = new SseEmitter(120_000L);   // 生成超时 2 分钟
        forwardPool.submit(() -> forward(emitter, userId, query, sessionId, thinking));
        return emitter;
    }

    // ===== 会话 API（JSON 透传 Python qa 服务） =====

    @PostMapping("/sessions")
    public ResponseEntity<?> createSession(@RequestBody Map<String, Object> body, HttpServletRequest req) {
        String username = (String) req.getAttribute("username");
        Long userId = userService.getUserIdByUsername(username);
        return jsonForward("POST", "/qa/sessions", body, userId, username, "qa.session.create");
    }

    @GetMapping("/sessions")
    public ResponseEntity<?> listSessions(@RequestParam(name = "dir_id", required = false) Long dirId,
                                          HttpServletRequest req) {
        String username = (String) req.getAttribute("username");
        Long userId = userService.getUserIdByUsername(username);
        String path = dirId == null ? "/qa/sessions" : "/qa/sessions?dir_id=" + dirId;
        return jsonForward("GET", path, null, userId, username, "qa.session.list");
    }

    @GetMapping("/sessions/{sessionId}/history")
    public ResponseEntity<?> sessionHistory(@PathVariable String sessionId, HttpServletRequest req) {
        String username = (String) req.getAttribute("username");
        Long userId = userService.getUserIdByUsername(username);
        return jsonForward("GET", "/qa/sessions/" + sessionId + "/history", null,
                userId, username, "qa.session.history");
    }

    /** JSON 转发：非流式接口统一处理（POST 带 body / GET 无 body），错误码透传。 */
    private ResponseEntity<?> jsonForward(String method, String path, Map<String, Object> body,
                                          Long userId, String username, String auditAction) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) URI.create(qaBaseUrl + path).toURL().openConnection();
            conn.setRequestMethod(method);
            conn.setConnectTimeout(10_000);
            conn.setReadTimeout(30_000);
            conn.setRequestProperty("X-User-Id", String.valueOf(userId));
            if (body != null) {
                conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                conn.setDoOutput(true);
                String payload = new com.fasterxml.jackson.databind.ObjectMapper()
                        .writeValueAsString(body);
                byte[] bytes = payload.getBytes(StandardCharsets.UTF_8);
                conn.setFixedLengthStreamingMode(bytes.length);
                try (var os = conn.getOutputStream()) {
                    os.write(bytes);
                }
            }
            int code = conn.getResponseCode();
            String resp = readBody(code >= 400 ? conn.getErrorStream() : conn.getInputStream());
            audit.record(username, auditAction, path, code, 0);
            if (code >= 400) {
                try {
                    return ResponseEntity.status(code).body(
                            new com.fasterxml.jackson.databind.ObjectMapper()
                                    .readValue(resp, Map.class));
                } catch (Exception e) {
                    return ResponseEntity.status(code).body(Map.of("error", resp));
                }
            }
            return ResponseEntity.ok(
                    new com.fasterxml.jackson.databind.ObjectMapper().readValue(resp, Object.class));
        } catch (Exception e) {
            log.warn("qa json forward failed: {} {}", path, e.toString());
            return ResponseEntity.status(502).body(Map.of("error", "问答服务暂不可用"));
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    /** 转发 Python qa 服务，SSE 事件逐行回写前端。 */
    private void forward(SseEmitter emitter, Long userId, String query, String sessionId, boolean thinking) {
        HttpURLConnection conn = null;
        try {
            log.info("qa forward start user={} thinking={}", userId, thinking);
            conn = (HttpURLConnection) URI.create(qaBaseUrl + "/qa/ask").toURL().openConnection();
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(10_000);
            conn.setReadTimeout(60_000);
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            conn.setRequestProperty("X-User-Id", String.valueOf(userId));
            conn.setDoOutput(true);
            String payload = "{\"query\":\"" + jsonEscape(query) + "\""
                    + (sessionId.isEmpty() ? "" : ",\"session_id\":\"" + jsonEscape(sessionId) + "\"")
                    + ",\"thinking\":" + thinking + "}";
            byte[] body = payload.getBytes(StandardCharsets.UTF_8);
            conn.setFixedLengthStreamingMode(body.length);
            try (var os = conn.getOutputStream()) {   // 显式 close 才会发送 body
                os.write(body);
            }

            int code = conn.getResponseCode();
            if (code >= 400) {
                String err = readBody(conn.getErrorStream());
                log.warn("qa upstream error: {} {}", code, err);
                // 透传上游错误码与信息（如会话归属 403），供前端区分业务错误与服务不可用
                String msg = "问答服务暂不可用";
                int errCode = code;
                try {
                    var obj = new com.fasterxml.jackson.databind.ObjectMapper().readValue(err, Map.class);
                    if (obj.get("detail") != null) {
                        msg = String.valueOf(obj.get("detail"));
                    } else if (obj.get("error") != null) {
                        msg = String.valueOf(obj.get("error"));
                    }
                } catch (Exception ignored) {
                    // 非 JSON 错误体，保留通用提示
                }
                emitter.send(SseEmitter.event().name("error")
                        .data("{\"type\":\"error\",\"message\":\"" + jsonEscape(msg)
                                + "\",\"code\":" + errCode + "}",
                                MediaType.APPLICATION_JSON));
                emitter.complete();
                return;
            }

            try (BufferedReader in = new BufferedReader(
                    new InputStreamReader(conn.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                int sent = 0;
                while ((line = in.readLine()) != null) {
                    if (!line.startsWith("data:")) {
                        continue;
                    }
                    // byte[] 原样转发：避免 SseEmitter 对 String 按默认编码二次编码（UTF-8 双重编码）
                    emitter.send(SseEmitter.event().data(
                            line.substring(5).trim().getBytes(StandardCharsets.UTF_8)));
                    sent++;
                }
                log.info("qa forward done sent={}", sent);
            }
            emitter.complete();
        } catch (Exception e) {
            log.info("qa forward closed: {}", e.toString());
            try {
                // 上游不可达/中途中断：补发 error 事件，否则前端只能收到空响应无法提示
                emitter.send(SseEmitter.event().name("error")
                        .data("{\"type\":\"error\",\"message\":\"问答服务暂不可用或连接中断\"}",
                                MediaType.APPLICATION_JSON));
                emitter.complete();
            } catch (Exception ignored) {
                // emitter 已失效（如前端已断开）
            }
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    private static String jsonEscape(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"")
                .replace("\n", "\\n").replace("\r", "\\r");
    }

    private static String readBody(java.io.InputStream is) {
        if (is == null) {
            return "";
        }
        try (BufferedReader r = new BufferedReader(new InputStreamReader(is, StandardCharsets.UTF_8))) {
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = r.readLine()) != null) {
                sb.append(line);
            }
            return sb.toString();
        } catch (Exception e) {
            return "";
        }
    }
}
