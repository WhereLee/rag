# -*- coding: utf-8 -*-
"""P3 问答存档验收 normal（自清理）：首次全链路→二次精确命中→归一化命中→用户隔离→拒答不存档。"""
import sys, io, json, time, urllib.request
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

# 自清理：清掉两个测试用户的存档，保证首次必然全链路
from db import pg_store
pg_store.execute("DELETE FROM qa_cache WHERE user_id IN (SELECT id FROM kb_user WHERE username IN ('e2e_demo_2026','e2e_demo_b'))")
print("-- qa_cache 已清理 --")

_, login = req("POST", "/api/auth/login", {"username": "e2e_demo_2026", "password": "Passw0rd1"})
tok = login.get("token", "")
check("登录", bool(tok), login)

def ask(q, token=tok):
    st, evs = req("POST", "/api/qa/ask", {"query": q}, token)
    meta = next((e for e in evs if e.get("type") == "meta"), {})
    text = "".join(e.get("text", "") for e in evs if e.get("type") == "delta")
    return meta, text

meta1, text1 = ask("生产服务器最低配置是什么")
check("首次走全链路(无cached)", meta1.get("cached") is not True and len(meta1.get("citations", [])) > 0, meta1)
check("首次回答正确", "4核" in text1.replace(" ", "") or "4 核" in text1, text1[:60])

t0 = time.time()
meta2, text2 = ask("生产服务器最低配置是什么")
check("二次命中 cached=true", meta2.get("cached") is True, meta2)
check("命中无检索引用", len(meta2.get("citations", [])) == 0, meta2)
check("答案一致", text2 == text1, (text1[:40], text2[:40]))
check("命中秒回(<5s)", (time.time() - t0) < 5, round(time.time() - t0, 2))

meta3, text3 = ask("生产服务器最低配置是什么？　")
check("全角/空白归一化命中", meta3.get("cached") is True, meta3)

_, loginB = req("POST", "/api/auth/login", {"username": "e2e_demo_b", "password": "Passw0rd1"})
tokB = loginB.get("token", "")
metaB, textB = ask("生产服务器最低配置是什么", tokB)
check("B 用户不命中缓存(无记录)", metaB.get("cached") is not True, metaB)
check("B 独立回答", len(textB) > 0, textB[:60])

metaR, _ = ask("p2_ui_doc 文件里的 P2_UI_MARK_8842 是什么", tok)
check("A 拒答", metaR.get("rejected") is True, metaR)
row = pg_store.query_one("""SELECT qc.id FROM qa_cache qc
    JOIN kb_user u ON u.id=qc.user_id WHERE u.username='e2e_demo_2026' AND qc.query LIKE '%%P2_UI_MARK%%'""")
check("拒答未写存档", row is None, row)

print("P3 normal:", "PASS" if fails == 0 else f"{fails} FAILED")
