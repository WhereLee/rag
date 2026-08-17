package com.rag.gateway.security;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

/**
 * InMemoryRateLimiter 纯 JUnit 单测：滑动窗口放行/限流。
 */
class InMemoryRateLimiterTest {

    @Test
    void allowsUpToLimitThenRejects() {
        RateLimiter limiter = new InMemoryRateLimiter(3);
        String key = "u1";
        assertTrue(limiter.tryAcquire(key));
        assertTrue(limiter.tryAcquire(key));
        assertTrue(limiter.tryAcquire(key));
        assertFalse(limiter.tryAcquire(key)); // 第 4 次被限流
    }

    @Test
    void keysAreIndependent() {
        RateLimiter limiter = new InMemoryRateLimiter(1);
        assertTrue(limiter.tryAcquire("a"));
        assertFalse(limiter.tryAcquire("a"));
        assertTrue(limiter.tryAcquire("b")); // 其他 key 不受影响
    }
}
