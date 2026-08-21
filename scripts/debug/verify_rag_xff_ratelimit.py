# -*- coding: utf-8 -*-
"""坑位 #1 集成验收：默认配置（无可信代理）下伪造 X-Forwarded-For 无法绕过注册限流。

验证逻辑：
- 同一真实 IP 连续注册 4 个用户，每次带不同的伪造 X-Forwarded-For
- 注册限流 3 次/分：第 4 次必须 429（说明限流按真实 IP 计数，伪造 XFF 无效）
- 对照组（修复前行为）：每次换 XFF 都会通过 → 第 4 次不是 429
"""
import sys, io, json, time, urllib.request
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8082"
fails = 0

def check(name, cond, detail=""):
    global fails
    print(("PASS" if cond else "FAIL") + f" | {name}" + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        fails += 1

def register(username, forged_xff):
    r = urllib.request.Request(BASE + "/api/auth/register", method="POST")
    r.data = json.dumps({"username": username, "password": "Passw0rd1", "role": "user"},
                        ensure_ascii=False).encode("utf-8")
    r.add_header("Content-Type", "application/json; charset=utf-8")
    r.add_header("X-Forwarded-For", forged_xff)
    try:
        resp = urllib.request.urlopen(r, timeout=15)
        return resp.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")

# 每个伪造 XFF 都不同（攻击者视角：换 IP 绕过限流）
forged_ips = ["203.0.113.101", "203.0.113.102", "203.0.113.103", "203.0.113.104"]
st_codes = []
for i, fip in enumerate(forged_ips):
    uname = f"xff_{i}_{int(time.time())}"
    st, body = register(uname, fip)
    st_codes.append(st)
    print(f"  第 {i+1} 次注册 (XFF={fip}) -> {st}")

# 前 3 次应成功（200/201/4xx业务错但非限流），第 4 次必须 429（限流）
# 注意：用户名重复会返回 4xx（业务错误），但限流未触发时不会是 429
ok_first3 = all(c != 429 for c in st_codes[:3])
fourth = st_codes[3]
check("前 3 次注册未被限流（不同 XFF 均通过）", ok_first3, f"codes={st_codes[:3]}")
check("第 4 次注册被 429 拦截（伪造 XFF 绕不过限流）", fourth == 429, f"actual={fourth}")

print(f"\n{fails} FAIL / 共 2 项")
sys.exit(1 if fails else 0)
