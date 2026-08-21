package com.rag.gateway.controller;

import com.rag.gateway.security.UploadRateLimiter;
import com.rag.gateway.service.AuditService;
import com.rag.gateway.service.FileCleanupTask;
import com.rag.gateway.service.FileService;
import com.rag.gateway.service.UserService;
import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.io.AbstractResource;
import org.springframework.core.io.FileSystemResource;
import org.springframework.core.io.Resource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpRange;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;
import java.io.IOException;
import java.io.InputStream;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

/**
 * 用户文件 API：上传 / 列表 / 下载 / 删除。
 *
 * 隔离原则：
 * - user_id 一律从 JWT 会话解析（AuthFilter 注入 username），不接受前端传参
 * - 删除/下载先做归属校验，不属于当前用户一律 404（不泄露文件存在性）
 */
@RestController
@RequestMapping("/api/files")
public class FileController {

    private static final Logger log = LoggerFactory.getLogger(FileController.class);

    private final FileService fileService;
    private final UserService userService;
    private final AuditService audit;
    private final UploadRateLimiter uploadRateLimiter;
    private final FileCleanupTask fileCleanupTask;

    public FileController(FileService fileService, UserService userService, AuditService audit,
                          UploadRateLimiter uploadRateLimiter, FileCleanupTask fileCleanupTask) {
        this.fileService = fileService;
        this.userService = userService;
        this.audit = audit;
        this.uploadRateLimiter = uploadRateLimiter;
        this.fileCleanupTask = fileCleanupTask;
    }

    @PostMapping("/upload")
    public ResponseEntity<?> upload(@RequestParam("file") MultipartFile file,
                                    @RequestParam(name = "dir_id", required = false) Long dirId,
                                    HttpServletRequest req) {
        String username = username(req);
        Long userId = userService.getUserIdByUsername(username);
        // 上传限流（按用户）：防止单用户疯狂上传撑爆磁盘
        if (!uploadRateLimiter.allow(userId)) {
            return ResponseEntity.status(429).body(Map.of("error", "上传过于频繁，请稍后再试"));
        }
        long t0 = System.currentTimeMillis();
        try {
            Map<String, Object> saved = fileService.saveFile(userId, file, dirId);
            audit.record(username, "file.upload", String.valueOf(saved.get("filename")), 200,
                    (int) (System.currentTimeMillis() - t0));
            return ResponseEntity.ok(saved);
        } catch (IllegalArgumentException e) {
            if ("目录不存在".equals(e.getMessage())) {
                return ResponseEntity.status(404).body(Map.of("error", "目录不存在"));
            }
            return ResponseEntity.badRequest().body(Map.of("error", e.getMessage()));
        } catch (Exception e) {
            audit.record(username, "file.upload", file.getOriginalFilename(), 500,
                    (int) (System.currentTimeMillis() - t0));
            return ResponseEntity.status(500).body(Map.of("error", "上传失败，请稍后重试"));
        }
    }

    @GetMapping
    public ResponseEntity<?> list(@RequestParam(defaultValue = "1") int page,
                                  @RequestParam(defaultValue = "20") int pageSize,
                                  @RequestParam(name = "dir_id", required = false) Long dirId,
                                  HttpServletRequest req) {
        if (page < 1) {
            return ResponseEntity.badRequest().body(Map.of("error", "page 必须 >= 1"));
        }
        if (pageSize < 1 || pageSize > 100) {
            return ResponseEntity.badRequest().body(Map.of("error", "pageSize 需在 1-100 之间"));
        }
        Long userId = userService.getUserIdByUsername(username(req));
        try {
            return ResponseEntity.ok(fileService.listFiles(userId, page, pageSize, dirId));
        } catch (IllegalArgumentException e) {
            return ResponseEntity.status(404).body(Map.of("error", "目录不存在"));
        }
    }

    /** 移动文件到目录：body {dir_id}，dir_id 为 null 移回根目录。 */
    @PatchMapping("/{id}/move")
    public ResponseEntity<?> move(@PathVariable Long id, @RequestBody(required = false) Map<String, Object> body,
                                  HttpServletRequest req) {
        String username = username(req);
        Long userId = userService.getUserIdByUsername(username);
        Long dirId = null;
        if (body != null && body.get("dir_id") != null) {
            try {
                dirId = ((Number) body.get("dir_id")).longValue();
            } catch (Exception e) {
                return ResponseEntity.badRequest().body(Map.of("error", "dir_id 无效"));
            }
        }
        long t0 = System.currentTimeMillis();
        try {
            fileService.moveFile(userId, id, dirId);
            audit.record(username, "file.move", String.valueOf(id), 200,
                    (int) (System.currentTimeMillis() - t0));
            return ResponseEntity.ok(Map.of("moved", true));
        } catch (IllegalArgumentException e) {
            if ("文件不存在".equals(e.getMessage())) {
                return ResponseEntity.status(404).body(Map.of("error", "文件不存在"));
            }
            return ResponseEntity.status(404).body(Map.of("error", e.getMessage()));
        }
    }

    /** 重命名：参数前置校验（400），归属失败 404，同名冲突 400。 */
    @PutMapping("/{id}/rename")
    public ResponseEntity<?> rename(@PathVariable Long id, @RequestBody Map<String, String> body,
                                    HttpServletRequest req) {
        String username = username(req);
        Long userId = userService.getUserIdByUsername(username);
        String newName = body == null ? null : body.get("filename");
        if (newName == null || newName.isBlank()) {
            return ResponseEntity.badRequest().body(Map.of("error", "文件名不能为空"));
        }
        if (newName.length() > 255) {
            return ResponseEntity.badRequest().body(Map.of("error", "文件名过长（上限 255 字符）"));
        }
        long t0 = System.currentTimeMillis();
        try {
            fileService.renameFile(userId, id, newName);
            audit.record(username, "file.rename", String.valueOf(id), 200,
                    (int) (System.currentTimeMillis() - t0));
            return ResponseEntity.ok(Map.of("renamed", true));
        } catch (IllegalArgumentException e) {
            if ("文件不存在".equals(e.getMessage())) {
                return ResponseEntity.status(404).body(Map.of("error", "文件不存在"));
            }
            return ResponseEntity.badRequest().body(Map.of("error", e.getMessage()));
        }
    }

    /** 在线预览：解析产物结构化文本（归属校验；未解析 409，产物缺失 404）。 */
    @GetMapping("/{id}/preview")
    public ResponseEntity<?> preview(@PathVariable Long id, HttpServletRequest req) {
        String username = username(req);
        Long userId = userService.getUserIdByUsername(username);
        try {
            Map<String, Object> result = fileService.preview(userId, id);
            if (!Boolean.TRUE.equals(result.get("previewable"))) {
                String reason = String.valueOf(result.get("reason"));
                if ("尚未解析完成".equals(reason)) {
                    return ResponseEntity.status(409).body(Map.of("error", reason));
                }
                return ResponseEntity.status(404).body(Map.of("error", reason));
            }
            return ResponseEntity.ok(result);
        } catch (IllegalArgumentException e) {
            return ResponseEntity.status(404).body(Map.of("error", e.getMessage()));
        }
    }

    /** 手动重新解析：失败/部分失败的文件置回待解析队列（手动兑底层）。 */
    @PostMapping("/{id}/reparse")
    public ResponseEntity<?> reparse(@PathVariable Long id, HttpServletRequest req) {
        String username = username(req);
        Long userId = userService.getUserIdByUsername(username);
        long t0 = System.currentTimeMillis();
        try {
            fileService.reparse(userId, id);
            audit.record(username, "file.reparse", String.valueOf(id), 200,
                    (int) (System.currentTimeMillis() - t0));
            return ResponseEntity.ok(Map.of("reparsed", true));
        } catch (IllegalArgumentException e) {
            if ("文件不存在".equals(e.getMessage())) {
                return ResponseEntity.status(404).body(Map.of("error", "文件不存在"));
            }
            return ResponseEntity.badRequest().body(Map.of("error", e.getMessage()));
        }
    }

    /**
     * 分片上传初始化：先查秒传（命中直接完成，零字节传输），否则创建会话返回 upload_id。
     * body: {hash, size, filename, chunk_count, chunk_size}
     */
    @PostMapping("/upload/init")
    public ResponseEntity<?> uploadInit(@RequestBody Map<String, Object> body, HttpServletRequest req) {
        String username = username(req);
        Long userId = userService.getUserIdByUsername(username);
        String hash = body == null ? null : (String) body.get("hash");
        Object sizeObj = body == null ? null : body.get("size");
        String filename = body == null ? null : (String) body.get("filename");
        Object ccObj = body == null ? null : body.get("chunk_count");
        Object csObj = body == null ? null : body.get("chunk_size");
        Long dirId = null;
        if (body != null && body.get("dir_id") != null) {
            try {
                dirId = ((Number) body.get("dir_id")).longValue();
            } catch (Exception e) {
                return ResponseEntity.badRequest().body(Map.of("error", "dir_id 无效"));
            }
        }
        if (hash == null || !hash.matches("[0-9a-f]{64}")) {
            return ResponseEntity.badRequest().body(Map.of("error", "hash 格式无效"));
        }
        if (filename == null || filename.isBlank()) {
            return ResponseEntity.badRequest().body(Map.of("error", "文件名不能为空"));
        }
        long size;
        int chunkCount;
        long chunkSize;
        try {
            size = ((Number) sizeObj).longValue();
            chunkCount = ((Number) ccObj).intValue();
            chunkSize = ((Number) csObj).longValue();
        } catch (Exception e) {
            return ResponseEntity.badRequest().body(Map.of("error", "参数无效"));
        }
        if (size <= 0 || size > 1024L * 1024 * 1024) {
            return ResponseEntity.badRequest().body(Map.of("error", "文件大小无效（上限 1GB）"));
        }
        if (chunkCount < 1 || chunkCount > 10000 || chunkSize < 1 || chunkSize > 50L * 1024 * 1024) {
            return ResponseEntity.badRequest().body(Map.of("error", "分片参数无效"));
        }
        if (!uploadRateLimiter.allow(userId)) {
            return ResponseEntity.status(429).body(Map.of("error", "上传过于频繁，请稍后再试"));
        }
        Map<String, Object> saved = fileService.instantUpload(userId, hash, size, filename, dirId);
        if (saved != null) {
            Map<String, Object> resp = new java.util.HashMap<>(saved);
            resp.put("hit", true);
            return ResponseEntity.ok(resp);
        }
        try {
            long uploadId = fileService.createUploadSession(userId, hash, size, filename, chunkCount, chunkSize, dirId);
            return ResponseEntity.ok(Map.of("hit", false, "upload_id", uploadId));
        } catch (IllegalArgumentException e) {
            if ("目录不存在".equals(e.getMessage())) {
                return ResponseEntity.status(404).body(Map.of("error", "目录不存在"));
            }
            // 会话数超限 / 磁盘空间不足等业务拒绝 → 400（与整传/complete 的错误语义一致）
            return ResponseEntity.badRequest().body(Map.of("error", e.getMessage()));
        }
    }

    /** 分片上传：落盘一片。同片重传（断点续传重试）覆盖。 */
    @PostMapping("/upload/{uploadId}/chunk")
    public ResponseEntity<?> uploadChunk(@PathVariable Long uploadId,
                                         @RequestParam("index") int index,
                                         @RequestParam("file") MultipartFile file,
                                         HttpServletRequest req) {
        Long userId = userService.getUserIdByUsername(username(req));
        try {
            fileService.saveChunk(userId, uploadId, index, file);
            return ResponseEntity.ok(Map.of("accepted", true, "index", index));
        } catch (IllegalArgumentException e) {
            if ("上传任务不存在".equals(e.getMessage())) {
                return ResponseEntity.status(404).body(Map.of("error", "上传任务不存在"));
            }
            return ResponseEntity.badRequest().body(Map.of("error", e.getMessage()));
        } catch (Exception e) {
            log.error("chunk save failed: user={} session={} index={}", userId, uploadId, index, e);
            return ResponseEntity.status(500).body(Map.of("error", "分片保存失败"));
        }
    }

    /** 分片状态（断点续传）：已传分片列表。 */
    @GetMapping("/upload/{uploadId}/status")
    public ResponseEntity<?> uploadStatus(@PathVariable Long uploadId, HttpServletRequest req) {
        Long userId = userService.getUserIdByUsername(username(req));
        try {
            return ResponseEntity.ok(fileService.sessionStatus(userId, uploadId));
        } catch (IllegalArgumentException e) {
            return ResponseEntity.status(404).body(Map.of("error", "上传任务不存在"));
        }
    }

    /** 分片合并 + 校验 + 入库（整体 hash 复验/魔数/配额）。 */
    @PostMapping("/upload/{uploadId}/complete")
    public ResponseEntity<?> uploadComplete(@PathVariable Long uploadId, HttpServletRequest req) {
        String username = username(req);
        Long userId = userService.getUserIdByUsername(username);
        long t0 = System.currentTimeMillis();
        try {
            Map<String, Object> saved = fileService.completeUpload(userId, uploadId);
            audit.record(username, "file.chunked-upload", String.valueOf(saved.get("filename")), 200,
                    (int) (System.currentTimeMillis() - t0));
            return ResponseEntity.ok(saved);
        } catch (IllegalArgumentException e) {
            if ("上传任务不存在".equals(e.getMessage())) {
                return ResponseEntity.status(404).body(Map.of("error", "上传任务不存在"));
            }
            return ResponseEntity.badRequest().body(Map.of("error", e.getMessage()));
        } catch (Exception e) {
            log.error("upload complete failed: user={} session={}", userId, uploadId, e);
            return ResponseEntity.status(500).body(Map.of("error", "上传合并失败"));
        }
    }

    /**
     * 下载（支持 Range）。返回类型必须写死 Resource：若用 ResponseEntity<?>，
     * 泛型擦除为 Object 会导致 ResourceRegionHttpMessageConverter 类型检查失败、
     * 退化为 Jackson 序列化（500）；且该 converter 在 Spring 6.2 下往已冻结的
     * 响应头再写 Content-Range 会抛 UnsupportedOperationException。
     * 因此这里自算区间、用切片资源输出，头全部在 Controller 层设置。
     */
    @GetMapping("/{id}/download")
    public ResponseEntity<Resource> download(@PathVariable Long id, HttpServletRequest req) {
        Long userId = userService.getUserIdByUsername(username(req));
        Map<String, Object> f = fileService.getOwnedFile(userId, id);
        if (f == null) {
            return ResponseEntity.status(404).build();
        }
        String filename = (String) f.get("filename");
        // RFC 5987：中文文件名用 filename*=UTF-8'' 编码，避免乱码
        String encoded = URLEncoder.encode(filename, StandardCharsets.UTF_8).replace("+", "%20");
        Path path = (Path) f.get("path");
        if (!Files.exists(path)) {
            return ResponseEntity.status(404).build();
        }
        long fileSize = ((Number) f.get("file_size")).longValue();
        String range = req.getHeader("Range");
        if (range != null && range.startsWith("bytes=")) {
            try {
                List<HttpRange> ranges = HttpRange.parseRanges(range);
                if (ranges.isEmpty()) {
                    return ResponseEntity.status(416).build();
                }
                // 多 range 请求取第一个（单 range 是主流用法；多 range 可后续扩展 multipart/byteranges）
                long fileLength = Files.size(path);
                long start = ranges.get(0).getRangeStart(fileLength);
                long end = ranges.get(0).getRangeEnd(fileLength);
                if (start >= fileLength || start > end) {
                    return ResponseEntity.status(416).build();
                }
                long count = end - start + 1;
                return ResponseEntity.status(HttpStatus.PARTIAL_CONTENT)
                        .header(HttpHeaders.CONTENT_RANGE, "bytes " + start + "-" + end + "/" + fileSize)
                        .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename*=UTF-8''" + encoded)
                        .contentType(MediaType.APPLICATION_OCTET_STREAM)
                        .contentLength(count)
                        .body(new FileSliceResource(path, start, count));
            } catch (IllegalArgumentException | IOException e) {
                return ResponseEntity.status(416).build();
            }
        }
        return ResponseEntity.ok()
                .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename*=UTF-8''" + encoded)
                .contentType(MediaType.APPLICATION_OCTET_STREAM)
                .contentLength(fileSize)
                .body(new FileSystemResource(path));
    }

    /** 文件局部切片资源：只暴露 [start, start+count) 字节（Range 下载用）。 */
    private static final class FileSliceResource extends AbstractResource {
        private final Path path;
        private final long start;
        private final long count;

        FileSliceResource(Path path, long start, long count) {
            this.path = path;
            this.start = start;
            this.count = count;
        }

        @Override
        public String getDescription() {
            return "file slice " + path + " [" + start + ", " + (start + count) + ")";
        }

        @Override
        public InputStream getInputStream() throws IOException {
            InputStream in = Files.newInputStream(path);
            in.skipNBytes(start);
            return new BoundedSliceInputStream(in, count);
        }

        @Override
        public long contentLength() {
            return count;
        }
    }

    /** 只读 count 字节的输入流包装（读完即 EOF，防 Range 越界读到文件尾）。 */
    private static final class BoundedSliceInputStream extends InputStream {
        private final InputStream delegate;
        private long remaining;

        BoundedSliceInputStream(InputStream delegate, long count) {
            this.delegate = delegate;
            this.remaining = count;
        }

        @Override
        public int read() throws IOException {
            if (remaining <= 0) {
                return -1;
            }
            int b = delegate.read();
            if (b >= 0) {
                remaining--;
            }
            return b;
        }

        @Override
        public int read(byte[] b, int off, int len) throws IOException {
            if (remaining <= 0) {
                return -1;
            }
            int toRead = (int) Math.min(len, remaining);
            int n = delegate.read(b, off, toRead);
            if (n > 0) {
                remaining -= n;
            }
            return n;
        }

        @Override
        public void close() throws IOException {
            delegate.close();
        }
    }

    /**
     * 秒传检查：先查内容 hash，命中则直接建引用（不传字节）；未命中返回 {hit:false} 走正常上传。
     * body: {hash, size, filename}
     */
    @PostMapping("/check-hash")
    public ResponseEntity<?> checkHash(@RequestBody Map<String, Object> body, HttpServletRequest req) {
        String username = username(req);
        Long userId = userService.getUserIdByUsername(username);
        String hash = body == null ? null : (String) body.get("hash");
        Object sizeObj = body == null ? null : body.get("size");
        String filename = body == null ? null : (String) body.get("filename");
        if (hash == null || !hash.matches("[0-9a-f]{64}")) {
            return ResponseEntity.badRequest().body(Map.of("error", "hash 格式无效"));
        }
        long size;
        try {
            size = ((Number) sizeObj).longValue();
        } catch (Exception e) {
            return ResponseEntity.badRequest().body(Map.of("error", "size 无效"));
        }
        if (filename == null || filename.isBlank()) {
            return ResponseEntity.badRequest().body(Map.of("error", "文件名不能为空"));
        }
        Map<String, Object> saved = fileService.instantUpload(userId, hash, size, filename, null);
        if (saved == null) {
            return ResponseEntity.ok(Map.of("hit", false));
        }
        audit.record(username, "file.instant-upload", String.valueOf(saved.get("filename")), 200, 0);
        Map<String, Object> resp = new java.util.HashMap<>(saved);
        resp.put("hit", true);
        return ResponseEntity.ok(resp);
    }

    /** 回收站列表（软删记录）。 */
    @GetMapping("/trash")
    public ResponseEntity<?> trash(HttpServletRequest req) {
        Long userId = userService.getUserIdByUsername(username(req));
        return ResponseEntity.ok(fileService.listTrash(userId));
    }

    /** 恢复回收站文件：blob 已过期清理 → 400。 */
    @PostMapping("/{id}/restore")
    public ResponseEntity<?> restore(@PathVariable Long id, HttpServletRequest req) {
        String username = username(req);
        Long userId = userService.getUserIdByUsername(username);
        long t0 = System.currentTimeMillis();
        try {
            fileService.restoreFile(userId, id);
            audit.record(username, "file.restore", String.valueOf(id), 200,
                    (int) (System.currentTimeMillis() - t0));
            return ResponseEntity.ok(Map.of("restored", true));
        } catch (IllegalArgumentException e) {
            if ("文件不存在".equals(e.getMessage())) {
                return ResponseEntity.status(404).body(Map.of("error", "文件不存在"));
            }
            return ResponseEntity.badRequest().body(Map.of("error", e.getMessage()));
        }
    }

    @DeleteMapping("/{id}")
    public ResponseEntity<?> delete(@PathVariable Long id, HttpServletRequest req) {
        String username = username(req);
        Long userId = userService.getUserIdByUsername(username);
        long t0 = System.currentTimeMillis();
        try {
            boolean removed = fileService.deleteFile(userId, id);
            audit.record(username, "file.delete", String.valueOf(id), 200,
                    (int) (System.currentTimeMillis() - t0));
            if (removed) {
                return ResponseEntity.ok(Map.of("deleted", true));
            }
            return ResponseEntity.ok(Map.of("deleted", true,
                    "warning", "物理文件删除失败，系统将自动清理"));
        } catch (IllegalArgumentException e) {
            return ResponseEntity.status(404).body(Map.of("error", "文件不存在"));
        } catch (Exception e) {
            return ResponseEntity.status(500).body(Map.of("error", "删除失败，请稍后重试"));
        }
    }

    /**
     * 手动触发孤儿清理（单机运维入口，登录即可；部署多实例/生产环境时建议收敛为 admin 角色）。
     * 幂等操作：清理超时 tmp 与无 DB 记录的文件。
     */
    @PostMapping("/cleanup")
    public ResponseEntity<?> cleanup(HttpServletRequest req) {
        userService.getUserIdByUsername(username(req));
        return ResponseEntity.ok(fileCleanupTask.sweep());
    }

    private String username(HttpServletRequest req) {
        return (String) req.getAttribute("username");
    }
}
