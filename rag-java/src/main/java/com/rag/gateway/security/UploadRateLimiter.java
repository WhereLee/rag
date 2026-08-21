package com.rag.gateway.security;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.util.ArrayDeque;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 上传接口限流（按用户）：每用户每分钟 N 次。
 *
 * 单机进程内实现；多实例部署时必须换 Redis 共享计数（见 docs/坑位记录.md 第 1 条）。
 * 与登录限流（AuthRateLimiter 按 IP）维度不同：上传按用户（登录后才有 user_id）。
 */
@Component
public class UploadRateLimiter {

    private static final Logger log = LoggerFactory.getLogger(UploadRateLimiter.class);
    private static final long WINDOW_MS = 60_000;

    private final Map<Long, ArrayDeque<Long>> timestamps = new ConcurrentHashMap<>();
    private final int ratePerMinute;

    public UploadRateLimiter(@Value("${gateway.upload.rate-per-minute:10}") int ratePerMinute) {
        this.ratePerMinute = Math.max(1, ratePerMinute);
    }

    /** 尝试放行；窗口内次数已满返回 false（调用方应返回 429）。 */
    public boolean allow(Long userId) {
        long now = System.currentTimeMillis();
        ArrayDeque<Long> q = timestamps.computeIfAbsent(userId, k -> new ArrayDeque<>());
        synchronized (q) {
            while (!q.isEmpty() && now - q.peekFirst() > WINDOW_MS) {
                q.pollFirst();
            }
            if (q.size() >= ratePerMinute) {
                return false;
            }
            q.addLast(now);
            return true;
        }
    }
}
