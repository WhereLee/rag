package com.rag.gateway.security;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.util.Base64;

/**
 * 手写 HS256 JWT（不引入第三方库，算法透明可追问）。
 * 载荷：sub（用户名）、role、exp。
 */
public final class JwtUtil {

    private static final String ALG = "HmacSHA256";
    private final byte[] secret;
    private final long ttlMillis;

    public JwtUtil(String secret, long ttlMillis) {
        this.secret = secret.getBytes(StandardCharsets.UTF_8);
        this.ttlMillis = ttlMillis;
    }

    public String issue(String username, String role) {
        String header = b64("{\"alg\":\"HS256\",\"typ\":\"JWT\"}");
        long exp = System.currentTimeMillis() + ttlMillis;
        String payloadJson = "{\"sub\":\"" + username + "\",\"role\":\"" + role + "\",\"exp\":" + exp + "}";
        String payload = b64(payloadJson);
        String sig = sign(header + "." + payload);
        return header + "." + payload + "." + sig;
    }

    /** 校验并返回载荷 JSON；失败返回 null。 */
    public String verify(String token) {
        try {
            String[] parts = token.split("\\.");
            if (parts.length != 3) return null;
            String expectSig = sign(parts[0] + "." + parts[1]);
            if (!constantTimeEquals(expectSig, parts[2])) return null;
            String payload = new String(Base64.getUrlDecoder().decode(parts[1]), StandardCharsets.UTF_8);
            long exp = extractLong(payload, "exp");
            if (exp < System.currentTimeMillis()) return null;
            return payload;
        } catch (Exception e) {
            return null;
        }
    }

    public static String extract(String payloadJson, String key) {
        String needle = "\"" + key + "\":\"";
        int i = payloadJson.indexOf(needle);
        if (i < 0) return null;
        int start = i + needle.length();
        int end = payloadJson.indexOf("\"", start);
        return payloadJson.substring(start, end);
    }

    private static long extractLong(String payloadJson, String key) {
        String needle = "\"" + key + "\":";
        int i = payloadJson.indexOf(needle);
        int start = i + needle.length();
        int end = start;
        while (end < payloadJson.length() && Character.isDigit(payloadJson.charAt(end))) end++;
        return Long.parseLong(payloadJson.substring(start, end));
    }

    private String sign(String data) {
        try {
            Mac mac = Mac.getInstance(ALG);
            mac.init(new SecretKeySpec(secret, ALG));
            return Base64.getUrlEncoder().withoutPadding()
                    .encodeToString(mac.doFinal(data.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception e) {
            throw new IllegalStateException("HMAC 签名失败", e);
        }
    }

    private static String b64(String s) {
        return Base64.getUrlEncoder().withoutPadding()
                .encodeToString(s.getBytes(StandardCharsets.UTF_8));
    }

    private static boolean constantTimeEquals(String a, String b) {
        if (a.length() != b.length()) return false;
        int r = 0;
        for (int i = 0; i < a.length(); i++) r |= a.charAt(i) ^ b.charAt(i);
        return r == 0;
    }
}
