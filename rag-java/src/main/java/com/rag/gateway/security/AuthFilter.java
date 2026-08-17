package com.rag.gateway.security;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;

/**
 * JWT 鉴权过滤器：校验 Authorization: Bearer 令牌。
 * 豁免：/api/auth/**（注册登录）、/health。
 * 校验通过后将 username/role 放入 request attribute 供下游使用。
 */
@Component
public class AuthFilter extends OncePerRequestFilter {

    private final JwtUtil jwtUtil;

    public AuthFilter(JwtUtil jwtUtil) {
        this.jwtUtil = jwtUtil;
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest req) {
        String uri = req.getRequestURI();
        return uri.startsWith("/api/auth/") || uri.equals("/health") || uri.startsWith("/error");
    }

    @Override
    protected void doFilterInternal(HttpServletRequest req, HttpServletResponse resp,
                                    FilterChain chain) throws ServletException, IOException {
        String header = req.getHeader("Authorization");
        if (header == null || !header.startsWith("Bearer ")) {
            resp.setStatus(401);
            resp.setContentType("application/json;charset=UTF-8");
            resp.getWriter().write("{\"error\":\"缺少 Authorization: Bearer 令牌\"}");
            return;
        }
        String payload = jwtUtil.verify(header.substring(7));
        if (payload == null) {
            resp.setStatus(401);
            resp.setContentType("application/json;charset=UTF-8");
            resp.getWriter().write("{\"error\":\"令牌无效或已过期\"}");
            return;
        }
        req.setAttribute("username", JwtUtil.extract(payload, "sub"));
        req.setAttribute("role", JwtUtil.extract(payload, "role"));
        chain.doFilter(req, resp);
    }
}
