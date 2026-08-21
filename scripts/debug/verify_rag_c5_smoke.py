# -*- coding: utf-8 -*-
"""最终冒烟：端口健康 + 登录 + 问答（含低分剔除验证）。以 UTF-8 文件方式运行避开 PS 管道编码坑。"""
import sys, io, json, urllib.request
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 自清理：P3 问答存档可能残留历史命中（B 用户隔离用例要求无缓存时真实拒答）
sys.path.insert(0, r"c:\Users\lrs\Desktop\py\rag\rag-python\src")
try:
    from db import pg_store
    pg_store.execute(
        "DELETE FROM qa_cache WHERE user_id IN "
        "(SELECT id FROM kb_user WHERE username IN ('e2e_demo_2026','e2e_demo_b'))")
except Exception as e:
    print("[warn] qa_cache clean failed:", e)

def check(name, cond, detail=""):
    print(("[OK] " if cond else "[FAIL] ") + name + ("" if cond else " -> " + str(detail)))
    return cond

fail = 0
# 1. 端口健康
for name, url in [("网关 8082", "http://127.0.0.1:8082/health"), ("qa 8091", "http://127.0.0.1:8091/docs"),
                  ("前端 3000", "http://127.0.0.1:3000/login"), ("Swagger", "http://127.0.0.1:8082/swagger-ui/index.html")]:
    try:
        code = urllib.request.urlopen(url, timeout=5).status
        fail += 0 if check(name, code == 200, code) else 1
    except Exception as e:
        fail += 0 if check(name, False, e) else 1

# 2. 登录 + 问答
req = urllib.request.Request("http://127.0.0.1:8082/api/auth/login",
    data=json.dumps({"username": "e2e_demo_2026", "password": "Passw0rd1"}).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
tok = json.loads(urllib.request.urlopen(req, timeout=10).read())["token"]
fail += 0 if check("登录", bool(tok)) else 1

def ask(q):
    r = urllib.request.Request("http://127.0.0.1:8082/api/qa/ask",
        data=json.dumps({"query": q}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer " + tok}, method="POST")
    raw = urllib.request.urlopen(r, timeout=180).read().decode("utf-8")
    evs = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith("data:")]
    meta = next(e for e in evs if e.get("type") == "meta")
    text = "".join(e.get("text", "") for e in evs if e.get("type") == "delta")
    return meta, text

# 3. 正常问答：回答正确 + 无低分引用
meta, text = ask("生产服务器最低配置是什么？回滚时长要求多少？")
scores = [c["score"] for c in meta.get("citations", [])]
fail += 0 if check("问答不拒答", not meta.get("rejected"), meta.get("reason")) else 1
fail += 0 if check("引用全部 >= -5（低分剔除生效）", all(s >= -5.0 for s in scores), scores) else 1
fail += 0 if check("回答含关键内容", "4核" in text.replace(" ", "") and "15分钟" in text.replace(" ", ""), text[:80]) else 1
fail += 0 if check("回答带来源", "[来源:" in text) else 1
print("  回答:", text[:120])

# 4. 用户隔离：B 的问答不能泄露 A 的文档内容。
#    e2e_demo_b 自己的 p2_ui_doc.txt 含"回滚时长小于 30 分钟"（P2 旅程遗留数据）；
#    A 的服务器部署手册.md 是"15 分钟"。B 拒答（无相关文档）或回答只基于自己的 30 分钟均算隔离通过。
req2 = urllib.request.Request("http://127.0.0.1:8082/api/auth/login",
    data=json.dumps({"username": "e2e_demo_b", "password": "Passw0rd1"}).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
tokB = json.loads(urllib.request.urlopen(req2, timeout=10).read())["token"]
r = urllib.request.Request("http://127.0.0.1:8082/api/qa/ask",
    data=json.dumps({"query": "服务器部署手册中的回滚时长要求是多少"}, ensure_ascii=False).encode("utf-8"),
    headers={"Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer " + tokB}, method="POST")
rawB = urllib.request.urlopen(r, timeout=180).read().decode("utf-8")
evB = [json.loads(l[5:].strip()) for l in rawB.splitlines() if l.startswith("data:")]
metaB = next(e for e in evB if e.get("type") == "meta")
textB = "".join(e.get("text", "") for e in evB if e.get("type") == "delta")
fail += 0 if check("用户隔离：B 不泄露 A 的 15 分钟回滚时长",
                   metaB.get("rejected") is True or "30分钟" in textB.replace(" ", ""),
                   (textB[:80], metaB)) else 1

print("最终冒烟:", "PASS" if fail == 0 else f"{fail} FAILED")
