package com.rag.gateway;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableAsync;

/**
 * 智能文档问答系统 Java 薄网关。
 *
 * 职责（架构文档"方向一"定位）：鉴权 / 限流 / 审计 / SSE 转发 / 管理 API 代理。
 * AI 核心能力在 Python 服务，本层不重复实现。
 */
@SpringBootApplication
@EnableAsync   // 审计日志异步写入
public class RagGatewayApplication {
    public static void main(String[] args) {
        SpringApplication.run(RagGatewayApplication.class, args);
    }
}
