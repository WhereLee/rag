package com.rag.gateway.security;

import java.util.Deque;
import java.util.Iterator;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedDeque;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * 滑动窗口限流（内存实现）：每 key 每分钟 N 次。
 * 单机网关场景足够；分布式场景实现 RateLimiter 接口换 Redis ZSET（接口化留位）。
 *
 * 安全加固：定时清理超过 5 分钟无活动的 key，防止内存泄漏。
 */
public class InMemoryRateLimiter implements RateLimiter {

    private final int maxPerMinute;
    private final Map<String, Deque<Long>> windows = new ConcurrentHashMap<>();
    private final ScheduledExecutorService cleaner;

    public InMemoryRateLimiter(int maxPerMinute) {
        this.maxPerMinute = maxPerMinute;
        // 每 5 分钟清理一次过期 key
        cleaner = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "rate-limiter-cleaner");
            t.setDaemon(true);
            return t;
        });
        cleaner.scheduleAtFixedRate(this::evictStale, 5, 5, TimeUnit.MINUTES);
    }

    @Override
    public boolean tryAcquire(String key) {
        long now = System.currentTimeMillis();
        Deque<Long> q = windows.computeIfAbsent(key, k -> new ConcurrentLinkedDeque<>());
        // 清理窗口外时间戳
        Iterator<Long> it = q.iterator();
        while (it.hasNext()) {
            if (now - it.next() > 60_000) it.remove();
            else break;
        }
        if (q.size() >= maxPerMinute) return false;
        q.addLast(now);
        return true;
    }

    /** 清除超过 5 分钟无活动的 key，防止内存泄漏。 */
    private void evictStale() {
        long cutoff = System.currentTimeMillis() - 5 * 60_000;
        windows.entrySet().removeIf(entry -> {
            Deque<Long> q = entry.getValue();
            if (q.isEmpty()) return true;
            Long latest = q.peekLast();
            return latest != null && latest < cutoff;
        });
    }
}
