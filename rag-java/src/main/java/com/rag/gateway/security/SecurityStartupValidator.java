package com.rag.gateway.security;

import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 启动安全校验：敏感配置（JWT Secret / 数据库密码 / Internal API Key）缺失时 fail-fast。
 *
 * 这三个配置在 application.yml 中已无默认值（${VAR:} 为空），若未通过环境变量注入，
 * Spring 注入空字符串；此处检测到空值直接拒绝启动，避免部署时静默使用可猜测的默认值。
 */
@Component
public class SecurityStartupValidator {

    private static final Logger log = LoggerFactory.getLogger(SecurityStartupValidator.class);

    @Value("${gateway.jwt.secret}")
    private String jwtSecret;

    @Value("${spring.datasource.password}")
    private String dbPassword;

    @Value("${gateway.internal-api-key:}")
    private String internalApiKey;

    @PostConstruct
    public void validate() {
        String missing = findMissingConfigs(jwtSecret, dbPassword, internalApiKey);
        if (missing != null) {
            log.error("========== 安全配置缺失，拒绝启动 ==========\n  {}", missing);
            throw new IllegalStateException(
                    "安全配置缺失，请设置环境变量后重新启动:" + missing);
        }
        log.info("安全配置检查通过：所有敏感配置已使用环境变量");
    }

    /** 纯函数：返回缺失配置清单（" A B C" 形式），无缺失返回 null。抽出来便于单元测试。 */
    static String findMissingConfigs(String jwtSecret, String dbPassword, String internalApiKey) {
        StringBuilder sb = new StringBuilder();
        if (jwtSecret == null || jwtSecret.isBlank()) {
            sb.append(" GATEWAY_JWT_SECRET");
        }
        if (dbPassword == null || dbPassword.isBlank()) {
            sb.append(" SPRING_DATASOURCE_PASSWORD");
        }
        if (internalApiKey == null || internalApiKey.isBlank()) {
            sb.append(" GATEWAY_INTERNAL_API_KEY");
        }
        return sb.length() == 0 ? null : sb.toString();
    }
}
