package com.rag.gateway.security;

import jakarta.servlet.http.HttpServletRequest;

import java.util.Deque;
import java.util.Iterator;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedDeque;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 认证接口限流器：IP 级频率限制 + 账号锁定机制。
 *
 * 策略：
 * - 注册：每 IP 每分钟 3 次
 * - 登录：每 IP 每分钟 10 次
 * - 登录失败：同一用户名连续失败 5 次后锁定 15 分钟
 */
public class AuthRateLimiter {

    private final Map<String, Deque<Long>> ipWindows = new ConcurrentHashMap<>();
    /** 账号锁定：username -> 解锁时间戳（毫秒） */
    private final Map<String, Long> lockedAccounts = new ConcurrentHashMap<>();
    /** 登录失败计数：username -> 连续失败次数 */
    private final Map<String, AtomicInteger> failCounts = new ConcurrentHashMap<>();
    private final ScheduledExecutorService cleaner;
    private final ClientIpResolver ipResolver;

    private static final int REGISTER_PER_MINUTE = 3;
    private static final int LOGIN_PER_MINUTE = 10;
    private static final int MAX_FAIL_ATTEMPTS = 5;
    private static final long LOCKOUT_MILLIS = 15 * 60 * 1000; // 15 分钟

    public AuthRateLimiter(ClientIpResolver ipResolver) {
        this.ipResolver = ipResolver;
        // 每 10 分钟清理过期数据
        cleaner = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "auth-rate-limiter-cleaner");
            t.setDaemon(true);
            return t;
        });
        cleaner.scheduleAtFixedRate(this::evictStale, 10, 10, TimeUnit.MINUTES);
    }

    /** 注册限流：每 IP 每分钟 3 次 */
    public boolean tryRegister(HttpServletRequest req) {
        return tryAcquireByIp(req, REGISTER_PER_MINUTE);
    }

    /** 登录限流：每 IP 每分钟 10 次 + 账号锁定检查 */
    public LoginResult tryLogin(HttpServletRequest req, String username) {
        // 1. 账号锁定检查
        Long unlockAt = lockedAccounts.get(username);
        if (unlockAt != null && System.currentTimeMillis() < unlockAt) {
            long remainSec = (unlockAt - System.currentTimeMillis()) / 1000;
            return LoginResult.lock(remainSec);
        } else if (unlockAt != null) {
            lockedAccounts.remove(username);
            failCounts.remove(username);
        }

        // 2. IP 限流
        if (!tryAcquireByIp(req, LOGIN_PER_MINUTE)) {
            return LoginResult.rateLimit();
        }

        return LoginResult.allow();
    }

    /** 清除过期数据，防止内存泄漏。 */
    private void evictStale() {
        long now = System.currentTimeMillis();
        long cutoff5m = now - 5 * 60_000;

        // 清除 5 分钟无活动的 IP 窗口
        ipWindows.entrySet().removeIf(entry -> {
            Deque<Long> q = entry.getValue();
            if (q.isEmpty()) return true;
            Long latest = q.peekLast();
            return latest != null && latest < cutoff5m;
        });

        // 清除已过期的锁定账号
        lockedAccounts.entrySet().removeIf(entry -> entry.getValue() < now);

        // 清除无锁定的失败计数
        failCounts.entrySet().removeIf(entry ->
                !lockedAccounts.containsKey(entry.getKey()));
    }

    /** 记录登录失败 */
    public void recordLoginFailure(String username) {
        AtomicInteger count = failCounts.computeIfAbsent(username, k -> new AtomicInteger(0));
        int fails = count.incrementAndGet();
        if (fails >= MAX_FAIL_ATTEMPTS) {
            lockedAccounts.put(username, System.currentTimeMillis() + LOCKOUT_MILLIS);
            count.set(0); // 重置计数，解锁后重新开始
        }
    }

    /** 记录登录成功，清除失败计数 */
    public void recordLoginSuccess(String username) {
        failCounts.remove(username);
        lockedAccounts.remove(username);
    }

    private boolean tryAcquireByIp(HttpServletRequest req, int maxPerMinute) {
        String ip = getClientIp(req);
        long now = System.currentTimeMillis();
        Deque<Long> q = ipWindows.computeIfAbsent(ip, k -> new ConcurrentLinkedDeque<>());

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

    private String getClientIp(HttpServletRequest req) {
        // 坑位 #1 修复：只在直连 IP 属于可信代理时才信任 X-Forwarded-For，
        // 默认（无可信代理配置）忽略 XFF 用直连 IP，伪造 XFF 无法绕过限流
        return ipResolver.resolve(req);
    }

    // ===== 结果类型（record 无状态：规避枚举单例可变字段在并发下的相互覆盖） =====
    public record LoginResult(boolean allowed, boolean rateLimited, boolean locked,
                              long remainSeconds) {
        public static LoginResult allow() {
            return new LoginResult(true, false, false, 0);
        }

        public static LoginResult rateLimit() {
            return new LoginResult(false, true, false, 0);
        }

        public static LoginResult lock(long remainSec) {
            return new LoginResult(false, false, true, remainSec);
        }
    }
}
