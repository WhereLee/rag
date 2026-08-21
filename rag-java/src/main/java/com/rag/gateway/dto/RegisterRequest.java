package com.rag.gateway.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;

/**
 * 注册请求。
 * role 不允许客户端指定：管理员账号仅通过初始化脚本创建（见 init_db.sql）。
 * 密码策略：8-32 位字母、数字或常见符号（BCrypt 输入 ≤72 字节，ASCII 场景无截断风险）。
 */
public record RegisterRequest(
        @NotBlank(message = "用户名不能为空")
        @Pattern(regexp = "^[a-zA-Z0-9_]{2,32}$", message = "用户名仅允许字母、数字、下划线，2-32位")
        String username,

        @NotBlank(message = "密码不能为空")
        @Pattern(regexp = "^[A-Za-z0-9@#$%^&*._\\-]{8,32}$",
                 message = "密码需为8-32位字母、数字或常见符号（@#$%^&*._-）")
        String password) {
}
