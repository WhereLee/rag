package com.rag.gateway.security;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;

/**
 * 安全响应头过滤器：为所有响应添加安全相关的 HTTP 头。
 *
 * 防护：
 * - X-Content-Type-Options: nosniff — 防止 MIME 类型嗅探
 * - X-Frame-Options: DENY — 防止点击劫持
 * - X-XSS-Protection: 0 — 现代浏览器已弃用，设为 0 避免副作用
 * - Referrer-Policy: strict-origin-when-cross-origin — 限制 Referer 泄露
 * - Cache-Control: no-store — API 响应不缓存（含敏感数据）
 */
@Component
@Order(-100)  // 最先执行
public class SecurityHeadersFilter extends OncePerRequestFilter {

    @Override
    protected void doFilterInternal(HttpServletRequest req, HttpServletResponse resp,
                                    FilterChain chain) throws ServletException, IOException {
        resp.setHeader("X-Content-Type-Options", "nosniff");
        resp.setHeader("X-Frame-Options", "DENY");
        resp.setHeader("X-XSS-Protection", "0");
        resp.setHeader("Referrer-Policy", "strict-origin-when-cross-origin");
        // API 响应不缓存
        if (req.getRequestURI().startsWith("/api/")) {
            resp.setHeader("Cache-Control", "no-store");
        }
        chain.doFilter(req, resp);
    }
}
