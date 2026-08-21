package com.rag.gateway.service;

import com.rag.gateway.security.MagicBytes;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.EmptyResultDataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.jdbc.support.KeyHolder;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.SequenceInputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.FileStore;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.security.DigestInputStream;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.PreparedStatement;
import java.text.Normalizer;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * 用户文件服务：上传（秒传去重）+ 落盘 + 列表/删除/回收站/下载。
 *
 * 多用户隔离原则（本步正确性核心）：
 * - 所有 SQL 强制 WHERE user_id=?，参数来自服务端会话，不接受前端传参
 * - 删除/下载先做归属校验，文件不属于当前用户一律视为不存在（404，不泄露存在性）
 *
 * 秒传体系（阶段2）：
 * - file_blob：物理文件（sha256 唯一，owner_user_id=首个上传者目录）；user_file：用户引用
 * - 上传顺序：写 tmp → 算 hash → move 正式名 → INSERT blob (ON CONFLICT 兜底并发) → 复用/计数 → 插 user_file
 * - blob 记录存在 ⇒ 物理文件已就位（move 先于 INSERT）；悬空 blob 由清理任务收敛
 */
@Service
public class FileService {

    private static final Logger log = LoggerFactory.getLogger(FileService.class);
    private static final long MAX_FILE_SIZE = 50L * 1024 * 1024;

    private final JdbcTemplate jdbc;
    private final Path storageRoot;
    private final Path previewDir;
    private final long quotaBytes;
    private final long minFreeBytes;
    private final double minFreeRatio;
    private final Set<String> allowedExts;
    private final ObjectMapper mapper = new ObjectMapper();

    public FileService(JdbcTemplate jdbc,
                       @Value("${gateway.storage.root:../data/files}") String storageRoot,
                       @Value("${gateway.parse.preview-dir:../data/parsed}") String previewDir,
                       @Value("${gateway.upload.quota-bytes:2147483648}") long quotaBytes,
                       @Value("${gateway.upload.min-free-bytes:1073741824}") long minFreeBytes,
                       @Value("${gateway.upload.min-free-ratio:0.05}") double minFreeRatio,
                       @Value("${gateway.upload.allowed-extensions:txt,md,pdf,docx,xlsx,pptx,png,jpg,jpeg,webp}") String allowedExtensions) {
        this.jdbc = jdbc;
        this.storageRoot = Paths.get(storageRoot).toAbsolutePath().normalize();
        this.previewDir = Paths.get(previewDir).toAbsolutePath().normalize();
        this.quotaBytes = quotaBytes;
        this.minFreeBytes = minFreeBytes;
        this.minFreeRatio = minFreeRatio;
        this.allowedExts = new HashSet<>();
        for (String e : allowedExtensions.split(",")) {
            String t = e.trim().toLowerCase();
            if (!t.isEmpty()) {
                // extractExt 输出带点（如 .txt），集合统一带点格式
                this.allowedExts.add("." + t);
            }
        }
    }

    /** 扩展名白名单粗筛：不在白名单直接拒绝（解析不了的格式不让上传）。 */
    private void checkAllowedExtension(String ext) {
        if (!allowedExts.contains(ext)) {
            throw new IllegalArgumentException("不支持的文件类型");
        }
    }

    /**
     * 磁盘空间检查：剩余空间低于绝对阈值/比例阈值，或不足以容纳本次写入 → 拒绝。
     * neededBytes=0 表示仅检查当前状态。检查失败时放行（不因磁盘检查自身异常阻断上传），但记录 WARN。
     */
    private void checkDiskSpace(long neededBytes) {
        try {
            FileStore store = Files.getFileStore(storageRoot);
            long usable = store.getUsableSpace();
            long total = store.getTotalSpace();
            boolean low = usable < minFreeBytes
                    || (total > 0 && (double) usable / total < minFreeRatio)
                    || (neededBytes > 0 && usable < neededBytes);
            if (low) {
                log.warn("disk low, upload rejected: usable={}B total={}B needed={}B minFree={}B minRatio={}",
                        usable, total, neededBytes, minFreeBytes, minFreeRatio);
                throw new IllegalArgumentException("磁盘空间不足，请稍后再试");
            }
        } catch (IOException e) {
            log.warn("disk space check skipped: {}", e.toString());
        }
    }

    /**
     * 保存上传文件：魔数校验 → 配额 → 写 tmp（边算 sha256）→ move → blob 去重 → 插 user_file。
     * 并发同 hash：INSERT ON CONFLICT DO NOTHING 兜底，复用已有 blob 并删除自己的文件。
     * 返回 {id, filename, file_size, duplicate_name}。dirId 可为 null（根目录）。
     */
    public Map<String, Object> saveFile(Long userId, MultipartFile file, Long dirId) {
        if (file == null || file.isEmpty()) {
            throw new IllegalArgumentException("文件内容为空");
        }
        checkDirOwned(userId, dirId);        long size = file.getSize();
        if (size <= 0) {
            throw new IllegalArgumentException("文件大小无效");
        }
        if (size > MAX_FILE_SIZE) {
            throw new IllegalArgumentException("文件过大（上限 50MB）");
        }
        String originalName = sanitizeFilename(file.getOriginalFilename());
        String ext = extractExt(originalName);
        checkAllowedExtension(ext);
        checkDiskSpace(size);
        checkQuota(userId, size);
        boolean dupName = existsActiveName(userId, originalName, null);
        String storedName = UUID.randomUUID().toString().replace("-", "") + ext;
        Path userDir = storageRoot.resolve(String.valueOf(userId));
        Path tmp = userDir.resolve(storedName + ".tmp");
        try {
            Files.createDirectories(userDir);
            MessageDigest md;
            try {
                md = MessageDigest.getInstance("SHA-256");
            } catch (NoSuchAlgorithmException e) {
                throw new IllegalStateException("SHA-256 不可用", e);
            }
            try (InputStream in = file.getInputStream()) {
                byte[] head = new byte[1024];
                int headLen = in.readNBytes(head, 0, head.length);
                if (!MagicBytes.matches(head, headLen, ext)) {
                    throw new IllegalArgumentException("文件类型与扩展名不符");
                }
                // Digest 包在拼接流外层：读出的每个字节（含文件头）都进 sha256
                InputStream src = new DigestInputStream(
                        new SequenceInputStream(new ByteArrayInputStream(head, 0, headLen), in), md);
                Files.copy(src, tmp, StandardCopyOption.REPLACE_EXISTING);
            }
            String hash = HexFormat.of().formatHex(md.digest());
            Map<String, Object> saved = registerTmpFile(userId, tmp, storedName, originalName, size,
                    file.getContentType() == null ? "" : file.getContentType(), hash, dirId);
            saved.put("duplicate_name", dupName);
            return saved;
        } catch (IllegalArgumentException e) {
            try {
                Files.deleteIfExists(tmp);
            } catch (IOException ignored) {
            }
            throw e;
        } catch (IOException | RuntimeException e) {
            try {
                Files.deleteIfExists(tmp);
            } catch (IOException ignored) {
            }
            log.error("file save failed: user={} name={}", userId, originalName, e);
            throw new IllegalStateException("文件存储失败，请稍后重试", e);
        }
    }

    /**
     * 公共入库：move tmp→正式名 + blob 内容去重（ON CONFLICT 并发兜底）+ 插 user_file。
     * saveFile 与分片 complete 共用；返回 {id, filename, file_size}。
     */
    private Map<String, Object> registerTmpFile(Long userId, Path tmp, String storedName, String originalName,
                                                long size, String contentType, String hash, Long dirId) {
        Path target = storageRoot.resolve(String.valueOf(userId)).resolve(storedName);
        Number blobId = null;
        Number id = null;
        try {
            Files.createDirectories(target.getParent());
            moveAtomic(tmp, target);
            jdbc.update("INSERT INTO file_blob (file_hash, stored_name, file_size, ref_count, owner_user_id) " +
                    "VALUES (?,?,?,1,?) ON CONFLICT (file_hash) DO NOTHING", hash, storedName, size, userId);
            Map<String, Object> blob = jdbc.queryForMap(
                    "SELECT id, stored_name FROM file_blob WHERE file_hash=?", hash);
            blobId = (Number) blob.get("id");
            if (!storedName.equals(blob.get("stored_name"))) {
                Files.deleteIfExists(target);
                jdbc.update("UPDATE file_blob SET ref_count=ref_count+1 WHERE id=?", blobId.longValue());
            }
            id = insertUserFile(userId, blobId, originalName, size, contentType, dirId);
            if (id == null) {
                throw new IllegalStateException("文件记录插入失败");
            }
            enqueueParse(id.longValue());  // 整传/分片完成统一入队：文件落库即触发解析
            log.info("file registered: user={} file={} size={} blob={}", userId, originalName, size, blobId);
            Map<String, Object> result = new java.util.HashMap<>();
            result.put("id", id.longValue());
            result.put("filename", originalName);
            result.put("file_size", size);
            return result;
        } catch (Exception e) {
            cleanupSave(id, tmp, target);
            if (e instanceof RuntimeException re) {
                throw re;
            }
            throw new IllegalStateException("文件存储失败，请稍后重试", e);
        }
    }

    /**
     * 秒传：check-hash 命中后直接建引用（不传字节）。
     * 返回 null 表示未命中（调用方应走正常上传）；命中返回 {id, filename, file_size, duplicate_name}。
     */
    public Map<String, Object> instantUpload(Long userId, String hash, long size, String filename, Long dirId) {
        Map<String, Object> blob;
        try {
            blob = jdbc.queryForMap(
                    "SELECT id, stored_name FROM file_blob WHERE file_hash=? AND file_size=?", hash, size);
        } catch (EmptyResultDataAccessException e) {
            return null;
        }
        checkDirOwned(userId, dirId);
        String originalName = sanitizeFilename(filename);
        boolean dupName = existsActiveName(userId, originalName, null);
        jdbc.update("UPDATE file_blob SET ref_count=ref_count+1 WHERE id=?", blob.get("id"));
        Number id = insertUserFile(userId, (Number) blob.get("id"), originalName, size, "", dirId);
        if (id == null) {
            throw new IllegalStateException("文件记录插入失败");
        }
        enqueueParse(id.longValue());  // 秒传同样入队（解析产物按 user_file 维度）
        log.info("file instant-uploaded: user={} file={} blob={}", userId, originalName, blob.get("id"));
        return Map.of("id", id.longValue(), "filename", originalName, "file_size", size,
                "duplicate_name", dupName);
    }

    /**
     * 当前用户的文件列表（分页，时间倒序）。dirId 为 null 时列出全部（兼容旧行为）。
     * 返回 {items, total, page, pageSize}；items 每项带 ext（展示用友好扩展名）。
     */
    public Map<String, Object> listFiles(Long userId, int page, int pageSize, Long dirId) {
        int offset = (page - 1) * pageSize;
        List<Map<String, Object>> items;
        Long total;
        if (dirId == null) {
            items = jdbc.queryForList(
                    "SELECT uf.id, uf.filename, uf.file_size, uf.content_type, uf.created_at, " +
                    "       pt.status AS parse_status, pt.error AS parse_error, pt.node_count AS parse_node_count " +
                    "FROM user_file uf " +
                    "LEFT JOIN parse_tasks pt ON pt.file_id = uf.id " +
                    "WHERE uf.user_id=? AND uf.status=1 ORDER BY uf.id DESC LIMIT ? OFFSET ?",
                    userId, pageSize, offset);
            total = jdbc.queryForObject(
                    "SELECT count(*)::bigint FROM user_file WHERE user_id=? AND status=1",
                    Long.class, userId);
        } else {
            if (!dirOwned(userId, dirId)) {
                throw new IllegalArgumentException("目录不存在");
            }
            items = jdbc.queryForList(
                    "SELECT uf.id, uf.filename, uf.file_size, uf.content_type, uf.created_at, " +
                    "       pt.status AS parse_status, pt.error AS parse_error, pt.node_count AS parse_node_count " +
                    "FROM user_file uf " +
                    "LEFT JOIN parse_tasks pt ON pt.file_id = uf.id " +
                    "WHERE uf.user_id=? AND uf.status=1 AND uf.dir_id=? ORDER BY uf.id DESC LIMIT ? OFFSET ?",
                    userId, dirId, pageSize, offset);
            total = jdbc.queryForObject(
                    "SELECT count(*)::bigint FROM user_file WHERE user_id=? AND status=1 AND dir_id=?",
                    Long.class, userId, dirId);
        }
        for (Map<String, Object> row : items) {
            row.put("ext", extractExt((String) row.get("filename")));
            if (row.get("parse_status") == null) {
                row.put("parse_status", "pending");  // 无任务记录视为待解析（历史文件）
            }
        }
        return Map.of("items", items, "total", total, "page", page, "pageSize", pageSize);
    }

    /**
     * 解析入队：幂等（file_id 主键，ON CONFLICT 不覆盖已有状态）。
     */
    private void enqueueParse(long fileId) {
        jdbc.update("INSERT INTO parse_tasks (file_id) VALUES (?) ON CONFLICT (file_id) DO NOTHING", fileId);
    }

    /**
     * 手动重新解析：仅允许失败/部分失败文件，置回 pending（attempt 保留，防刷）。
     */
    public void reparse(Long userId, Long fileId) {
        Map<String, Object> row = findOwned(userId, fileId);
        if (row == null) {
            throw new IllegalArgumentException("文件不存在");
        }
        String status = jdbc.queryForObject(
                "SELECT status FROM parse_tasks WHERE file_id=?", String.class, fileId);
        if (status == null) {
            jdbc.update("INSERT INTO parse_tasks (file_id, status) VALUES (?, 'pending')", fileId);
            return;
        }
        if (!"failed".equals(status) && !"partial".equals(status)) {
            throw new IllegalArgumentException("仅失败或部分失败的文件可重新解析");
        }
        // attempt 重置：手动重试不受自动重试上限限制（worker 按 attempt < MAX 拉取）
        jdbc.update("UPDATE parse_tasks SET status='pending', attempt=0, updated_at=now() WHERE file_id=?", fileId);
        log.info("parse re-enqueued: file={} (prev={})", fileId, status);
    }

    /**
     * 在线预览：读解析产物 JSON（rag/data/parsed/{fileId}.json），按节点顺序拼文本。
     * 产物缺失/未解析 → 404；解析失败 → 422 带原因。
     */
    public Map<String, Object> preview(Long userId, Long fileId) {
        Map<String, Object> row = findOwned(userId, fileId);
        if (row == null) {
            throw new IllegalArgumentException("文件不存在");
        }
        Path json = previewDir.resolve(fileId + ".json");
        if (!Files.exists(json)) {
            String status = jdbc.queryForObject(
                    "SELECT status FROM parse_tasks WHERE file_id=?", String.class, fileId);
            if (status == null || "pending".equals(status) || "parsing".equals(status)) {
                return Map.of("previewable", false, "reason", "尚未解析完成");
            }
            return Map.of("previewable", false, "reason", "解析产物缺失");
        }
        try {
            JsonNode root = mapper.readTree(Files.readString(json, StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder();
            int count = 0;
            for (JsonNode node : root.path("nodes")) {
                sb.append(node.path("text").asText()).append("\n\n");
                count++;
            }
            return Map.of("previewable", true, "text", sb.toString(),
                    "node_count", count,
                    "parse_status", root.path("status").asText());
        } catch (Exception e) {
            log.warn("preview read failed: file={}: {}", fileId, e.toString());
            return Map.of("previewable", false, "reason", "预览内容读取失败");
        }
    }

    /**
     * 分片上传初始化：创建上传会话（落盘目录 data/files/.upload-tmp/{uploadId}/）。
     * 每用户最多 5 个进行中的会话（防 tmp 泛滥）；超时会话由清理任务收敛。
     */
    public long createUploadSession(Long userId, String hash, long size, String filename,
                                    int chunkCount, long chunkSize, Long dirId) {
        checkDiskSpace(size);
        checkDirOwned(userId, dirId);
        Integer active = jdbc.queryForObject(
                "SELECT count(*)::int FROM upload_session WHERE user_id=? AND status='uploading'",
                Integer.class, userId);
        if (active != null && active >= 5) {
            throw new IllegalArgumentException("同时进行的上传任务过多，请先完成或等待超时清理");
        }
        String clean = sanitizeFilename(filename);
        checkAllowedExtension(extractExt(clean));
        KeyHolder kh = new GeneratedKeyHolder();
        jdbc.update(con -> {
            PreparedStatement ps = con.prepareStatement(
                    "INSERT INTO upload_session (user_id, file_hash, filename, file_size, chunk_size, chunk_count, dir_id) " +
                    "VALUES (?,?,?,?,?,?,?)", new String[]{"id"});
            ps.setLong(1, userId);
            ps.setString(2, hash);
            ps.setString(3, clean);
            ps.setLong(4, size);
            ps.setLong(5, chunkSize);
            ps.setInt(6, chunkCount);
            if (dirId == null) {
                ps.setNull(7, java.sql.Types.BIGINT);
            } else {
                ps.setLong(7, dirId);
            }
            return ps;
        }, kh);
        Number id = kh.getKey();
        if (id == null) {
            throw new IllegalStateException("上传会话创建失败");
        }
        log.info("upload session created: user={} session={} size={} chunks={}", userId, id, size, chunkCount);
        return id.longValue();
    }

    /**
     * 分片落盘：校验会话归属/序号/单片大小；同片重传（断点续传重试）覆盖旧片。
     */
    public void saveChunk(Long userId, long uploadId, int index, MultipartFile file) {
        Map<String, Object> s = findSession(userId, uploadId);
        if ("completed".equals(s.get("status"))) {
            throw new IllegalArgumentException("该上传任务已完成");
        }
        int chunkCount = ((Number) s.get("chunk_count")).intValue();
        if (index < 0 || index >= chunkCount) {
            throw new IllegalArgumentException("分片序号非法");
        }
        long chunkSize = ((Number) s.get("chunk_size")).longValue();
        if (file.getSize() > chunkSize) {
            throw new IllegalArgumentException("分片大小超过限制");
        }
        long size = file.getSize();
        Path dir = chunkDir(uploadId);
        try {
            Files.createDirectories(dir);
            try (InputStream in = file.getInputStream()) {
                Files.copy(in, dir.resolve(index + ".part"), StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (IOException e) {
            throw new IllegalStateException("分片保存失败", e);
        }
        jdbc.update("INSERT INTO upload_chunk (session_id, chunk_index, chunk_size) VALUES (?,?,?) " +
                "ON CONFLICT (session_id, chunk_index) DO UPDATE SET chunk_size=EXCLUDED.chunk_size",
                uploadId, index, size);
        jdbc.update("UPDATE upload_session SET updated_at=now() WHERE id=?", uploadId);
        log.debug("chunk saved: session={} index={} size={}", uploadId, index, size);
    }

    /** 分片状态（断点续传用）：已传分片列表。 */
    public Map<String, Object> sessionStatus(Long userId, long uploadId) {
        Map<String, Object> s = findSession(userId, uploadId);
        List<Integer> uploaded = jdbc.queryForList(
                "SELECT chunk_index FROM upload_chunk WHERE session_id=? ORDER BY chunk_index",
                Integer.class, uploadId);
        return Map.of("upload_id", uploadId, "chunk_count", s.get("chunk_count"),
                "uploaded", uploaded, "status", s.get("status"));
    }

    /**
     * 分片合并 + 校验（数量/大小/整体 hash/魔数/配额）+ 入库（blob 去重体系）。
     * 校验失败保留分片，可修正后重试 complete（幂等）。
     */
    public Map<String, Object> completeUpload(Long userId, long uploadId) {
        Map<String, Object> s = findSession(userId, uploadId);
        if ("completed".equals(s.get("status"))) {
            throw new IllegalArgumentException("该上传任务已完成");
        }
        int chunkCount = ((Number) s.get("chunk_count")).intValue();
        long fileSize = ((Number) s.get("file_size")).longValue();
        String hash = (String) s.get("file_hash");
        String filename = (String) s.get("filename");
        Path dir = chunkDir(uploadId);
        // 校验：分片数量齐全 + 总大小与声明一致
        List<Map<String, Object>> chunks = jdbc.queryForList(
                "SELECT chunk_index, chunk_size FROM upload_chunk WHERE session_id=? ORDER BY chunk_index", uploadId);
        if (chunks.size() != chunkCount) {
            throw new IllegalArgumentException("分片不完整");
        }
        long sum = 0;
        for (Map<String, Object> c : chunks) {
            sum += ((Number) c.get("chunk_size")).longValue();
        }
        if (sum != fileSize) {
            throw new IllegalArgumentException("分片大小与声明不符");
        }
        // 合并前再查一次磁盘（分片传输期间磁盘可能已变化）
        checkDiskSpace(fileSize);
        // 合并
        Path merged = dir.resolve("merged");
        log.info("complete merge start: session={} chunks={} sum={} expect={}", uploadId, chunks.size(), sum, fileSize);
        try (java.io.OutputStream out = Files.newOutputStream(merged)) {
            for (int i = 0; i < chunkCount; i++) {
                Files.copy(dir.resolve(i + ".part"), out);
            }
        } catch (IOException e) {
            log.error("merge failed: session={} dir={}", uploadId, dir, e);
            throw new IllegalStateException("分片合并失败", e);
        }
        try {
            log.info("merge done: session={} mergedSize={}", uploadId, Files.size(merged));
        } catch (IOException ignored) {
        }
        // 整体 hash 复验（防分片被篡改）
        try {
            String actual = sha256File(merged);
            if (!hash.equals(actual)) {
                throw new IllegalArgumentException("文件校验失败（内容与声明不一致）");
            }
            // 魔数校验
            try (InputStream in = Files.newInputStream(merged)) {
                byte[] head = in.readNBytes(1024);
                if (!MagicBytes.matches(head, head.length, extractExt(filename))) {
                    throw new IllegalArgumentException("文件类型与扩展名不符");
                }
            }
        } catch (IOException e) {
            throw new IllegalStateException("文件校验失败", e);
        }
        checkQuota(userId, fileSize);
        String ext = extractExt(filename);
        String storedName = UUID.randomUUID().toString().replace("-", "") + ext;
        boolean dupName = existsActiveName(userId, filename, null);
        Long dirId = s.get("dir_id") == null ? null : ((Number) s.get("dir_id")).longValue();
        Map<String, Object> saved = registerTmpFile(userId, merged, storedName, filename, fileSize, "", hash, dirId);
        jdbc.update("UPDATE upload_session SET status='completed', updated_at=now() WHERE id=?", uploadId);
        deleteChunkDir(uploadId);
        saved.put("duplicate_name", dupName);
        log.info("upload completed: user={} session={} file={}", userId, uploadId, filename);
        return saved;
    }

    /** 回收站列表（软删记录，时间倒序）。 */
    public List<Map<String, Object>> listTrash(Long userId) {
        List<Map<String, Object>> items = jdbc.queryForList(
                "SELECT id, filename, file_size, content_type, deleted_at " +
                "FROM user_file WHERE user_id=? AND status=0 ORDER BY deleted_at DESC", userId);
        for (Map<String, Object> row : items) {
            row.put("ext", extractExt((String) row.get("filename")));
        }
        return items;
    }

    /**
     * 重命名：归属校验（404）→ 同名冲突检查（400）→ 更新。
     * 参数合法性（非空/长度）由 Controller 前置校验；Service 只处理业务语义。
     */
    public void renameFile(Long userId, Long fileId, String newName) {
        Map<String, Object> row = findOwned(userId, fileId);
        if (row == null) {
            throw new IllegalArgumentException("文件不存在");
        }
        String clean = sanitizeFilename(newName);
        if (existsActiveName(userId, clean, fileId)) {
            throw new IllegalArgumentException("已存在同名文件");
        }
        jdbc.update("UPDATE user_file SET filename=? WHERE id=?", clean, fileId);
        log.info("file renamed: user={} file={} -> {}", userId, fileId, clean);
    }

    /**
     * 删除：归属校验 → 软删记录（进回收站）→ blob 引用减一 → 归零删物理文件。
     * 返回 true=物理文件已删除；false=物理删除失败（记录已软删，文件由清理任务兜底重试）。
     * 文件不存在或不属于该用户抛 IllegalArgumentException。
     */
    public boolean deleteFile(Long userId, Long fileId) {
        Map<String, Object> row = findOwned(userId, fileId);
        if (row == null) {
            throw new IllegalArgumentException("文件不存在");
        }
        long blobId = ((Number) row.get("blob_id")).longValue();
        jdbc.update("UPDATE user_file SET status=0, deleted_at=now() WHERE id=?", fileId);
        // 文件软删 → 关联问答存档失效（file_ids 含该文件的行置 invalidated，避免 L1 直返已删文件的旧答案）；
        // 重新上传/恢复后，再次问答会走全链路并重建缓存（ON CONFLICT DO UPDATE 重置 invalidated=FALSE）
        jdbc.update("UPDATE qa_cache SET invalidated=TRUE WHERE user_id=? AND ? = ANY(file_ids)", userId, fileId);
        boolean removed = releaseBlobRef(blobId, storageRoot.resolve(String.valueOf(userId)));
        log.info("file deleted (to trash): user={} file={} blob={} physicalRemoved={}", userId, fileId, blobId, removed);
        return removed;
    }

    /**
     * 移动文件到目录：归属校验（404）+ 目录归属校验（404）+ 更新。dirId 为 null = 移回根目录。
     */
    public void moveFile(Long userId, Long fileId, Long dirId) {
        if (findOwned(userId, fileId) == null) {
            throw new IllegalArgumentException("文件不存在");
        }
        checkDirOwned(userId, dirId);
        jdbc.update("UPDATE user_file SET dir_id=? WHERE id=? AND user_id=?", dirId, fileId, userId);
        log.info("file moved: user={} file={} dir={}", userId, fileId, dirId);
    }

    /** 恢复回收站文件：blob 仍在才可恢复（过期清理后 → 400"已过期"）。 */
    public void restoreFile(Long userId, Long fileId) {
        Map<String, Object> row;
        try {
            row = jdbc.queryForMap(
                    "SELECT id, blob_id FROM user_file WHERE id=? AND user_id=? AND status=0", fileId, userId);
        } catch (EmptyResultDataAccessException e) {
            throw new IllegalArgumentException("文件不存在");
        }
        long blobId = ((Number) row.get("blob_id")).longValue();
        Integer cnt = jdbc.queryForObject(
                "SELECT count(*)::int FROM file_blob WHERE id=?", Integer.class, blobId);
        if (cnt == null || cnt == 0) {
            throw new IllegalArgumentException("文件已过期，无法恢复");
        }
        jdbc.update("UPDATE user_file SET status=1, deleted_at=NULL WHERE id=?", fileId);
        jdbc.update("UPDATE file_blob SET ref_count=ref_count+1 WHERE id=?", blobId);
        log.info("file restored: user={} file={} blob={}", userId, fileId, blobId);
    }

    /** 下载用：返回归属文件信息（filename/file_size/path）。不存在或不属于该用户返回 null。 */
    public Map<String, Object> getOwnedFile(Long userId, Long fileId) {
        Map<String, Object> row = findOwned(userId, fileId);
        if (row == null) {
            return null;
        }
        try {
            // 物理文件在 owner 目录（秒传共享时与当前用户目录可能不同），必须按 blob.owner_user_id 定位
            Map<String, Object> blob = jdbc.queryForMap(
                    "SELECT stored_name, owner_user_id FROM file_blob WHERE id=?", row.get("blob_id"));
            row.put("path", storageRoot.resolve(String.valueOf(blob.get("owner_user_id")))
                    .resolve((String) blob.get("stored_name")));
            return row;
        } catch (EmptyResultDataAccessException e) {
            return null; // blob 已被清理（极端情况），视为不存在
        }
    }

    // ---------- 内部 ----------

    /** blob 引用减一；归零时按 owner 目录删除物理文件。返回物理删除是否成功（未归零视为成功）。 */
    private boolean releaseBlobRef(long blobId, Path fallbackDir) {
        Long ref = jdbc.queryForObject(
                "SELECT ref_count FROM file_blob WHERE id=?", Long.class, blobId);
        if (ref == null) {
            return true; // blob 已不存在（已被清理）
        }
        long newRef = ref - 1;
        jdbc.update("UPDATE file_blob SET ref_count=? WHERE id=?", newRef, blobId);
        if (newRef > 0) {
            return true;
        }
        Map<String, Object> blob = jdbc.queryForMap(
                "SELECT stored_name, owner_user_id FROM file_blob WHERE id=?", blobId);
        Path path = storageRoot.resolve(String.valueOf(blob.get("owner_user_id")))
                .resolve((String) blob.get("stored_name"));
        try {
            boolean removed = Files.deleteIfExists(path);
            jdbc.update("DELETE FROM file_blob WHERE id=?", blobId);
            if (!removed) {
                log.warn("blob file already gone: blob={} path={}", blobId, path);
            }
            return true;
        } catch (IOException e) {
            // 物理删除失败：保留 blob 记录（ref=0），由清理任务 sweepBlobs 重试
            log.warn("blob physical delete failed, cleanup task will retry: blob={} path={}", blobId, path, e);
            return false;
        }
    }

    private Map<String, Object> findOwned(Long userId, Long fileId) {
        try {
            return jdbc.queryForMap(
                    "SELECT filename, blob_id, file_size FROM user_file " +
                    "WHERE id=? AND user_id=? AND status=1", fileId, userId);
        } catch (EmptyResultDataAccessException e) {
            return null;
        }
    }

    private Number insertUserFile(Long userId, Number blobId, String filename, long size, String contentType,
                                  Long dirId) {
        KeyHolder kh = new GeneratedKeyHolder();
        jdbc.update(con -> {
            PreparedStatement ps = con.prepareStatement(
                    "INSERT INTO user_file (user_id, blob_id, filename, file_size, content_type, dir_id) " +
                    "VALUES (?,?,?,?,?,?)", new String[]{"id"});
            ps.setLong(1, userId);
            ps.setLong(2, blobId.longValue());
            ps.setString(3, filename);
            ps.setLong(4, size);
            ps.setString(5, contentType);
            if (dirId == null) {
                ps.setNull(6, java.sql.Types.BIGINT);
            } else {
                ps.setLong(6, dirId);
            }
            return ps;
        }, kh);
        return kh.getKey();
    }

    /** 目录归属校验：不存在或不属于当前用户 → IllegalArgumentException("目录不存在")。dirId 为 null 跳过。 */
    private void checkDirOwned(Long userId, Long dirId) {
        if (dirId == null) {
            return;
        }
        if (!dirOwned(userId, dirId)) {
            throw new IllegalArgumentException("目录不存在");
        }
    }

    private boolean dirOwned(Long userId, Long dirId) {
        Integer cnt = jdbc.queryForObject(
                "SELECT count(*)::int FROM user_dir WHERE id=? AND user_id=?", Integer.class, dirId, userId);
        return cnt != null && cnt > 0;
    }

    private void checkQuota(Long userId, long size) {
        long used = jdbc.queryForObject(
                "SELECT COALESCE(SUM(file_size),0)::bigint FROM user_file WHERE user_id=? AND status=1",
                Long.class, userId);
        if (used + size > quotaBytes) {
            throw new IllegalArgumentException("存储空间不足");
        }
    }

    private boolean existsActiveName(Long userId, String filename, Long excludeId) {
        Integer cnt;
        if (excludeId == null) {
            cnt = jdbc.queryForObject(
                    "SELECT count(*)::int FROM user_file WHERE user_id=? AND filename=? AND status=1",
                    Integer.class, userId, filename);
        } else {
            cnt = jdbc.queryForObject(
                    "SELECT count(*)::int FROM user_file WHERE user_id=? AND filename=? AND status=1 AND id<>?",
                    Integer.class, userId, filename, excludeId);
        }
        return cnt != null && cnt > 0;
    }

    /** 保存失败清理：删 tmp/正式文件 + 回滚 user_file 记录；blob 残留由清理任务收敛。 */
    private void cleanupSave(Number id, Path tmp, Path target) {
        try {
            Files.deleteIfExists(tmp);
        } catch (IOException ignored) {
        }
        try {
            Files.deleteIfExists(target);
        } catch (IOException ignored) {
        }
        if (id != null) {
            try {
                jdbc.update("DELETE FROM user_file WHERE id=?", id.longValue());
            } catch (RuntimeException ignored) {
            }
        }
    }

    private void moveAtomic(Path tmp, Path target) throws IOException {
        try {
            Files.move(tmp, target, StandardCopyOption.ATOMIC_MOVE);
        } catch (AtomicMoveNotSupportedException e) {
            Files.move(tmp, target, StandardCopyOption.REPLACE_EXISTING);
        }
    }

    /** 分片会话查询：归属校验（不属于当前用户视为不存在 → 404）。 */
    private Map<String, Object> findSession(Long userId, long uploadId) {
        try {
            return jdbc.queryForMap(
                    "SELECT id, file_hash, filename, file_size, chunk_size, chunk_count, status, dir_id " +
                    "FROM upload_session WHERE id=? AND user_id=?", uploadId, userId);
        } catch (EmptyResultDataAccessException e) {
            throw new IllegalArgumentException("上传任务不存在");
        }
    }

    /** 分片临时目录：data/files/.upload-tmp/{uploadId}/（三级路径，不受孤儿扫描影响）。 */
    private Path chunkDir(long uploadId) {
        return storageRoot.resolve(".upload-tmp").resolve(String.valueOf(uploadId));
    }

    /** 计算文件 sha256（分片合并后整体复验用）。 */
    private String sha256File(Path p) throws IOException {
        MessageDigest md;
        try {
            md = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 不可用", e);
        }
        try (InputStream in = Files.newInputStream(p)) {
            byte[] buf = new byte[64 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) {
                md.update(buf, 0, n);
            }
        }
        return HexFormat.of().formatHex(md.digest());
    }

    /** 递归删除分片目录（complete 成功/会话超时清理）。 */
    private void deleteChunkDir(long uploadId) {
        Path dir = chunkDir(uploadId);
        if (!Files.isDirectory(dir)) {
            return;
        }
        try (java.util.stream.Stream<Path> paths = Files.walk(dir)) {
            for (Path p : (Iterable<Path>) paths.sorted(java.util.Comparator.reverseOrder())::iterator) {
                Files.deleteIfExists(p);
            }
        } catch (IOException e) {
            log.warn("chunk dir cleanup failed: {}", dir, e);
        }
    }

    /**
     * 文件名清洗：Unicode NFC 归一化（macOS/Windows 一致性）→ 只取文件名部分（防路径穿越）→ 剔除危险字符。
     * 顺序不可颠倒：NFD 的 combining char 在 Windows 上 Paths.get 会抛 InvalidPathException，先归一化再解析。
     */
    private static String sanitizeFilename(String raw) {
        if (raw == null || raw.isBlank()) {
            return "unnamed";
        }
        String normalized = Normalizer.normalize(raw, Normalizer.Form.NFC);
        String name;
        try {
            name = Paths.get(normalized).getFileName().toString();
        } catch (java.nio.file.InvalidPathException e) {
            // 兜底：极端非法字符跳过取段，直接字符级清洗
            name = normalized;
        }
        name = name.replaceAll("[\\x00-\\x1f\\\\/:*?\"<>|]", "_");
        if (name.isBlank() || ".".equals(name) || "..".equals(name)) {
            return "unnamed";
        }
        return name.length() > 255 ? name.substring(name.length() - 255) : name;
    }

    /** 提取扩展名（白名单校验），无扩展名或非法返回空串。 */
    private static String extractExt(String filename) {
        int dot = filename.lastIndexOf('.');
        if (dot < 0 || dot == filename.length() - 1) {
            return "";
        }
        String ext = filename.substring(dot + 1).toLowerCase();
        if (!ext.matches("[a-z0-9]{1,10}")) {
            return "";
        }
        return "." + ext;
    }
}
