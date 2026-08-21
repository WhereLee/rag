# -*- coding: utf-8 -*-
"""P2 后端链路验证：会话创建/列表/历史 + ask 带 session + 归属校验 + qa_log 落库。"""
import sys, io, json, time, urllib.request
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
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
            evs = []
            for line in raw.splitlines():
                if line.startswith("data:"):
                    evs.append(json.loads(line[5:].strip()))
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

# 用户：复用 e2e_demo_2026（已有文档 服务器部署手册.md）
_, login = req("POST", "/api/auth/login", {"username": "e2e_demo_2026", "password": "Passw0rd1"})
tok = login.get("token", "")
check("登录", bool(tok), login)

# 1. 创建会话（不绑目录）
st, s1 = req("POST", "/api/qa/sessions", {"summary": "P2测试会话"}, tok)
sid1 = s1.get("session_id")
check("创建会话", st == 200 and sid1, (st, s1))

# 2. 会话列表
st, lst = req("GET", "/api/qa/sessions", None, tok)
check("会话列表含新会话", st == 200 and any(i["session_id"] == sid1 for i in lst.get("items", [])),
      (st, [i["session_id"] for i in lst.get("items", [])]))

# 3. ask 第一轮（带 session）
st, evs = req("POST", "/api/qa/ask", {"query": "生产服务器最低配置是什么", "session_id": sid1}, tok)
meta = next(e for e in evs if e.get("type") == "meta")
text1 = "".join(e.get("text", "") for e in evs if e.get("type") == "delta")
check("第一轮问答不拒答", not meta.get("rejected"), meta)
check("第一轮回答含关键内容", "4核" in text1.replace(" ", "") or "4 核" in text1, text1[:80])

# 4. qa_log 落库
import sys as _s
_s.path.insert(0, r"c:\Users\lrs\Desktop\py\rag\rag-python\src")
from db import pg_store
row = pg_store.query_one("SELECT query, answer, route, chunk_ids FROM qa_log WHERE session_id=%s AND user_id=(SELECT id FROM kb_user WHERE username='e2e_demo_2026') ORDER BY id DESC LIMIT 1", (sid1,))
check("qa_log 落库", row is not None and row["route"] == "qa" and row["chunk_ids"], row)

# 5. 第二轮（追问，验证历史注入不报错）
st, evs2 = req("POST", "/api/qa/ask", {"query": "那回滚时长要求呢？", "session_id": sid1}, tok)
meta2 = next(e for e in evs2 if e.get("type") == "meta")
text2 = "".join(e.get("text", "") for e in evs2 if e.get("type") == "delta")
check("第二轮追问可答", not meta2.get("rejected") and "15" in text2 and "分钟" in text2, (meta2, text2[:80]))

# 6. 历史接口
st, hist = req("GET", f"/api/qa/sessions/{sid1}/history", None, tok)
check("历史接口 2 轮", st == 200 and len(hist.get("items", [])) == 2, (st, len(hist.get("items", []))))

# 7. 归属校验：用户 B 用 A 的会话 → 403
_, loginB = req("POST", "/api/auth/login", {"username": "e2e_demo_b", "password": "Passw0rd1"})
tokB = loginB.get("token", "")
st, r = req("POST", "/api/qa/ask", {"query": "测试", "session_id": sid1}, tokB)
check("B 用 A 会话 403", st == 200 and any(e.get("code") == 403 for e in r), (st, r[:2]))
st, r = req("GET", f"/api/qa/sessions/{sid1}/history", None, tokB)
check("B 读 A 历史 403", st == 403, (st, r))

# 8. 不存在会话 → 403
st, r = req("POST", "/api/qa/ask", {"query": "测试", "session_id": "no_such_session"}, tokA if (tokA := tok) else tok)
check("不存在会话 403", st == 200 and any(e.get("code") == 403 for e in r), (st, r[:2]))

# 9. 会话绑定目录：创建带 dir 的会话（e2e_demo_2026 有目录吗？没有就跳过）
st, dirs = req("GET", "/api/dirs", None, tok)
dirs_list = dirs.get("items", [])
if dirs_list:
    d0 = dirs_list[0]["id"]
    st, s2 = req("POST", "/api/qa/sessions", {"dir_id": d0}, tok)
    check("创建目录会话", st == 200 and s2.get("session_id"), (st, s2))
    st, lst2 = req("GET", f"/api/qa/sessions?dir_id={d0}", None, tok)
    check("目录过滤会话列表", st == 200 and len(lst2.get("items", [])) >= 1, (st, lst2))
else:
    print("(跳过目录会话验证：该用户无目录)")

print("P2 backend:", "PASS" if fails == 0 else f"{fails} FAILED")
