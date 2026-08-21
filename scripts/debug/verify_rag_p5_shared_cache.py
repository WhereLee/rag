# -*- coding: utf-8 -*-
"""跨用户问答复用验收：相同文件（同 blob）+ 相同/近似问题 → 复用他人回答，不显示来源。

normal：A 上传 F → 问 Q 全链路；B 上传同内容 F'（秒传同 blob）→ 问同 Q 直接复用
        （cached+cache_shared、秒回、答案一致）；B 再问 → B 自己 L1 命中
edge：C（无同 blob 文件）不复用；A 独有文件 F2 的问答不复用给 B（内容边界）；
      B 问近义词 → L2 跨用户参考注入（cache_ref）
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
        resp = urllib.request.urlopen(r, timeout=300)
        raw = resp.read().decode("utf-8")
        if resp.headers.get("Content-Type", "").startswith("text/event-stream"):
            evs = [json.loads(l[5:].strip()) for l in raw.splitlines() if l.startswith("data:")]
        else:
            evs = json.loads(raw) if raw else {}
        return resp.status, evs
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}

def check(name, cond, detail=""):
    global fails
    print(("[OK] " if cond else "[FAIL] ") + name + ("" if cond else " -> " + str(detail)))
    if not cond: fails += 1

def new_user(tag):
    uname = f"p5_{tag}_{time.strftime('%H%M%S')}"
    # 注册限流每 IP 3 次/分：连续注册多个用户可能 429，等待窗口重试（最长 ~2.5 分钟）
    for _ in range(6):
        st, _ = req("POST", "/api/auth/register", {"username": uname, "password": "Passw0rd1", "role": "user"})
        if st != 429:
            break
        time.sleep(25)
    st, login = req("POST", "/api/auth/login", {"username": uname, "password": "Passw0rd1"})
    return uname, login.get("token", "")

def upload(tok, fname, content):
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n"
            f"Content-Type: text/plain\r\n\r\n{content}\r\n"
            f"--{boundary}--\r\n").encode("utf-8")
    r = urllib.request.Request(BASE + "/api/files/upload", data=body, method="POST",
        headers={"Authorization": "Bearer " + tok, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    resp = urllib.request.urlopen(r, timeout=30)
    return json.loads(resp.read().decode("utf-8"))

def wait_parse(tok, fid):
    for _ in range(30):
        st, lst = req("GET", "/api/files?page=1&pageSize=50", None, tok)
        for it in lst.get("items", []):
            if it["id"] == fid and it["parse_status"] in ("success", "partial"):
                return True
        time.sleep(2)
    return False

def ask(q, token, sid=None):
    body = {"query": q}
    if sid:
        body["session_id"] = sid
    st, evs = req("POST", "/api/qa/ask", body, token)
    if not isinstance(evs, list):
        return {}, ""   # 非 SSE（如 401/429）防御
    meta = next((e for e in evs if e.get("type") == "meta"), {})
    text = "".join(e.get("text", "") for e in evs if e.get("type") == "delta")
    return meta, text

from db import pg_store

# ---------- 1. A 上传 F（共享）与 F2（独有） ----------
uname_a, tok_a = new_user("a")
UNIQ = uuid.uuid4().hex[:8]    # 每轮唯一标记：避免与历史运行的同内容文件（同 blob）互相命中
UNIQ2 = uuid.uuid4().hex[:8]   # F2 独立标记：与共享文件内容零重叠（保证 B 查询 Q2 时无关键词命中）
F_CONTENT = f"共享文档标记 SHARED_MARK_{UNIQ}。生产服务器最低配置为 4 核 8G 内存，磁盘 100G。"
F2_CONTENT = f"独有文档标记 PRIVATE_MARK_{UNIQ2}。数据库使用 PostgreSQL 15，连接池最大 100。"
fa = upload(tok_a, f"p5_share_{uuid.uuid4().hex[:6]}.txt", F_CONTENT)
fa2 = upload(tok_a, f"p5_priv_{uuid.uuid4().hex[:6]}.txt", F2_CONTENT)
check("A 上传 F/F2", "id" in fa and "id" in fa2, (fa, fa2))
check("F 解析完成", wait_parse(tok_a, fa["id"]))
check("F2 解析完成", wait_parse(tok_a, fa2["id"]))

# ---------- 2. A 问 Q / Q2 → 全链路写缓存 ----------
meta_a1, text_a1 = ask("生产服务器最低配置是什么", tok_a)
check("A 问 Q 全链路", meta_a1.get("cached") is not True and len(text_a1) > 0, meta_a1)
meta_a2, text_a2 = ask(f"PRIVATE_MARK_{UNIQ2} 是什么", tok_a)
check("A 问 Q2 全链路", meta_a2.get("cached") is not True and len(text_a2) > 0, meta_a2)

uid_a = pg_store.query_one("SELECT id AS user_id FROM kb_user WHERE username=%s", (uname_a,))["user_id"]
row_q = pg_store.query_one("SELECT id, file_ids, cache_shared_from FROM qa_cache WHERE user_id=%s AND query_hash=md5('生产服务器最低配置是什么')", (uid_a,))
row_q2 = pg_store.query_one("SELECT id, file_ids FROM qa_cache WHERE user_id=%s AND query_hash=md5(%s)", (uid_a, f"PRIVATE_MARK_{UNIQ2} 是什么"))
check("A 缓存 Q 引用 F", row_q and fa["id"] in (row_q["file_ids"] or []), row_q)
check("A 缓存 Q2 引用 F2", row_q2 and fa2["id"] in (row_q2["file_ids"] or []), row_q2)

# ---------- 3. B 上传同内容 F'（秒传同 blob）→ 问同 Q → 复用 ----------
uname_b, tok_b = new_user("b")
fb = upload(tok_b, f"p5_share_b_{uuid.uuid4().hex[:6]}.txt", F_CONTENT)
check("B 上传同内容 F'", "id" in fb, fb)
check("F' 解析完成", wait_parse(tok_b, fb["id"]))

t0 = time.time()
meta_b1, text_b1 = ask("生产服务器最低配置是什么", tok_b)
check("B 问同 Q 直接复用", meta_b1.get("cached") is True and meta_b1.get("cache_shared") is True, meta_b1)
check("B 复用秒回(<5s)", (time.time() - t0) < 5, round(time.time() - t0, 2))
check("B 复用答案一致", text_b1 == text_a1, (text_a1[:40], text_b1[:40]))

uid_b = pg_store.query_one("SELECT id AS user_id FROM kb_user WHERE username=%s", (uname_b,))["user_id"]
row_b = pg_store.query_one(
    "SELECT id, file_ids, cache_shared_from, query_embedding IS NOT NULL AS has_emb FROM qa_cache WHERE user_id=%s AND query_hash=md5('生产服务器最低配置是什么')", (uid_b,))
check("B 缓存已写入(引用 F' 且标记来源)", row_b and fb["id"] in (row_b["file_ids"] or []) and row_b["cache_shared_from"] == row_q["id"] and row_b["has_emb"], row_b)

meta_b2, text_b2 = ask("生产服务器最低配置是什么", tok_b)
check("B 再问同 Q 走本人缓存", meta_b2.get("cached") is True and meta_b2.get("cache_shared") is not True, meta_b2)

# ---------- 4. edge：B 问 A 独有文件的问题 → 不复用 ----------
meta_b3, text_b3 = ask(f"PRIVATE_MARK_{UNIQ2} 是什么", tok_b)
check("B 问 Q2 不复用(内容边界)", meta_b3.get("cached") is not True and meta_b3.get("cache_shared") is not True, meta_b3)
check("B 问 Q2 无资料拒答", meta_b3.get("rejected") is True, meta_b3)

# ---------- 5. edge：C 无同 blob 文件 → 不复用 ----------
uname_c, tok_c = new_user("c")
meta_c, text_c = ask("生产服务器最低配置是什么", tok_c)
check("C(无同文件)不复用", meta_c.get("cached") is not True and meta_c.get("cache_shared") is not True, meta_c)

# ---------- 6. edge：B 问近义词 → L2 跨用户参考注入 ----------
# A 先问"服务器的硬件要求是什么"（B 未问过，避免本人缓存干扰）
meta_a3, text_a3 = ask("服务器的硬件要求是什么", tok_a)
check("A 问硬件要求全链路", meta_a3.get("cached") is not True, meta_a3)
meta_b4, text_b4 = ask("服务器需要什么硬件", tok_b)
check("B 近义词 L2 跨用户参考注入", meta_b4.get("cache_ref") is True and meta_b4.get("cached") is not True, meta_b4)
check("B 近义词回答正常", len(text_b4) > 0, text_b4[:60])

print("P5 shared cache:", "PASS" if fails == 0 else f"{fails} FAILED")
