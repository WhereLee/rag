# -*- coding: utf-8 -*-
"""P4 验收：L2 语义参考命中 + 长期记忆接入。

场景（对应计划）：
  1. 上传多主题文档 → 会话 S1 连续 5 轮提问 → 触发 maybe_extract → memory_entry 有 focus
  2. 会话 S2 问 Q1（写存档）→ 近义问题 Q2 → 精确不命中、L2 语义命中（meta.cache_ref=true）
     且回答质量不降（仍含核心事实）
  3. 会话 S3 问与 focus 相关的问题 → memory_hits>0（recall 有返回）
  4. edge：B 用户问同近义问题 → 不命中 L2（用户隔离）；删除文件后 L2 同失效
"""
import sys, io, json, time, uuid, urllib.request
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"c:\Users\lrs\Desktop\py\rag\rag-python\src")
BASE = "http://127.0.0.1:8082"
fails = 0

def req(method, path, body=None, token=None):
    r = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        r.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        r.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        resp = urllib.request.urlopen(r, timeout=180)
        raw = resp.read().decode("utf-8")
        if resp.headers.get("Content-Type", "").startswith("text/event-stream"):
            evs = [json.loads(l[5:].strip()) for l in raw.splitlines() if l.startswith("data:")]
            return resp.status, evs
        return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}

def check(name, cond, detail=""):
    global fails
    print(("[OK] " if cond else "[FAIL] ") + name + ("" if cond else " -> " + str(detail)))
    if not cond: fails += 1

# ---------- 1. 注册/登录 ----------
uname = "p4_user_" + time.strftime("%H%M%S")
pwd = "Passw0rd1"
req("POST", "/api/auth/register", {"username": uname, "password": pwd, "role": "user"})
st, login = req("POST", "/api/auth/login", {"username": uname, "password": pwd})
tok = login.get("token", "")
check("注册/登录", st == 200 and bool(tok), (st, login))

# ---------- 2. 上传多主题文档 → 等解析 ----------
content = ("生产服务器最低配置为 4 核 8G 内存，磁盘 100G。\n"
           "数据库使用 PostgreSQL 15，连接池最大 100。\n"
           "缓存采用 Redis 7，内存 2G。\n"
           "日志保留 30 天，自动轮转。")
fname = f"p4_{uuid.uuid4().hex[:8]}.txt"
boundary = uuid.uuid4().hex
body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n"
        f"Content-Type: text/plain\r\n\r\n{content}\r\n"
        f"--{boundary}--\r\n").encode("utf-8")
r = urllib.request.Request(BASE + "/api/files/upload", data=body, method="POST",
    headers={"Authorization": "Bearer " + tok, "Content-Type": f"multipart/form-data; boundary={boundary}"})
resp = urllib.request.urlopen(r, timeout=30)
up = json.loads(resp.read().decode("utf-8"))
check("上传文档", resp.status == 200 and "id" in up, up)
file_id = up.get("id")

ok = False
for _ in range(30):
    st, lst = req("GET", "/api/files?page=1&pageSize=20", None, tok)
    for it in lst.get("items", []):
        if it["id"] == file_id and it["parse_status"] in ("success", "partial"):
            ok = True
    if ok: break
    time.sleep(2)
check("文档解析完成", ok)

from db import pg_store
uid = pg_store.query_one("SELECT id AS user_id FROM kb_user WHERE username=%s", (uname,))["user_id"]

# ---------- 3. 建会话 + 提问辅助 ----------
def new_session():
    st, r = req("POST", "/api/qa/sessions", {}, tok)
    return r.get("session_id", "")

def ask(q, sid=None, token=tok):
    body = {"query": q}
    if sid:
        body["session_id"] = sid
    st, evs = req("POST", "/api/qa/ask", body, token)
    meta = next((e for e in evs if e.get("type") == "meta"), {})
    text = "".join(e.get("text", "") for e in evs if e.get("type") == "delta")
    return meta, text

s1 = new_session()
check("会话S1创建", bool(s1))

# ---------- 4. 会话 S1 连续 5 轮（触发 maybe_extract） ----------
# LLM 服务偶发空输出：每轮重试直到有回答（最多 5 次）；重试产生的空记录会计入轮次，
# 后续用补问把 turn 拉到 5 的倍数，保证 extract 必然触发（失败空回答也写入 qa_log）
rounds = [
    "服务器最低配置是什么",
    "数据库用的是什么版本",
    "缓存用什么",
    "日志保留多久",
    "磁盘空间多大",
]
for i, q in enumerate(rounds):
    got = False
    for attempt in range(5):
        meta, text = ask(q, s1)
        if len(text) > 0:
            got = True
            break
        time.sleep(3)
    check(f"S1第{i+1}轮有回答", got, f"attempts={attempt + 1}")

# 补问：直到 qa_log 轮次为 5 的倍数（当前轮次触发 extract；拒答也会写 qa_log）
extra = 0
for _ in range(12):
    n = pg_store.query_one(
        "SELECT count(*) AS n FROM qa_log WHERE session_id=%s", (s1,))["n"]
    if n > 0 and n % 5 == 0:
        break
    extra += 1
    ask(f"补充技术问题{extra}", s1)

# extract 在 ask 返回前同步完成，直接查询
mem_rows = pg_store.query(
    "SELECT mem_type, content FROM memory_entry WHERE user_id=%s ORDER BY id", (uid,))
check("5轮后 memory_entry 有记录", len(mem_rows) >= 1, [(m["mem_type"], m["content"][:20]) for m in mem_rows])
focus = [m for m in mem_rows if m["mem_type"] == "focus"]
check("包含 focus 类型", len(focus) >= 1, focus)

# ---------- 5. 会话 S2：Q0 写存档 → 近义 Q2 触发 L2 ----------
# 注意：S1 第 1 轮已问过"服务器最低配置是什么"（L1 存档用户级跨会话），
# S2 必须换新问题避免 L1 命中；Q0 与 Q2 语义近义（实测相似度 0.937 > 0.9 阈值）
s2 = new_session()
check("会话S2创建", bool(s2))
q0 = "服务器的硬件要求是什么"
meta1, text1 = ask(q0, s2)
check("S2问Q0全链路", meta1.get("cached") is not True and len(text1) > 0, (meta1, text1[:50]))

q2 = "服务器需要什么硬件"
meta2, text2 = ask(q2, s2)
check("近义词精确不命中", meta2.get("cached") is not True, meta2)
check("近义词 L2 语义命中", meta2.get("cache_ref") is True, meta2)
check("L2 回答质量不降(含核心事实)", ("4核" in text2.replace(" ", "")) or ("8G" in text2.replace(" ", "")), text2[:80])

# ---------- 6. 会话 S3：与 focus 相关 → recall 有返回 ----------
s3 = new_session()
meta3, text3 = ask("服务器硬件配置情况怎么样", s3)
check("S3 提问有回答", len(text3) > 0, text3[:50])
check("recall 命中(memory_hits>0)", (meta3.get("memory_hits") or 0) > 0, meta3)

# ---------- 7. edge：B 用户近义词不命中 L2 ----------
unameB = "p4_user_b_" + time.strftime("%H%M%S")
req("POST", "/api/auth/register", {"username": unameB, "password": pwd, "role": "user"})
st, loginB = req("POST", "/api/auth/login", {"username": unameB, "password": pwd})
tokB = loginB.get("token", "")
st, sessB = req("POST", "/api/qa/sessions", {}, tokB)
metaB, textB = ask(q2, sessB.get("session_id", ""), tokB)
check("B 用户 L2 不命中(无存档)", metaB.get("cache_ref") is not True and metaB.get("cached") is not True, metaB)
check("B 用户无文档拒答或正常回答", metaB.get("rejected") is True or len(textB) > 0, metaB)

# ---------- 8. edge：删除文件后 L2 失效 ----------
st, r = req("DELETE", f"/api/files/{file_id}", None, tok)
check("删除文档", st == 200, (st, r))
meta4, text4 = ask("服务器需要什么硬件", s2)
check("删除后 L2 不命中", meta4.get("cache_ref") is not True and meta4.get("cached") is not True, meta4)
check("删除后拒答", meta4.get("rejected") is True, meta4)

print("P4:", "PASS" if fails == 0 else f"{fails} FAILED")
