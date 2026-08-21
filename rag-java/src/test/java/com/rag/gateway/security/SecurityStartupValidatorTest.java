package com.rag.gateway.security;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

/**
 * SecurityStartupValidator 纯 JUnit 单测：缺失配置 fail-fast 判定。
 * 校验逻辑抽在静态纯函数 findMissingConfigs 上，无需 Spring 上下文。
 */
class SecurityStartupValidatorTest {

    @Test
    void allConfiguredPasses() {
        assertNull(SecurityStartupValidator.findMissingConfigs(
                "secret", "password", "internal-key"));
    }

    @Test
    void missingJwtSecretReported() {
        String missing = SecurityStartupValidator.findMissingConfigs("", "password", "internal-key");
        assertNotNull(missing);
        assertTrue(missing.contains("GATEWAY_JWT_SECRET"));
        assertFalse(missing.contains("SPRING_DATASOURCE_PASSWORD"));
    }

    @Test
    void missingDbPasswordReported() {
        String missing = SecurityStartupValidator.findMissingConfigs("secret", null, "internal-key");
        assertNotNull(missing);
        assertTrue(missing.contains("SPRING_DATASOURCE_PASSWORD"));
    }

    @Test
    void missingInternalKeyReported() {
        String missing = SecurityStartupValidator.findMissingConfigs("secret", "password", "  ");
        assertNotNull(missing);
        assertTrue(missing.contains("GATEWAY_INTERNAL_API_KEY"));
    }

    @Test
    void allMissingReportedTogether() {
        String missing = SecurityStartupValidator.findMissingConfigs(null, null, null);
        assertNotNull(missing);
        assertTrue(missing.contains("GATEWAY_JWT_SECRET"));
        assertTrue(missing.contains("SPRING_DATASOURCE_PASSWORD"));
        assertTrue(missing.contains("GATEWAY_INTERNAL_API_KEY"));
    }
}
