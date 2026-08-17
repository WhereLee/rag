package com.rag.gateway.controller;

import com.rag.gateway.security.RateLimiter;
import com.rag.gateway.service.AuditService;
import com.rag.gateway.service.UserService;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Flux;

import java.time.Duration;
import java.util.Map;

/**
 * 对话代理：鉴权/限流/审计后转发 Python 问答服务。
 * 多租户：透传 X-User-Id 请求头给 Python。
 * - JSON 模式：RestClient 语义的同步转发（超时 120s，失败降级明确错误）
 * - SSE 模式：WebClient Flux 透传 Python 侧事件流
 */
@RestController
@RequestMapping("/api/chat")
public class ChatController {

    private final WebClient python;
    private final RateLimiter rateLimiter;
    private final AuditService audit;
    private final UserService userService;

    public ChatController(WebClient pythonWebClient, RateLimiter rateLimiter,
                          AuditService audit, UserService userService) {
        this.python = pythonWebClient;
        this.rateLimiter = rateLimiter;
        this.audit = audit;
        this.userService = userService;
    }

    @PostMapping("/ask")
    public ResponseEntity<?> ask(@RequestBody Map<String, Object> body, HttpServletRequest req) {
        String username = (String) req.getAttribute("username");
        if (!rateLimiter.tryAcquire(username)) {
            return ResponseEntity.status(429).body(Map.of("error", "请求过于频繁，请稍后再试"));
        }
        Long userId = userService.getUserIdByUsername(username);
        long t0 = System.currentTimeMillis();
        boolean stream = Boolean.TRUE.equals(body.get("stream"));
        try {
            if (stream) {
                // SSE 由 askStream 处理；这里不应到达
                return ResponseEntity.badRequest().body(Map.of("error", "stream 请走 /api/chat/ask-stream"));
            }
            String requestId = (String) req.getAttribute("X-Request-ID");
            Map<?, ?> resp = python.post().uri("/api/rag/ask")
                    .contentType(MediaType.APPLICATION_JSON)
                    .header("X-User-Id", String.valueOf(userId))
                    .header("X-Request-ID", requestId != null ? requestId : "")
                    .bodyValue(body)
                    .retrieve().bodyToMono(Map.class)
                    .timeout(Duration.ofSeconds(180))
                    .retry(1)
                    .block();
            audit.record(username, "chat.ask", String.valueOf(body.get("query")), 200,
                    System.currentTimeMillis() - t0);
            return ResponseEntity.ok(resp);
        } catch (Exception e) {
            audit.record(username, "chat.ask", String.valueOf(body.get("query")), 502,
                    System.currentTimeMillis() - t0);
            return ResponseEntity.status(502).body(Map.of(
                    "error", "AI 服务暂不可用: " + rootMsg(e)));
        }
    }

    /** SSE 流式转发：Python 事件原样透传给客户端。 */
    @PostMapping(value = "/ask-stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<ServerSentEvent<String>> askStream(@RequestBody Map<String, Object> body,
                                                   HttpServletRequest req) {
        String username = (String) req.getAttribute("username");
        if (!rateLimiter.tryAcquire(username)) {
            return Flux.just(ServerSentEvent.<String>builder()
                    .data("{\"error\":\"请求过于频繁\"}").build());
        }
        Long userId = userService.getUserIdByUsername(username);
        String requestId = (String) req.getAttribute("X-Request-ID");
        body.put("stream", true);
        audit.record(username, "chat.ask-stream", String.valueOf(body.get("query")), 200, 0);
        return python.post().uri("/api/rag/ask")
                .contentType(MediaType.APPLICATION_JSON)
                .accept(MediaType.TEXT_EVENT_STREAM)
                .header("X-User-Id", String.valueOf(userId))
                .header("X-Request-ID", requestId != null ? requestId : "")
                .bodyValue(body)
                .retrieve()
                .bodyToFlux(String.class)
                .map(line -> ServerSentEvent.<String>builder().data(line).build())
                .timeout(Duration.ofMinutes(5))
                .onErrorResume(e -> Flux.just(ServerSentEvent.<String>builder()
                        .data("{\"error\":\"流式转发中断: " + rootMsg(e) + "\"}").build()));
    }

    @GetMapping("/history/{sessionId}")
    public ResponseEntity<?> history(@PathVariable String sessionId, HttpServletRequest req) {
        String username = (String) req.getAttribute("username");
        Long userId = userService.getUserIdByUsername(username);
        try {
            Object resp = python.get().uri("/api/rag/history/{sid}", sessionId)
                    .header("X-User-Id", String.valueOf(userId))
                    .retrieve().bodyToMono(Object.class)
                    .timeout(Duration.ofSeconds(30)).block();
            audit.record(username, "chat.history", sessionId, 200, 0);
            return ResponseEntity.ok(resp);
        } catch (Exception e) {
            return ResponseEntity.status(502).body(Map.of("error", rootMsg(e)));
        }
    }

    private String rootMsg(Throwable e) {
        Throwable t = e;
        while (t.getCause() != null) t = t.getCause();
        return t.getMessage() == null ? t.getClass().getSimpleName() : t.getMessage();
    }
}
