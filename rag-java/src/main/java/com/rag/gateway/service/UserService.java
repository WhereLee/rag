package com.rag.gateway.service;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 用户服务：注册 / 登录 / 获取用户 ID。
 *
 * 密码哈希：BCrypt（spring-security-crypto，salt 内置 + 自适应 cost），
 * 替代早期手写 SHA-256+随机盐（单次摘要易受 GPU 暴力破解）。
 * kb_user.salt 列保留但不再写入（BCrypt 自带盐），留作 schema 兼容。
 * 注意：旧 SHA-256 哈希不兼容，本地开发库重新注册即可（升级说明见 docs）。
 */
@Service
public class UserService {

    private final JdbcTemplate jdbc;
    private final BCryptPasswordEncoder encoder = new BCryptPasswordEncoder();
    /** 本地缓存：username -> user_id，避免每次请求查库。登录时填充。 */
    private final ConcurrentHashMap<String, Long> userIdCache = new ConcurrentHashMap<>();

    public UserService(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    private static final java.util.regex.Pattern USERNAME_PATTERN =
            java.util.regex.Pattern.compile("^[a-zA-Z0-9_]{2,32}$");

    /** 密码策略：8-32 位字母、数字或常见符号（与 RegisterRequest 一致，Service 层兜底防御）。 */
    private static final java.util.regex.Pattern PASSWORD_PATTERN =
            java.util.regex.Pattern.compile("^[A-Za-z0-9@#$%^&*._\\-]{8,32}$");

    public Map<String, Object> register(String username, String password) {
        if (username == null || !USERNAME_PATTERN.matcher(username).matches()) {
            throw new IllegalArgumentException("用户名仅允许字母、数字、下划线，2-32位");
        }
        if (password == null || !PASSWORD_PATTERN.matcher(password).matches()) {
            throw new IllegalArgumentException("密码需为8-32位字母、数字或常见符号（@#$%^&*._-）");
        }
        // 安全加固：注册接口强制 role="user"，忽略前端传入值
        // 管理员账号仅通过 init_db.sql 初始化脚本创建
        Integer exists = jdbc.queryForObject(
                "SELECT count(*)::int FROM kb_user WHERE username=?", Integer.class, username);
        if (exists != null && exists > 0) {
            throw new IllegalArgumentException("用户名已存在");
        }
        String hash = encoder.encode(password);
        try {
            jdbc.update("INSERT INTO kb_user (username, password_hash, role) VALUES (?,?,?)",
                    username, hash, "user");
        } catch (org.springframework.dao.DuplicateKeyException e) {
            // 并发竞态兜底：SELECT 预检后另一个请求已插入同用户名，唯一约束冲突 → 友好提示
            throw new IllegalArgumentException("用户名已存在");
        }
        return Map.of("username", username, "created", true);
    }

    public Map<String, Object> login(String username, String password) {
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT id, username, password_hash, role FROM kb_user WHERE username=?", username);
        if (rows.isEmpty()) {
            throw new IllegalArgumentException("用户名或密码错误");
        }
        Map<String, Object> u = rows.get(0);
        String stored = (String) u.get("password_hash");
        // BCrypt.matches 对存储格式做了防御（盐前缀校验），无需手动常数时间比较
        if (stored == null || !encoder.matches(password, stored)) {
            throw new IllegalArgumentException("用户名或密码错误");
        }
        // 登录时填充缓存
        Long userId = ((Number) u.get("id")).longValue();
        userIdCache.put(username, userId);
        return Map.of("id", userId, "username", u.get("username"), "role", u.get("role"));
    }

    /** 获取用户 ID（本地缓存优先，未命中时查库）。 */
    public Long getUserIdByUsername(String username) {
        Long cached = userIdCache.get(username);
        if (cached != null) {
            return cached;
        }
        Long id = jdbc.queryForObject(
                "SELECT id FROM kb_user WHERE username=?", Long.class, username);
        if (id != null) {
            userIdCache.put(username, id);
        }
        return id;
    }
}
