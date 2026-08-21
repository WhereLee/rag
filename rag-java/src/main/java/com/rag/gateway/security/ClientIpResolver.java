package com.rag.gateway.security;

import jakarta.servlet.http.HttpServletRequest;

import java.net.InetAddress;
import java.net.UnknownHostException;
import java.util.ArrayList;
import java.util.List;

/**
 * 客户端真实 IP 解析器：只在直连 IP 属于可信反向代理时才信任 X-Forwarded-For。
 *
 * 安全背景（坑位 #1）：X-Forwarded-For 完全由客户端可控，直连场景直接信任该头，
 * 攻击者每次请求换一个伪造 IP 即可绕过"每 IP 每分钟 N 次"的注册/登录限流。
 * 本解析器默认（trusted-proxies 为空）完全忽略 XFF、仅用直连 IP（fail-safe）；
 * 配置可信代理后按标准代理链语义解析：XFF 形如 "client, proxy1, proxy2"，
 * 从右往左跳过可信代理后第一个 IP 即最接近客户端的真实 IP——
 * 即使客户端预置伪造 IP 链（fake1, fake2），nginx 追加真实 IP 后为
 * "fake1, fake2, real_client"，取最右非代理 IP 仍得到真实客户端。
 */
public class ClientIpResolver {

    private final List<Cidr> trustedProxies;

    public ClientIpResolver(List<String> trustedProxies) {
        this.trustedProxies = new ArrayList<>();
        if (trustedProxies != null) {
            for (String spec : trustedProxies) {
                if (spec != null && !spec.isBlank()) {
                    this.trustedProxies.add(Cidr.parse(spec.trim()));
                }
            }
        }
    }

    public String resolve(HttpServletRequest req) {
        String remote = req.getRemoteAddr();
        // 直连 IP 不是可信代理 → 忽略 XFF（客户端可伪造），直接用直连 IP
        if (!isTrustedProxy(remote)) {
            return remote;
        }
        String xff = req.getHeader("X-Forwarded-For");
        if (xff != null && !xff.isEmpty()) {
            String[] chain = xff.split(",");
            for (int i = chain.length - 1; i >= 0; i--) {
                String ip = chain[i].trim();
                if (!ip.isEmpty() && !isTrustedProxy(ip)) {
                    return ip;
                }
            }
        }
        return remote;
    }

    private boolean isTrustedProxy(String ip) {
        for (Cidr c : trustedProxies) {
            if (c.matches(ip)) {
                return true;
            }
        }
        return false;
    }

    /** 轻量 IP/CIDR 匹配：支持 "1.2.3.4"、"10.0.0.0/8"、"::1"、"2001:db8::/32"。 */
    static final class Cidr {
        private final byte[] network;
        private final int prefix;

        static Cidr parse(String spec) {
            String addr = spec;
            int prefix = -1;
            int slash = spec.indexOf('/');
            if (slash >= 0) {
                addr = spec.substring(0, slash);
                prefix = Integer.parseInt(spec.substring(slash + 1));
            }
            try {
                byte[] raw = normalize(InetAddress.getByName(addr).getAddress());
                if (prefix < 0) {
                    prefix = raw.length == 4 ? 32 : 128;
                }
                if (prefix < 0 || prefix > raw.length * 8) {
                    throw new IllegalArgumentException("非法前缀长度: " + spec);
                }
                return new Cidr(raw, prefix);
            } catch (UnknownHostException e) {
                throw new IllegalArgumentException("非法 IP/CIDR: " + spec, e);
            }
        }

        private Cidr(byte[] network, int prefix) {
            this.network = network;
            this.prefix = prefix;
        }

        boolean matches(String ip) {
            try {
                byte[] raw = normalize(InetAddress.getByName(ip).getAddress());
                if (raw.length != network.length) {
                    return false; // IPv4 与 IPv6 不互通
                }
                int fullBytes = prefix / 8;
                int remBits = prefix % 8;
                for (int i = 0; i < fullBytes; i++) {
                    if (raw[i] != network[i]) {
                        return false;
                    }
                }
                if (remBits > 0) {
                    int mask = 0xFF << (8 - remBits);
                    if ((raw[fullBytes] & mask) != (network[fullBytes] & mask)) {
                        return false;
                    }
                }
                return true;
            } catch (UnknownHostException e) {
                return false;
            }
        }

        /** IPv4-mapped IPv6（::ffff:a.b.c.d）归一化为 IPv4 字节，与 IPv4 列表可匹配。 */
        private static byte[] normalize(byte[] raw) {
            if (raw.length == 16) {
                boolean mapped = true;
                for (int i = 0; i < 10; i++) {
                    if (raw[i] != 0) {
                        mapped = false;
                        break;
                    }
                }
                if (mapped && raw[10] == (byte) 0xFF && raw[11] == (byte) 0xFF) {
                    byte[] v4 = new byte[4];
                    System.arraycopy(raw, 12, v4, 0, 4);
                    return v4;
                }
            }
            return raw;
        }
    }
}
