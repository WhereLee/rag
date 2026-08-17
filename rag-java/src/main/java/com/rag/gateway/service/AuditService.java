package com.rag.gateway.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Service;

/**
 * 审计日志：网关代理的每次调用落 kb_audit_log（异步，不阻塞请求）。
 */
@Service
public class AuditService {

    private static final Logger log = LoggerFactory.getLogger(AuditService.class);
    private final JdbcTemplate jdbc;

    public AuditService(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Async
    public void record(String username, String action, String target,
                       int statusCode, long elapsedMs) {
        try {
            jdbc.update("INSERT INTO kb_audit_log (username, action, target, status_code, elapsed_ms) VALUES (?,?,?,?,?)",
                    username, action, truncate(target, 500), statusCode, elapsedMs);
        } catch (Exception e) {
            log.warn("审计写入失败: {}", e.getMessage());
        }
    }

    private String truncate(String s, int max) {
        return s == null ? null : (s.length() > max ? s.substring(0, max) : s);
    }
}
