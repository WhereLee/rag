package com.rag.gateway.security;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;
import org.springframework.web.util.ContentCachingResponseWrapper;

import java.io.IOException;
import java.util.UUID;

/**
 * 请求日志过滤器：每次请求生成唯一 correlation ID，记录入参/出参/耗时。
 *
 * 输出结构化日志字段：
 * - method, uri, userId, status, elapsedMs, size
 * - 慢请求（>5s）标记 WARN
 * - X-Request-ID 透传给下游 Python 服务，实现跨服务关联
 *
 * 排除：/health（频繁探活噪声大）、静态资源。
 */
@Component
@Order(-200)  // 最先执行（在 SecurityHeadersFilter 之前）
public class RequestLoggingFilter extends OncePerRequestFilter {

    private static final Logger log = LoggerFactory.getLogger("rag.access");
    private static final long SLOW_THRESHOLD_MS = 5000;

    @Override
    protected boolean shouldNotFilter(HttpServletRequest req) {
        String uri = req.getRequestURI();
        // /api/qa/**：SSE 流式响应与 ContentCachingResponseWrapper 不兼容
        // （异步线程在 filter finally 的 copyBodyToResponse 之后才写数据，缓冲被清空导致 0 字节），
        // 该路径不做响应缓存包装（鉴权仍由 AuthFilter 保证，业务日志在 QaController 内）
        return uri.startsWith("/api/qa") || uri.equals("/health") || uri.startsWith("/error")
                || uri.endsWith(".ico") || uri.endsWith(".js") || uri.endsWith(".css");
    }

    @Override
    protected void doFilterInternal(HttpServletRequest req, HttpServletResponse resp,
                                    FilterChain chain) throws ServletException, IOException {
        // 生成或复用 correlation ID
        String requestId = req.getHeader("X-Request-ID");
        if (requestId == null || requestId.isEmpty() || requestId.length() > 64) {
            requestId = UUID.randomUUID().toString().replace("-", "").substring(0, 16);
        }
        resp.setHeader("X-Request-ID", requestId);
        req.setAttribute("X-Request-ID", requestId);

        // MDC 注入，让本请求后续所有日志自动携带 requestId
        MDC.put("requestId", requestId);

        long start = System.currentTimeMillis();
        String method = req.getMethod();
        String uri = req.getRequestURI();
        String username = (String) req.getAttribute("username");
        int contentLength = req.getContentLength();

        // 包装 response 以便读取 status
        ContentCachingResponseWrapper wrappedResp = new ContentCachingResponseWrapper(resp);

        try {
            chain.doFilter(req, wrappedResp);
        } catch (Exception e) {
            long elapsed = System.currentTimeMillis() - start;
            log.error("{} {} user={} elapsed={}ms ERROR: {}",
                    method, uri, orAnon(username), elapsed, e.getMessage());
            MDC.clear();
            throw e;
        } finally {
            wrappedResp.copyBodyToResponse();
        }

        long elapsed = System.currentTimeMillis() - start;
        int status = wrappedResp.getStatus();
        int respSize = wrappedResp.getContentSize();

        if (elapsed > SLOW_THRESHOLD_MS) {
            log.warn("SLOW {} {} user={} status={} elapsed={}ms resp={}B req={}B",
                    method, uri, orAnon(username), status, elapsed, respSize, contentLength);
        } else {
            log.info("{} {} user={} status={} elapsed={}ms resp={}B",
                    method, uri, orAnon(username), status, elapsed, respSize);
        }

        MDC.clear();
    }

    private String orAnon(String username) {
        return username != null ? username : "-";
    }
}
