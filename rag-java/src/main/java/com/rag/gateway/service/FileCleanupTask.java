package com.rag.gateway.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.file.FileStore;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.Instant;
import java.util.HashSet;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Stream;

/**
 * 孤儿文件清理（定时 + 手动触发）：
 * 1. 清理超过 TTL 的 *.tmp（上传原子化"写 tmp → 插库 → move"的崩溃残留）
 * 2. 清理正式目录中无 DB 记录（status=1）的文件（崩溃窗口/物理删除失败的残留）
 * 3. 清理回收站中超 TTL 的记录（deleted_at 过期，引用计数随之收敛）
 * 4. 收敛悬空 blob：无任何 user_file 引用的 blob 删物理文件 + 删记录；引用数与实际不一致时修正
 *
 * 删除失败（Windows 文件锁）不静默：记录 WARN，下一轮重试。
 */
@Component
public class FileCleanupTask {

    private static final Logger log = LoggerFactory.getLogger(FileCleanupTask.class);

    private final JdbcTemplate jdbc;
    private final Path storageRoot;
    private final long tmpTtlMs;
    private final long trashTtlMs;
    private final long sessionTtlMs;
    private final double warnFreeRatio;

    public FileCleanupTask(JdbcTemplate jdbc,
                           @Value("${gateway.storage.root:../data/files}") String storageRoot,
                           @Value("${gateway.cleanup.tmp-ttl-ms:3600000}") long tmpTtlMs,
                           @Value("${gateway.cleanup.trash-ttl-ms:2592000000}") long trashTtlMs,
                           @Value("${gateway.cleanup.session-ttl-ms:3600000}") long sessionTtlMs,
                           @Value("${gateway.cleanup.warn-free-ratio:0.10}") double warnFreeRatio) {
        this.jdbc = jdbc;
        this.storageRoot = Paths.get(storageRoot).toAbsolutePath().normalize();
        this.tmpTtlMs = tmpTtlMs;
        this.trashTtlMs = trashTtlMs;
        this.sessionTtlMs = sessionTtlMs;
        this.warnFreeRatio = warnFreeRatio;
    }

    @Scheduled(fixedDelayString = "${gateway.cleanup.interval-ms:3600000}")
    public void sweepScheduled() {
        sweep();
    }

    /** 手动触发入口（运维/测试用），返回清理统计。 */
    public Map<String, Object> sweep() {
        checkAndWarnDisk();
        int tmpCleaned = sweepTmp();
        int orphanCleaned = sweepOrphans();
        int trashCleaned = sweepTrash();
        int blobCleaned = sweepBlobs();
        int sessionCleaned = sweepSessions();
        if (tmpCleaned + orphanCleaned + trashCleaned + blobCleaned + sessionCleaned > 0) {
            log.info("cleanup done: tmp={} orphans={} trash={} blobs={} sessions={}",
                    tmpCleaned, orphanCleaned, trashCleaned, blobCleaned, sessionCleaned);
        }
        return Map.of("tmp_cleaned", tmpCleaned, "orphan_cleaned", orphanCleaned,
                "trash_cleaned", trashCleaned, "blob_cleaned", blobCleaned,
                "session_cleaned", sessionCleaned);
    }

    /**
     * 磁盘剩余空间告警：剩余比例低于阈值时 WARN（随每小时清理任务执行，磁盘快满时提前暴露）。
     */
    private void checkAndWarnDisk() {
        try {
            FileStore store = Files.getFileStore(storageRoot);
            long usable = store.getUsableSpace();
            long total = store.getTotalSpace();
            double ratio = total > 0 ? (double) usable / total : 1.0;
            if (ratio < warnFreeRatio) {
                log.warn("磁盘剩余空间告警: usable={}B total={}B ratio={}% (warn threshold: {}%)",
                        usable, total, String.format("%.1f", ratio * 100), warnFreeRatio * 100);
            }
        } catch (IOException e) {
            log.warn("disk check failed", e);
        }
    }

    /** 清理超 TTL 的 *.tmp 残留。 */
    private int sweepTmp() {
        if (!Files.isDirectory(storageRoot)) {
            return 0;
        }
        int count = 0;
        try (Stream<Path> paths = Files.walk(storageRoot)) {
            for (Path p : (Iterable<Path>) paths::iterator) {
                if (Files.isRegularFile(p) && p.getFileName().toString().endsWith(".tmp")) {
                    if (olderThan(p, tmpTtlMs) && tryDelete(p)) {
                        count++;
                    }
                }
            }
        } catch (IOException e) {
            log.warn("tmp sweep failed", e);
        }
        return count;
    }

    /** 清理正式目录中无 blob 记录的文件（崩溃窗口残留/物理删除失败的残留）。只处理 {userId}/{file} 两级路径。 */
    private int sweepOrphans() {
        if (!Files.isDirectory(storageRoot)) {
            return 0;
        }
        Set<String> known = new HashSet<>(jdbc.queryForList(
                "SELECT stored_name FROM file_blob", String.class));
        int count = 0;
        try (Stream<Path> paths = Files.walk(storageRoot)) {
            for (Path p : (Iterable<Path>) paths::iterator) {
                if (Files.isRegularFile(p) && !p.getFileName().toString().endsWith(".tmp")) {
                    Path rel = storageRoot.relativize(p);
                    if (rel.getNameCount() == 2 && !known.contains(p.getFileName().toString())) {
                        if (tryDelete(p)) {
                            count++;
                        }
                    }
                }
            }
        } catch (IOException e) {
            log.warn("orphan sweep failed", e);
        }
        return count;
    }

    /** 清理回收站中超 TTL 的记录（软删记录直接删除；blob 引用由 sweepBlobs 按实际引用收敛）。 */
    private int sweepTrash() {
        int n = jdbc.update(
                "DELETE FROM user_file WHERE status=0 AND deleted_at < now() - (? || ' milliseconds')::interval",
                trashTtlMs);
        if (n > 0) {
            log.info("trash cleaned: {} records", n);
        }
        return n;
    }

    /**
     * 收敛悬空 blob：无任何 user_file 引用的 blob 删物理文件 + 删记录；
     * 引用数与实际不一致时修正（覆盖崩溃窗口/回滚残留的计数漂移）。
     */
    private int sweepBlobs() {
        List<Map<String, Object>> blobs = jdbc.queryForList(
                "SELECT id, stored_name, owner_user_id, ref_count FROM file_blob");
        int cleaned = 0;
        for (Map<String, Object> b : blobs) {
            long blobId = ((Number) b.get("id")).longValue();
            Integer refs = jdbc.queryForObject(
                    "SELECT count(*)::int FROM user_file WHERE blob_id=?", Integer.class, blobId);
            int actual = refs == null ? 0 : refs;
            int declared = ((Number) b.get("ref_count")).intValue();
            if (actual <= 0) {
                Path p = storageRoot.resolve(String.valueOf(b.get("owner_user_id")))
                        .resolve((String) b.get("stored_name"));
                try {
                    Files.deleteIfExists(p);
                    jdbc.update("DELETE FROM file_blob WHERE id=?", blobId);
                    cleaned++;
                    log.info("blob converged: id={} path={}", blobId, p);
                } catch (IOException e) {
                    log.warn("blob converge delete failed (will retry next round): id={} path={}", blobId, p, e);
                }
            } else if (declared != actual) {
                jdbc.update("UPDATE file_blob SET ref_count=? WHERE id=?", actual, blobId);
                log.info("blob ref_count corrected: id={} {} -> {}", blobId, declared, actual);
            }
        }
        return cleaned;
    }

    /** 清理超时未完成的分片会话：删 tmp 目录 + 记录（chunk 级联删）。 */
    private int sweepSessions() {
        List<Map<String, Object>> expired = jdbc.queryForList(
                "SELECT id FROM upload_session WHERE status='uploading' " +
                "AND updated_at < now() - (? || ' milliseconds')::interval", sessionTtlMs);
        int n = 0;
        for (Map<String, Object> s : expired) {
            long id = ((Number) s.get("id")).longValue();
            Path dir = storageRoot.resolve(".upload-tmp").resolve(String.valueOf(id));
            if (Files.isDirectory(dir)) {
                try (Stream<Path> paths = Files.walk(dir)) {
                    for (Path p : (Iterable<Path>) paths.sorted(Comparator.reverseOrder())::iterator) {
                        Files.deleteIfExists(p);
                    }
                } catch (IOException e) {
                    log.warn("session tmp cleanup failed: {}", dir, e);
                }
            }
            jdbc.update("DELETE FROM upload_session WHERE id=?", id);
            n++;
            log.info("upload session expired & cleaned: id={}", id);
        }
        return n;
    }

    private boolean olderThan(Path p, long ttlMs) {
        try {
            return Files.getLastModifiedTime(p).toInstant().isBefore(Instant.now().minusMillis(ttlMs));
        } catch (IOException e) {
            return false;
        }
    }

    private boolean tryDelete(Path p) {
        try {
            Files.deleteIfExists(p);
            log.info("cleanup removed: {}", p);
            return true;
        } catch (IOException e) {
            log.warn("cleanup delete failed (will retry next round): {}", p, e);
            return false;
        }
    }
}
