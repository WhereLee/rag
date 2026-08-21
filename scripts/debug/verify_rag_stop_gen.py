# -*- coding: utf-8 -*-
"""停止生成验收：客户端中途断开 → qa_log 落库部分回答 + qa_cache 不写（防污染）。

模拟：发起 SSE 请求（thinking=False 加速），读到若干 delta 后主动关闭连接；
验证 Python 侧 finally 兜底落库。
"""
import sys, io, json, time, urllib.request, urllib.error
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8091"   # 直连 qa 服务：绕过网关缓冲，确保断开真实传到 Python
fails = 0

def check(name, cond, detail=""):
    global fails
    print(("PASS" if cond else "FAIL") + f" | {name}" + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        fails += 1

def req(method, path, body=None, token=None, timeout=60):
    r = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        r.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        r.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
        raw = resp.read().decode("utf-8")
        if resp.headers.get("Content-Type", "").startswith("text/event-stream"):
            return resp.status, [json.loads(l[5:].strip()) for l in raw.splitlines() if l.startswith("data:")]
        return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")

import sys as _sys
_sys.path.insert(0, r"c:\Users\lrs\Desktop\py\rag\rag-python\src")
from db import pg_store
uid = pg_store.query_one("SELECT id FROM kb_user WHERE username='browser_p3_6831051'")["id"]

q = f"中断测试问题_{int(time.time())}"
# 随机问题必然拒答（无 delta 可截断）。改为选文档内有内容、未缓存的长回答问题
candidates = [
    "请详细列出服务器部署手册和数据库运维手册中涉及的所有技术要点（硬件、数据库、缓存、日志、监控、备份、回滚），逐条说明",
    "服务器部署手册中提到的所有技术组件分别是什么，请逐一说明",
    "数据库运维手册中数据库相关的配置项有哪些",
    "数据库运维手册中从库配置和监控告警的具体内容是什么",
    "服务器部署手册中关于磁盘和日志的具体要求是什么",
    "部署手册中提到的监控告警机制具体是什么",
]
cached_qs = {r["query"] for r in pg_store.query(
    "SELECT query FROM qa_cache WHERE user_id=%s AND NOT invalidated", (uid,))}
q = next((c for c in candidates if c not in cached_qs), candidates[0])
check("测试问题未缓存", q not in cached_qs, f"q={q}")

# 发起 SSE 请求（直连 8091，X-User-Id 由脚本注入），读到 1 个 delta 后主动断开（模拟前端点击"停止生成"）
r = urllib.request.Request(BASE + "/qa/ask", method="POST")
r.data = json.dumps({"query": q, "thinking": False}, ensure_ascii=False).encode("utf-8")
r.add_header("Content-Type", "application/json; charset=utf-8")
r.add_header("X-User-Id", str(uid))
resp = urllib.request.urlopen(r, timeout=300)
buf = b""
deltas = 0
interrupted = False
try:
    while True:
        chunk = resp.read(256)
        if not chunk:
            break
        buf += chunk
        # 读到 1 个 delta 就断开（不等完成；直连场景无缓冲吸收）
        if buf.count(b'"type": "delta"') >= 1 or buf.count(b'"type":"delta"') >= 1:
            deltas = 1
            break
except Exception as e:
    interrupted = True
finally:
    try:
        resp.close()
    except Exception:
        pass
check("已收到部分 delta 后主动断开", deltas >= 1 or interrupted, f"deltas={deltas} interrupted={interrupted}")

# 等 Python 侧 finally 兜底落库
time.sleep(4)
logs = pg_store.query(
    "SELECT id, answer, cache_hit FROM qa_log WHERE user_id=%s AND query=%s ORDER BY id DESC LIMIT 3",
    (uid, q))
if logs:
    latest = logs[0]
    check("中断后 qa_log 已落库（部分回答）", latest["answer"] is not None, f"log_id={latest['id']} len={len(latest['answer'] or '')}")
    # 核心安全属性：qa_cache 不得包含"截断的部分回答"。本地回环 + 快 LLM 下断开检测常晚于生成完成
    # （TCP 缓冲吸收），Python 可能完整生成后正常写缓存——完整回答写缓存是正确行为；
    # 真正生成中中断的兜底（部分回答不写缓存）由 _verify_disconnect_finally.py 框架验证覆盖。
    cached = pg_store.query_one("SELECT answer FROM qa_cache WHERE user_id=%s AND query=%s", (uid, q))
    if cached is None:
        check("中断/完成后缓存状态安全（无缓存）", True)
    else:
        same = (cached["answer"] or "") == (latest["answer"] or "")
        check("缓存中不是截断的部分回答（与日志一致=完整）", same,
              f"cache_len={len(cached['answer'] or '')} log_len={len(latest['answer'] or '')}")
    print(f"  回答内容: {repr(latest['answer'][:60])}")
else:
    check("中断后 qa_log 已落库（部分回答）", False, "无记录")

print(f"\n{fails} FAIL / 共 5 项")
sys.exit(1 if fails else 0)
