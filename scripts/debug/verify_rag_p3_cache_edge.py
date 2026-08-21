# -*- coding: utf-8 -*-
"""P3 问答存档验收 edge：删除文件→缓存失效→重传同内容（秒传+重新解析）→缓存重建。

场景（对应计划 "reparse（重传同内容触发重新解析）后缓存失效再问走全链路"）：
  上传 A → 提问 Q（全链路写入缓存）→ 同问 cached=true
  → 软删 A（Java 直连失效 SQL）→ DB invalidated=true → 再问 Q 走全链路（拒答）
  → 重传同内容 → 新文件 B 解析完成 → 问 Q 全链路回答 → 缓存重建
  → 同问 Q cached=true（重建成功）
  附加：query 带多余空格/全角标点归一化命中、拒答不存档（normal 已覆盖，此处交叉验证）
"""
import sys, io, json, time, uuid, urllib.request
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"c:\Users\lrs\Desktop\py\rag\rag-python\src")
BASE = "http://127.0.0.1:8082"
fails = 0

def req(method, path, body=None, token=None, raw=False):
    r = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        r.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        r.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        resp = urllib.request.urlopen(r, timeout=180)
        data = resp.read()
        if resp.headers.get("Content-Type", "").startswith("text/event-stream"):
            txt = data.decode("utf-8")
            evs = [json.loads(l[5:].strip()) for l in txt.splitlines() if l.startswith("data:")]
            return resp.status, evs
        return resp.status, (data if raw else json.loads(data.decode("utf-8") if data else "{}"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}

def check(name, cond, detail=""):
    global fails
    print(("[OK] " if cond else "[FAIL] ") + name + ("" if cond else " -> " + str(detail)))
    if not cond: fails += 1

# ---------- 1. 注册/登录（随机账号，避开注册限流与登录锁定） ----------
uname = "p3_edge_" + time.strftime("%H%M%S")
pwd = "Passw0rd1"
req("POST", "/api/auth/register", {"username": uname, "password": pwd, "role": "user"})
st, login = req("POST", "/api/auth/login", {"username": uname, "password": pwd})
tok = login.get("token", "")
check("注册/登录", st == 200 and bool(tok), (st, login))

# ---------- 2. 上传文件 A → 等解析 ----------
content = "P3_EDGE_A 标识。生产服务器最低配置为 8 核 16G 内存，磁盘 200G。"
fname = f"p3_{uuid.uuid4().hex[:8]}.txt"
boundary = uuid.uuid4().hex
body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n"
        f"Content-Type: text/plain\r\n\r\n{content}\r\n"
        f"--{boundary}--\r\n").encode("utf-8")
r = urllib.request.Request(BASE + "/api/files/upload", data=body, method="POST",
    headers={"Authorization": "Bearer " + tok, "Content-Type": f"multipart/form-data; boundary={boundary}"})
resp = urllib.request.urlopen(r, timeout=30)
up = json.loads(resp.read().decode("utf-8"))
check("上传文件A", resp.status == 200 and "id" in up, up)
file_a = up.get("id")

ok = False
for _ in range(30):
    st, lst = req("GET", "/api/files?page=1&pageSize=20", None, tok)
    for it in lst.get("items", []):
        if it["id"] == file_a and it["parse_status"] in ("success", "partial"):
            ok = True
    if ok: break
    time.sleep(2)
check("文件A解析完成", ok)

# ---------- 3. 提问 Q → 全链路 → 缓存写入 ----------
Q = "服务器最低配置是什么"
def ask(q, token=tok):
    st, evs = req("POST", "/api/qa/ask", {"query": q}, token)
    meta = next((e for e in evs if e.get("type") == "meta"), {})
    text = "".join(e.get("text", "") for e in evs if e.get("type") == "delta")
    return meta, text

from db import pg_store
uid = pg_store.query_one("SELECT id AS user_id FROM kb_user WHERE username=%s", (uname,))["user_id"]

meta1, text1 = ask(Q)
check("首次全链路(无cached)", meta1.get("cached") is not True and len(meta1.get("citations", [])) > 0, meta1)
check("首次回答正确", "8核" in text1.replace(" ", ""), text1[:60])
row = pg_store.query_one(
    "SELECT id, invalidated, file_ids FROM qa_cache WHERE user_id=%s AND query_hash=md5(%s)",
    (uid, Q))
check("缓存已写入(未失效)", row is not None and row["invalidated"] is False, row)
check("缓存引用文件A", row is not None and file_a in (row["file_ids"] or []), row)

meta2, text2 = ask(Q)
check("同问命中 cached=true", meta2.get("cached") is True, meta2)

# ---------- 4. 软删文件 A → 缓存失效 ----------
st, r = req("DELETE", f"/api/files/{file_a}", None, tok)
check("软删文件A", st == 200, (st, r))
row = pg_store.query_one(
    "SELECT invalidated FROM qa_cache WHERE user_id=%s AND query_hash=md5(%s)", (uid, Q))
check("删除后缓存 invalidated=true", row is not None and row["invalidated"] is True, row)

# ---------- 5. 再问 Q → 不走缓存（文件没了 → 拒答） ----------
meta3, text3 = ask(Q)
check("删除后再问不走缓存", meta3.get("cached") is not True, meta3)
check("删除后再问拒答", meta3.get("rejected") is True, meta3)

# ---------- 6. 重传同内容（秒传复用 blob + 新文件重新解析） ----------
boundary2 = uuid.uuid4().hex
body2 = (f"--{boundary2}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n"
         f"Content-Type: text/plain\r\n\r\n{content}\r\n"
         f"--{boundary2}--\r\n").encode("utf-8")
r2 = urllib.request.Request(BASE + "/api/files/upload", data=body2, method="POST",
    headers={"Authorization": "Bearer " + tok, "Content-Type": f"multipart/form-data; boundary={boundary2}"})
resp2 = urllib.request.urlopen(r2, timeout=30)
up2 = json.loads(resp2.read().decode("utf-8"))
check("重传同内容(新文件)", resp2.status == 200 and "id" in up2 and up2["id"] != file_a, up2)
file_b = up2.get("id")

ok2 = False
for _ in range(30):
    st, lst = req("GET", "/api/files?page=1&pageSize=20", None, tok)
    for it in lst.get("items", []):
        if it["id"] == file_b and it["parse_status"] in ("success", "partial"):
            ok2 = True
    if ok2: break
    time.sleep(2)
check("新文件B解析完成", ok2)

# ---------- 7. 再问 Q → 全链路 → 缓存重建 ----------
meta4, text4 = ask(Q)
check("重传后再问走全链路", meta4.get("cached") is not True and len(meta4.get("citations", [])) > 0, meta4)
check("重传后回答正确", "8核" in text4.replace(" ", ""), text4[:60])
row = pg_store.query_one(
    "SELECT id, invalidated, file_ids FROM qa_cache WHERE user_id=%s AND query_hash=md5(%s)",
    (uid, Q))
check("缓存重建(未失效且引用文件B)", row is not None and row["invalidated"] is False and file_b in (row["file_ids"] or []), row)

meta5, text5 = ask(Q)
check("重建后同问命中", meta5.get("cached") is True, meta5)

# ---------- 8. 归一化交叉验证：多余空格 + 全角标点 ----------
meta6, _ = ask("  服务器最低配置是什么？　")
check("归一化命中(空格+全角问号)", meta6.get("cached") is True, meta6)

# ---------- 9. 拒答不存档交叉验证（查询与文档内容完全无关，触发拒答） ----------
metaR, _ = ask("法式马卡龙的制作方法是什么")
rowR = pg_store.query_one(
    "SELECT id FROM qa_cache WHERE user_id=%s AND query_hash=md5('法式马卡龙的制作方法是什么')",
    (uid,))
check("拒答未写存档", metaR.get("rejected") is True and rowR is None, (metaR, rowR))

print("P3 edge:", "PASS" if fails == 0 else f"{fails} FAILED")
