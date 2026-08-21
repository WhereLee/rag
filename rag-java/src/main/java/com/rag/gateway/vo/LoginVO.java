package com.rag.gateway.vo;

/** 登录成功响应。 */
public record LoginVO(String token, Long userId, String username, String role) {
}
