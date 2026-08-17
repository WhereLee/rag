package com.rag.gateway.config;

import com.rag.gateway.security.AuthRateLimiter;
import com.rag.gateway.security.InMemoryRateLimiter;
import com.rag.gateway.security.JwtUtil;
import com.rag.gateway.security.RateLimiter;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.reactive.function.client.WebClient;

@Configuration
public class GatewayConfig {

    @Bean
    public JwtUtil jwtUtil(@Value("${gateway.jwt.secret}") String secret,
                           @Value("${gateway.jwt.ttl-hours:8}") long ttlHours) {
        return new JwtUtil(secret, ttlHours * 3600_000);
    }

    @Bean
    public RateLimiter rateLimiter(@Value("${gateway.rate-limit.per-minute:20}") int perMinute) {
        return new InMemoryRateLimiter(perMinute);
    }

    @Bean
    public AuthRateLimiter authRateLimiter() {
        return new AuthRateLimiter();
    }

    @Bean
    public WebClient pythonWebClient(@Value("${gateway.python.base-url}") String baseUrl,
                                     @Value("${gateway.internal-api-key:}") String internalKey) {
        WebClient.Builder builder = WebClient.builder()
                .baseUrl(baseUrl)
                .codecs(c -> c.defaultCodecs().maxInMemorySize(8 * 1024 * 1024));
        // 安全加固：注入 X-Internal-Key，与 Python 服务 InternalAuthMiddleware 配合
        if (internalKey != null && !internalKey.isEmpty()) {
            builder.defaultHeader("X-Internal-Key", internalKey);
        }
        return builder.build();
    }
}
