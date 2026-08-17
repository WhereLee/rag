package com.rag.gateway.service;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Map;

/**
 * 用户服务：注册 / 登录。
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

    public UserService(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public Map<String, Object> register(String username, String password, String role) {
        if (username == null || username.length() < 2 || password == null || password.length() < 6) {
            throw new IllegalArgumentException("用户名至少2位，密码至少6位");
        }
        Integer exists = jdbc.queryForObject(
                "SELECT count(*)::int FROM kb_user WHERE username=?", Integer.class, username);
        if (exists != null && exists > 0) {
            throw new IllegalArgumentException("用户名已存在");
        }
        String hash = encoder.encode(password);
        jdbc.update("INSERT INTO kb_user (username, password_hash, salt, role) VALUES (?,?,?,?)",
                username, hash, "", role == null ? "user" : role);
        return Map.of("username", username, "created", true);
    }

    public Map<String, Object> login(String username, String password) {
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT username, password_hash, role FROM kb_user WHERE username=?", username);
        if (rows.isEmpty()) {
            throw new IllegalArgumentException("用户名或密码错误");
        }
        Map<String, Object> u = rows.get(0);
        String stored = (String) u.get("password_hash");
        // BCrypt.matches 对存储格式做了防御（盐前缀校验），无需手动常数时间比较
        if (stored == null || !encoder.matches(password, stored)) {
            throw new IllegalArgumentException("用户名或密码错误");
        }
        return Map.of("username", u.get("username"), "role", u.get("role"));
    }
}
