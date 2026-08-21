package com.rag.gateway.security;

import jakarta.servlet.http.HttpServletRequest;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

/**
 * ClientIpResolver 单元测试：XFF 信任边界（坑位 #1 修复验证）。
 * 覆盖：无可信代理忽略 XFF / 可信代理标准解析 / CIDR 匹配 / 伪造代理链 / IPv6。
 */
class ClientIpResolverTest {

    private static HttpServletRequest req(String remoteAddr, String xff) {
        HttpServletRequest req = mock(HttpServletRequest.class);
        when(req.getRemoteAddr()).thenReturn(remoteAddr);
        when(req.getHeader("X-Forwarded-For")).thenReturn(xff);
        return req;
    }

    @Test
    void noTrustedProxyIgnoresForgedXff() {
        // 默认（无可信代理）：即使客户端伪造 XFF，也用直连 IP
        ClientIpResolver resolver = new ClientIpResolver(List.of());
        assertEquals("203.0.113.7", resolver.resolve(req("203.0.113.7", "1.2.3.4")));
        assertEquals("203.0.113.7", resolver.resolve(req("203.0.113.7", "1.2.3.4, 5.6.7.8")));
        assertEquals("203.0.113.7", resolver.resolve(req("203.0.113.7", null)));
    }

    @Test
    void trustedProxyParsesRightmostNonProxyIp() {
        // 可信代理（精确 IP）：取 XFF 从右往左第一个非可信代理 IP
        ClientIpResolver resolver = new ClientIpResolver(List.of("192.168.1.10"));
        assertEquals("198.51.100.3", resolver.resolve(req("192.168.1.10", "198.51.100.3")));
        // 客户端预置伪造链，nginx 追加真实 IP 到最右 → 取最右非代理 IP = 真实客户端（伪造 IP 在最左被忽略）
        assertEquals("198.51.100.3", resolver.resolve(req("192.168.1.10", "203.0.113.9, 198.51.100.3")));
        // 双代理链：proxy2(可信) 追加 proxy1(可信)，proxy1 追加 client → 从右往左跳过可信代理后取 client
        ClientIpResolver doubleProxy = new ClientIpResolver(List.of("192.168.1.0/24"));
        assertEquals("198.51.100.3", doubleProxy.resolve(
                req("192.168.1.10", "198.51.100.3, 192.168.1.11, 192.168.1.12")));
    }

    @Test
    void trustedProxyWithCidr() {
        // 可信代理支持 CIDR 网段
        ClientIpResolver resolver = new ClientIpResolver(List.of("10.0.0.0/8"));
        assertEquals("198.51.100.3", resolver.resolve(req("10.1.2.3", "198.51.100.3")));
        // 网段外直连不信任 XFF
        assertEquals("11.1.2.3", resolver.resolve(req("11.1.2.3", "198.51.100.3")));
    }

    @Test
    void allProxyIpsTrustedFallsBackToRemote() {
        // XFF 里全是可信代理（客户端伪装成代理）→ 回退直连 IP，防自报代理
        ClientIpResolver resolver = new ClientIpResolver(List.of("192.168.1.0/24"));
        assertEquals("192.168.1.10", resolver.resolve(req("192.168.1.10", "192.168.1.20, 192.168.1.30")));
    }

    @Test
    void ipv6LoopbackAndMapped() {
        ClientIpResolver resolver = new ClientIpResolver(List.of("::1"));
        assertEquals("2001:db8::1", resolver.resolve(req("::1", "2001:db8::1")));
        // IPv4-mapped IPv6 归一化后可与 IPv4 可信代理匹配
        ClientIpResolver v4 = new ClientIpResolver(List.of("192.168.1.10"));
        assertEquals("198.51.100.3", v4.resolve(req("::ffff:192.168.1.10", "198.51.100.3")));
    }

    @Test
    void invalidCidrFailsFast() {
        try {
            new ClientIpResolver(List.of("not-an-ip"));
            org.junit.jupiter.api.Assertions.fail("非法 CIDR 应抛 IllegalArgumentException");
        } catch (IllegalArgumentException expected) {
            // 非法配置启动即失败（fail-fast），防止部署时静默配置错误
        }
    }
}
