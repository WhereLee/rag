package com.rag.gateway.security;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

/**
 * JwtUtil 纯 JUnit 单测：签发/校验/过期/篡改。
 */
class JwtUtilTest {

    private final JwtUtil jwt = new JwtUtil("unit-test-secret-0123456789abcdef", 3600_000);

    @Test
    void issueAndVerifyRoundTrip() {
        String token = jwt.issue("alice", "admin");
        assertNotNull(token);
        assertEquals(3, token.split("\\.").length);

        String payload = jwt.verify(token);
        assertNotNull(payload);
        assertEquals("alice", JwtUtil.extract(payload, "sub"));
        assertEquals("admin", JwtUtil.extract(payload, "role"));
    }

    @Test
    void verifyRejectsTamperedSignature() {
        String token = jwt.issue("alice", "user");
        String[] parts = token.split("\\.");
        // 篡改载荷（保留签名不变）
        String forgedPayload = java.util.Base64.getUrlEncoder().withoutPadding()
                .encodeToString("{\"sub\":\"mallory\",\"role\":\"admin\",\"exp\":9999999999999}"
                        .getBytes(java.nio.charset.StandardCharsets.UTF_8));
        String forged = parts[0] + "." + forgedPayload + "." + parts[2];
        assertNull(jwt.verify(forged));
    }

    @Test
    void verifyRejectsExpiredToken() {
        JwtUtil shortTtl = new JwtUtil("unit-test-secret-0123456789abcdef", -1000); // 已过期
        String token = shortTtl.issue("alice", "user");
        assertNull(shortTtl.verify(token));
    }

    @Test
    void verifyRejectsMalformedToken() {
        assertNull(jwt.verify("not-a-jwt"));
        assertNull(jwt.verify("a.b"));
        assertNull(jwt.verify(""));
    }
}
