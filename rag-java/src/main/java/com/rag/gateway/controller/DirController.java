package com.rag.gateway.controller;

import com.rag.gateway.service.AuditService;
import com.rag.gateway.service.DirService;
import com.rag.gateway.service.UserService;
import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

/**
 * 目录 API：建/列/重命名/删除。
 *
 * 隔离原则：user_id 一律从 JWT 会话解析；目录不属于当前用户一律 404。
 * 错误映射：同名目录 409、非空目录 409、不存在 404。
 */
@RestController
@RequestMapping("/api/dirs")
public class DirController {

    private static final Logger log = LoggerFactory.getLogger(DirController.class);

    private final DirService dirService;
    private final UserService userService;
    private final AuditService audit;

    public DirController(DirService dirService, UserService userService, AuditService audit) {
        this.dirService = dirService;
        this.userService = userService;
        this.audit = audit;
    }

    @PostMapping
    public ResponseEntity<?> create(@RequestBody Map<String, String> body, HttpServletRequest req) {
        String username = username(req);
        Long userId = userService.getUserIdByUsername(username);
        String name = body == null ? null : body.get("name");
        if (name == null || name.isBlank()) {
            return ResponseEntity.badRequest().body(Map.of("error", "目录名不能为空"));
        }
        long t0 = System.currentTimeMillis();
        try {
            Map<String, Object> dir = dirService.createDir(userId, name);
            audit.record(username, "dir.create", String.valueOf(dir.get("id")), 200,
                    (int) (System.currentTimeMillis() - t0));
            return ResponseEntity.ok(dir);
        } catch (IllegalArgumentException e) {
            if ("同名目录".equals(e.getMessage())) {
                return ResponseEntity.status(409).body(Map.of("error", e.getMessage()));
            }
            return ResponseEntity.badRequest().body(Map.of("error", e.getMessage()));
        }
    }

    @GetMapping
    public ResponseEntity<?> list(HttpServletRequest req) {
        Long userId = userService.getUserIdByUsername(username(req));
        return ResponseEntity.ok(Map.of("items", dirService.listDirs(userId)));
    }

    @PatchMapping("/{id}")
    public ResponseEntity<?> rename(@PathVariable Long id, @RequestBody Map<String, String> body,
                                    HttpServletRequest req) {
        String username = username(req);
        Long userId = userService.getUserIdByUsername(username);
        String name = body == null ? null : body.get("name");
        if (name == null || name.isBlank()) {
            return ResponseEntity.badRequest().body(Map.of("error", "目录名不能为空"));
        }
        long t0 = System.currentTimeMillis();
        try {
            dirService.renameDir(userId, id, name);
            audit.record(username, "dir.rename", String.valueOf(id), 200,
                    (int) (System.currentTimeMillis() - t0));
            return ResponseEntity.ok(Map.of("renamed", true));
        } catch (IllegalArgumentException e) {
            if ("目录不存在".equals(e.getMessage())) {
                return ResponseEntity.status(404).body(Map.of("error", "目录不存在"));
            }
            if ("同名目录".equals(e.getMessage())) {
                return ResponseEntity.status(409).body(Map.of("error", e.getMessage()));
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
            dirService.deleteDir(userId, id);
            audit.record(username, "dir.delete", String.valueOf(id), 200,
                    (int) (System.currentTimeMillis() - t0));
            return ResponseEntity.ok(Map.of("deleted", true));
        } catch (IllegalArgumentException e) {
            if ("目录不存在".equals(e.getMessage())) {
                return ResponseEntity.status(404).body(Map.of("error", "目录不存在"));
            }
            return ResponseEntity.status(409).body(Map.of("error", e.getMessage()));
        }
    }

    private static String username(HttpServletRequest req) {
        return (String) req.getAttribute("username");
    }
}
