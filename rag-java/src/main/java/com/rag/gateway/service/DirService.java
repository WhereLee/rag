package com.rag.gateway.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.dao.EmptyResultDataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Map;

/**
 * 单层目录服务：建/列/重命名/删除（非空禁删）。
 *
 * 隔离原则与文件一致：所有 SQL 强制 WHERE user_id=?，参数来自 JWT 会话；
 * 目录不属于当前用户一律视为不存在（404，不泄露存在性）。
 */
@Service
public class DirService {

    private static final Logger log = LoggerFactory.getLogger(DirService.class);

    private final JdbcTemplate jdbc;

    public DirService(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /** 创建目录：同名冲突抛 IllegalArgumentException("同名目录")。 */
    public Map<String, Object> createDir(Long userId, String name) {
        String clean = cleanName(name);
        try {
            jdbc.update("INSERT INTO user_dir (user_id, name) VALUES (?,?)", userId, clean);
        } catch (DuplicateKeyException e) {
            throw new IllegalArgumentException("同名目录");
        }
        Map<String, Object> dir = jdbc.queryForMap(
                "SELECT id, name, created_at FROM user_dir WHERE user_id=? AND name=?", userId, clean);
        log.info("dir created: user={} dir={} name={}", userId, dir.get("id"), clean);
        return dir;
    }

    public List<Map<String, Object>> listDirs(Long userId) {
        List<Map<String, Object>> dirs = jdbc.queryForList(
                "SELECT d.id, d.name, d.created_at, " +
                "       (SELECT count(*)::int FROM user_file uf WHERE uf.dir_id=d.id AND uf.status=1) AS file_count " +
                "FROM user_dir d WHERE d.user_id=? ORDER BY d.id", userId);
        return dirs;
    }

    /** 重命名：归属校验（404）+ 同名冲突（400"同名目录"）。 */
    public void renameDir(Long userId, Long dirId, String newName) {
        if (!exists(userId, dirId)) {
            throw new IllegalArgumentException("目录不存在");
        }
        String clean = cleanName(newName);
        try {
            jdbc.update("UPDATE user_dir SET name=? WHERE id=? AND user_id=?", clean, dirId, userId);
        } catch (DuplicateKeyException e) {
            throw new IllegalArgumentException("同名目录");
        }
        log.info("dir renamed: user={} dir={} -> {}", userId, dirId, clean);
    }

    /** 删除：归属校验（404）；目录非空禁删（409"目录非空"）。 */
    public void deleteDir(Long userId, Long dirId) {
        if (!exists(userId, dirId)) {
            throw new IllegalArgumentException("目录不存在");
        }
        Integer cnt = jdbc.queryForObject(
                "SELECT count(*)::int FROM user_file WHERE dir_id=? AND status=1", Integer.class, dirId);
        if (cnt != null && cnt > 0) {
            throw new IllegalArgumentException("目录非空，请先移出文件");
        }
        jdbc.update("DELETE FROM user_dir WHERE id=? AND user_id=?", dirId, userId);
        log.info("dir deleted: user={} dir={}", userId, dirId);
    }

    /** 归属校验：目录存在且属于当前用户。 */
    public boolean exists(Long userId, Long dirId) {
        try {
            jdbc.queryForMap("SELECT id FROM user_dir WHERE id=? AND user_id=?", dirId, userId);
            return true;
        } catch (EmptyResultDataAccessException e) {
            return false;
        }
    }

    private static String cleanName(String raw) {
        if (raw == null || raw.isBlank()) {
            throw new IllegalArgumentException("目录名不能为空");
        }
        String name = raw.trim();
        if (name.length() > 100) {
            throw new IllegalArgumentException("目录名过长（上限 100 字符）");
        }
        if (name.contains("/") || name.contains("\\")) {
            throw new IllegalArgumentException("目录名不能包含路径分隔符");
        }
        return name;
    }
}
