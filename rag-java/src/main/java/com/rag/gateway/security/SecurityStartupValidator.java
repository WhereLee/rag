package com.rag.gateway.security;

import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 启动安全校验：检测敏感配置是否使用默认值。
 *
 * 生产部署时如果 JWT Secret / 数据库密码 / Internal API Key 未设置环境变量，
 * 将使用硬编码默认值，存在安全风险。此处启动时打印 WARNING 提醒。
 *
 * 如果环境变量 SPRING_PROFILES_ACTIVE=prod，检测到默认值时直接拒绝启动。
 */
@Component
public class SecurityStartupValidator {

    private static final Logger log = LoggerFactory.getLogger(SecurityStartupValidator.class);

    private static final String DEFAULT_JWT_SECRET = "rag-gateway-local-dev-secret-change-me";
    private static final String DEFAULT_DB_PASSWORD = "root";
    private static final String DEFAULT_INTERNAL_KEY = "rag-internal-dev-key-2026";

    @Value("${gateway.jwt.secret}")
    private String jwtSecret;

    @Value("${spring.datasource.password}")
    private String dbPassword;

    @Value("${gateway.internal-api-key:}")
    private String internalApiKey;

    @Value("${spring.profiles.active:}")
    private String activeProfile;

    @PostConstruct
    public void validate() {
        boolean isProd = "prod".equalsIgnoreCase(activeProfile);
        StringBuilder warnings = new StringBuilder();

        if (DEFAULT_JWT_SECRET.equals(jwtSecret)) {
            warnings.append("\n  [!] GATEWAY_JWT_SECRET 未设置，使用默认值（可被伪造 JWT）");
        }
        if (DEFAULT_DB_PASSWORD.equals(dbPassword)) {
            warnings.append("\n  [!] SPRING_DATASOURCE_PASSWORD 未设置，使用默认密码 'root'");
        }
        if (DEFAULT_INTERNAL_KEY.equals(internalApiKey)) {
            warnings.append("\n  [!] GATEWAY_INTERNAL_API_KEY 未设置，使用默认值");
        }

        if (warnings.length() > 0) {
            String msg = "\n========== 安全配置警告 ==========" + warnings
                    + "\n  生产部署前请设置对应环境变量！"
                    + "\n==================================";

            if (isProd) {
                log.error(msg);
                throw new IllegalStateException(
                        "生产环境检测到不安全的默认配置，请设置环境变量后重新启动");
            } else {
                log.warn(msg);
            }
        } else {
            log.info("安全配置检查通过：所有敏感配置已使用环境变量");
        }
    }
}
