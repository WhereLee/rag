# -*- coding: utf-8 -*-
"""反馈闭环验收：done 事件带 qa_log_id → 点赞/点踩提交 → bad case 自动归因。

覆盖：
1. 全链路回答：done.qa_log_id 非空
2. 缓存命中回答：done.qa_log_id 非空（缓存路径新增落库）
3. 点赞提交成功（feedback 表落库）
4. 点踩 + 纠错 → bad_case 生成且自动归因（retrieval/generation 非 pending）
5. 无效 qa_log_id → 404
"""
import sys, io, json, time, urllib.request, urllib.error
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8082"
fails = 0

def check(name, cond, detail=""):
    global fails
    print(("PASS" if cond else "FAIL") + f" | {name}" + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        fails += 1

def req(method, path, body=None, token=None, timeout=300):
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

login = req("POST", "/api/auth/login", {"username": "browser_p3_6831051", "password": "Passw0rd1"})
tok = login[1]["token"]
check("登录", login[0] == 200)

# 1. 缓存命中路径：问已缓存问题 → done 带 qa_log_id
st, evs = req("POST", "/api/qa/ask", {"query": "部署手册里服务器的内存要求是多少"}, tok)
done = next((e for e in evs if e.get("type") == "done"), None)
meta = next((e for e in evs if e.get("type") == "meta"), {})
check("缓存命中回答 done 带 qa_log_id", st == 200 and meta.get("cached") and done and done.get("qa_log_id"),
      f"st={st} cached={meta.get('cached')} qa_log_id={done and done.get('qa_log_id')}")
cached_log_id = done and done.get("qa_log_id")

# 2. 缓存命中回答提交点赞
st, body = req("POST", "/api/admin/proxy/api/feedback",
               {"qa_log_id": cached_log_id, "rating": 1}, tok, timeout=60)
check("缓存命中回答可点赞（feedback 落库）", st == 200 and body.get("feedback_id"),
      f"st={st} body={str(body)[:120]}")

# 3. 全链路路径：用时间戳随机问题（必然未缓存；拒答也落 qa_log，qa_log_id 同样有效）
import sys as _sys
_sys.path.insert(0, r"c:\Users\lrs\Desktop\py\rag\rag-python\src")
from db import pg_store
uid = pg_store.query_one("SELECT id FROM kb_user WHERE username='browser_p3_6831051'")["id"]
cached_qs = {r["query"] for r in pg_store.query("SELECT query FROM qa_cache WHERE user_id=%s", (uid,))}
candidate = f"部署方式建议验证_{int(time.time())}"
check("测试问题未撞缓存（坑位 #33 教训）", candidate not in cached_qs, f"candidate={candidate}")

st, evs = req("POST", "/api/qa/ask", {"query": candidate}, tok)
done = next((e for e in evs if e.get("type") == "done"), None)
meta = next((e for e in evs if e.get("type") == "meta"), {})
check("全链路回答 done 带 qa_log_id", st == 200 and not meta.get("cached") and done and done.get("qa_log_id"),
      f"st={st} qa_log_id={done and done.get('qa_log_id')}")
full_log_id = done and done.get("qa_log_id")

# 4. 全链路回答提交点踩 + 纠错 → bad_case + 自动归因（LLM 归因可能较慢，超时 120s）
st, body = req("POST", "/api/admin/proxy/api/feedback",
               {"qa_log_id": full_log_id, "rating": -1,
                "correction": "回答没有给出部署方式的具体建议"}, tok, timeout=180)
check("点踩提交成功且生成 bad_case", st == 200 and body.get("bad_case_id"),
      f"st={st} body={str(body)[:200]}")
if body.get("bad_case_id"):
    # 归因已异步化：轮询等待后台 LLM 归因完成（最多 120s）
    bc = None
    for _ in range(24):
        bc = pg_store.query_one("SELECT attribution, status FROM bad_case WHERE id=%s", (body["bad_case_id"],))
        if bc and bc["attribution"] in ("retrieval", "generation"):
            break
        time.sleep(5)
    check("bad_case 自动归因完成（非 pending）", bc and bc["attribution"] in ("retrieval", "generation"),
          f"attribution={bc and bc['attribution']} status={bc and bc['status']}")

# 5. 无效 qa_log_id → 404
st, body = req("POST", "/api/admin/proxy/api/feedback",
               {"qa_log_id": 999999999, "rating": 1}, tok, timeout=60)
check("无效 qa_log_id 返回 404", st == 404, f"st={st} body={str(body)[:100]}")

print(f"\n{fails} FAIL / 共 7 项")
sys.exit(1 if fails else 0)
