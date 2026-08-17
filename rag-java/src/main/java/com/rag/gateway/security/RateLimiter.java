package com.rag.gateway.security;

/**
 * 限流器接口：按 key（用户名等）判断是否放行。
 * 当前实现：InMemoryRateLimiter（单机滑动窗口）；
 * 多实例部署时可加 RedisRateLimiter（Redis ZSET）替换，业务代码无需改动。
 */
public interface RateLimiter {

    /** 尝试获取一次配额。返回 true=放行，false=限流拒绝。 */
    boolean tryAcquire(String key);
}
