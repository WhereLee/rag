package com.rag.gateway.dto;

import jakarta.validation.constraints.NotBlank;

/**
 * 登录请求。
 * 密码只校验非空、不校验格式：历史账号的密码可能早于当前策略创建，
 * 登录必须兼容；格式策略只在注册/改密时强制。
 * 业务失败统一返回"用户名或密码错误"，不区分账号不存在/密码错误（防账号枚举）。
 */
public record LoginRequest(
        @NotBlank(message = "请输入用户名") String username,
        @NotBlank(message = "请输入密码") String password) {
}
