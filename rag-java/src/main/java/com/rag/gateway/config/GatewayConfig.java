package com.rag.gateway.config;

import com.rag.gateway.security.AuthRateLimiter;
import com.rag.gateway.security.ClientIpResolver;
import com.rag.gateway.security.InMemoryRateLimiter;
import com.rag.gateway.security.JwtUtil;
import com.rag.gateway.security.RateLimiter;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.reactive.function.client.WebClient;

import java.util.ArrayList;
import java.util.List;

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
    public ClientIpResolver clientIpResolver(@Value("${gateway.security.trusted-proxies:}") String trustedProxies) {
        List<String> list = new ArrayList<>();
        if (trustedProxies != null && !trustedProxies.isBlank()) {
            for (String s : trustedProxies.split(",")) {
                if (!s.isBlank()) {
                    list.add(s.trim());
                }
            }
        }
        return new ClientIpResolver(list);
    }

    @Bean
    public AuthRateLimiter authRateLimiter(ClientIpResolver clientIpResolver) {
        return new AuthRateLimiter(clientIpResolver);
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
            // 网关签名（X-Gateway-Sign）：对每个转发请求的 X-User-Id 做 HMAC-SHA256。
            // Python 侧校验签名后才信任 X-User-Id，堵死"直连 8090 伪造用户身份"的信任边界。
            final String key = internalKey;
            builder.filter((request, next) -> {
                String uid = request.headers().getFirst("X-User-Id");
                if (uid != null && !uid.isEmpty()) {
                    return next.exchange(org.springframework.web.reactive.function.client.ClientRequest.from(request)
                            .header("X-Gateway-Sign", gatewaySign(key, uid))
                            .build());
                }
                return next.exchange(request);
            });
        }
        return builder.build();
    }

    /** HMAC-SHA256 hex 签名（与 Python 侧 hashlib.sha256 hexdigest 一致）。 */
    private static String gatewaySign(String key, String data) {
        try {
            javax.crypto.Mac mac = javax.crypto.Mac.getInstance("HmacSHA256");
            mac.init(new javax.crypto.spec.SecretKeySpec(
                    key.getBytes(java.nio.charset.StandardCharsets.UTF_8), "HmacSHA256"));
            return java.util.HexFormat.of().formatHex(
                    mac.doFinal(data.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        } catch (Exception e) {
            throw new IllegalStateException("gateway sign failed", e);
        }
    }
}
