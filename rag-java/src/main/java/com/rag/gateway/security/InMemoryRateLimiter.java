package com.rag.gateway.security;

import java.util.Deque;
import java.util.Iterator;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedDeque;

/**
 * 滑动窗口限流（内存实现）：每 key 每分钟 N 次。
 * 单机网关场景足够；分布式场景实现 RateLimiter 接口换 Redis ZSET（接口化留位）。
 */
public class InMemoryRateLimiter implements RateLimiter {

    private final int maxPerMinute;
    private final Map<String, Deque<Long>> windows = new ConcurrentHashMap<>();

    public InMemoryRateLimiter(int maxPerMinute) {
        this.maxPerMinute = maxPerMinute;
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
}
