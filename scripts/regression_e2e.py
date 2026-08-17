"""全链路回归测试：Java 网关 + Python 服务 + 存储 + Zipkin。

用法：python scripts/regression_e2e.py [--skip-approval]
每步输出 PASS/FAIL，最后汇总；失败时退出码非 0。
"""
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag-python" / "src"))

GW = "http://127.0.0.1:8082"
PY = "http://127.0.0.1:8090"
SKIP_APPROVAL = "--skip-approval" in sys.argv

results = []


def step(name):
    def deco(fn):
        def wrapper(*a, **kw):
            t0 = time.time()
            try:
                detail = fn(*a, **kw) or ""
                results.append(("PASS", name, f"{time.time()-t0:.1f}s {detail}"))
                print(f"[PASS] {name} {detail}")
            except AssertionError as e:
                results.append(("FAIL", name, str(e)))
                print(f"[FAIL] {name}: {e}")
            except Exception as e:
                results.append(("FAIL", name, f"{type(e).__name__}: {e}"))
                print(f"[FAIL] {name}: {type(e).__name__}: {e}")
        return wrapper
    return deco


def db(sql, args=None):
    from db import pg_store
    return pg_store.query(sql, args) if sql.lower().startswith("select") \
        else pg_store.execute(sql, args)


# ---------- 1. 认证 ----------
@step("1.1 注册普通用户与 admin")
def t_register():
    ts = str(int(time.time()))
    for u, role in [(f"u{ts}", "user"), (f"adm{ts}", "admin")]:
        r = requests.post(f"{GW}/api/auth/register",
                          json={"username": u, "password": "p123456", "role": role},
                          timeout=10)
        assert r.status_code == 200, f"{u}: {r.status_code} {r.text}"
    return f"users: u{ts}/adm{ts}"


@step("1.3 无 token / 坏 token 拒绝")
def t_no_token():
    r1 = requests.post(f"{GW}/api/chat/ask", json={"query": "x"}, timeout=10)
    r2 = requests.post(f"{GW}/api/chat/ask", json={"query": "x"}, timeout=10,
                       headers={"Authorization": "Bearer bad.token.here"})
    assert r1.status_code in (401, 403), f"无 token 应 401/403，实际 {r1.status_code}"
    assert r2.status_code in (401, 403), f"坏 token 应 401/403，实际 {r2.status_code}"


# ---------- 2. 问答 ----------
@step("2.1 网关问答（JSON，含引用）")
def t_ask(token):
    r = requests.post(f"{GW}/api/chat/ask",
                      json={"query": "白皮书调研了多少家企业？"}, timeout=180,
                      headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d.get("answer"), "answer 为空"
    assert d.get("citations"), "缺少 citations"
    assert "1283" in d["answer"] or "1,283" in d["answer"], \
        f"答案未含事实 1283：{d['answer'][:80]}"
    return f"answer[{len(d['answer'])}字] cites={len(d['citations'])}"


@step("2.2 语义缓存命中（同题二次）")
def t_cache(token):
    q = {"query": "白皮书调研了多少家企业？"}
    h = {"Authorization": f"Bearer {token}"}
    requests.post(f"{GW}/api/chat/ask", json=q, timeout=180, headers=h)
    r = requests.post(f"{GW}/api/chat/ask", json=q, timeout=60, headers=h)
    d = r.json()
    assert d.get("cache_hit") is True, f"二次请求未命中缓存: {list(d.keys())}"


@step("2.3 拒答（越界问题）")
def t_refuse(token):
    r = requests.post(f"{GW}/api/chat/ask",
                      json={"query": "帮我写一首关于秋天的诗"}, timeout=180,
                      headers={"Authorization": f"Bearer {token}"})
    d = r.json()
    assert d.get("refused") is True or "未找到" in d.get("answer", ""), \
        f"越界题未拒答: {d.get('answer', '')[:60]}"


@step("2.4 SSE 流式问答")
def t_sse(token):
    r = requests.post(f"{GW}/api/chat/ask-stream",
                      json={"query": "白皮书由哪个机构发布？"}, timeout=300,
                      headers={"Authorization": f"Bearer {token}"}, stream=True)
    assert r.status_code == 200, r.status_code
    lines, done = [], False
    for line in r.iter_lines(decode_unicode=True):
        if line:
            lines.append(line)
            if "[DONE]" in line:
                done = True
                break
    assert done, f"未收到 [DONE]，共 {len(lines)} 行"
    assert any("data:" in l for l in lines), "无 data 事件"
    return f"{len(lines)} 事件"


# ---------- 3. 限流 ----------
@step("3.1 限流（20/min，25 并发触发 429）")
def t_rate_limit():
    ts = str(int(time.time()))
    u = f"rl{ts}"
    requests.post(f"{GW}/api/auth/register",
                  json={"username": u, "password": "p123456"}, timeout=10)
    tok = requests.post(f"{GW}/api/auth/login",
                        json={"username": u, "password": "p123456"},
                        timeout=10).json()["token"]
    # 同一问题并发：首个真实问答，其余大概率缓存命中，控成本
    def one(_):
        try:
            return requests.post(f"{GW}/api/chat/ask",
                                 json={"query": "白皮书调研了多少家企业？"},
                                 timeout=180,
                                 headers={"Authorization": f"Bearer {tok}"}).status_code
        except Exception:
            return 0
    with ThreadPoolExecutor(max_workers=25) as ex:
        codes = list(ex.map(one, range(25)))
    n429 = codes.count(429)
    assert n429 >= 3, f"429 数量不足：codes={codes}"
    return f"429 x{n429} / 25"


# ---------- 4. 反馈闭环 ----------
@step("4.1 反馈提交 → bad case → 归因")
def t_feedback():
    row = db("SELECT id FROM qa_log ORDER BY id DESC LIMIT 1")[0]
    r = requests.post(f"{PY}/api/feedback",
                      json={"qa_log_id": row["id"], "rating": -1,
                            "correction": "测试回归：答案不够完整"}, timeout=30)
    assert r.status_code == 200, r.text
    bc = requests.get(f"{PY}/api/feedback/bad-cases", timeout=30).json()
    assert bc, "bad-cases 为空"
    bc_id = bc[0]["id"]
    attr = requests.post(f"{PY}/api/feedback/bad-cases/{bc_id}/attribute",
                         timeout=600)
    assert attr.status_code == 200, attr.text[:200]
    d = attr.json()
    assert d.get("attribution"), f"归因无 attribution: {d}"
    return f"qa#{row['id']} → bc#{bc_id} → {d['attribution']}"


# ---------- 5. Prompt 管理 + HITL 审批 ----------
@step("5.1 Prompt 列表（经网关 admin 代理）")
def t_prompts(token):
    r = requests.get(f"{GW}/api/admin/proxy/api/admin/prompts", timeout=30,
                     headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text[:200]
    codes = [p["code"] for p in r.json()]
    assert "generate" in codes, codes
    return ",".join(codes)


@step("5.2 非 admin 访问 POST 代理 → 403")
def t_admin_guard(user_token):
    r = requests.post(f"{GW}/api/admin/proxy/api/admin/prompts/route/change",
                      json={"new_content": "x"}, timeout=30,
                      headers={"Authorization": f"Bearer {user_token}"})
    assert r.status_code == 403, f"应 403，实际 {r.status_code}"


@step("5.3 HITL 审批全流程（submit→interrupt→rejected）")
def t_approval(admin_token):
    cur = requests.get(f"{PY}/api/admin/prompts/no_answer", timeout=10)
    if cur.status_code != 200:
        code = "rewrite"  # 无 no_answer 则用 rewrite
        cur = requests.get(f"{PY}/api/admin/prompts/{code}", timeout=10)
    else:
        code = "no_answer"
    old = cur.json()["content"]
    new = old + "\n（回归测试临时追加说明，将被 rejected 回滚）"
    r = requests.post(f"{GW}/api/admin/proxy/api/admin/prompts/{code}/change",
                      json={"new_content": new}, timeout=1800,
                      headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    aid = d.get("approval_id") or d.get("id")
    assert aid, f"无 approval_id: {d}"
    assert d.get("status") in ("pending", "awaiting", "waiting_human"), \
        f"状态非待审批: {d}"
    # 审批拒绝 → 回滚
    r2 = requests.post(f"{GW}/api/admin/proxy/api/admin/approvals/{aid}/resume",
                       json={"decision": "rejected"}, timeout=120,
                       headers={"Authorization": f"Bearer {admin_token}"})
    assert r2.status_code == 200, r2.text[:300]
    after = requests.get(f"{PY}/api/admin/prompts/{code}", timeout=10).json()
    assert after["content"] == old, "rejected 后 prompt 未回滚"
    return f"{code}#{aid} rejected+回滚验证"


# ---------- 6. 诊断 ----------
@step("6.1 诊断 Agent 触发 + 报告")
def t_diagnosis():
    r = requests.post(f"{PY}/api/diagnosis/trigger", timeout=600)
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d.get("report") or d.get("summary") or d.get("content"), f"报告为空: {list(d.keys())}"
    latest = requests.get(f"{PY}/api/diagnosis/latest", timeout=30).json()
    assert latest, "latest 为空"
    return f"报告 {len(json.dumps(d, ensure_ascii=False))} 字符"


# ---------- 7. 审计 + Zipkin ----------
@step("7.1 网关审计日志落库")
def t_audit():
    rows = db("SELECT count(*) n FROM kb_audit_log")
    assert rows[0]["n"] >= 3, f"审计记录不足: {rows[0]['n']}"
    return f"{rows[0]['n']} 条"


@step("7.2 Zipkin 可查 trace")
def t_zipkin():
    url = "http://127.0.0.1:9411/api/v2/traces?limit=5"
    req = urllib.request.urlopen(url, timeout=10)
    data = json.loads(req.read())
    assert data, "Zipkin 无 trace"
    return f"{len(data)} traces"


def main():
    t_register()

    # 登录步骤需要取回 token 供后续使用：内联执行
    ts = str(int(time.time()))
    t0 = time.time()
    try:
        requests.post(f"{GW}/api/auth/register",
                      json={"username": f"l{ts}", "password": "p123456"}, timeout=10)
        r = requests.post(f"{GW}/api/auth/login",
                          json={"username": f"l{ts}", "password": "p123456"}, timeout=10)
        assert r.status_code == 200, r.text
        tok = r.json()["token"]
        assert tok.count(".") == 2, "token 不是 JWT 三段式"
        bad = requests.post(f"{GW}/api/auth/login",
                            json={"username": f"l{ts}", "password": "wrong"}, timeout=10)
        assert bad.status_code == 401, f"错误密码应 401，实际 {bad.status_code}"
        results.append(("PASS", "1.2 登录拿 JWT + 错误密码 401",
                        f"{time.time()-t0:.1f}s"))
        print("[PASS] 1.2 登录拿 JWT")
    except Exception as e:
        tok = None
        results.append(("FAIL", "1.2 登录拿 JWT", str(e)))
        print(f"[FAIL] 1.2: {e}")

    t_no_token()
    if not tok:
        print("无 token，跳过后续需要认证的步骤")
    else:
        ts = str(int(time.time()))
        requests.post(f"{GW}/api/auth/register",
                      json={"username": f"adm{ts}", "password": "p123456",
                            "role": "admin"}, timeout=10)
        admin_tok = requests.post(
            f"{GW}/api/auth/login",
            json={"username": f"adm{ts}", "password": "p123456"},
            timeout=10).json()["token"]

        t_ask(tok)
        t_cache(tok)
        t_refuse(tok)
        t_sse(tok)
        t_rate_limit()
        t_feedback()
        t_prompts(admin_tok)
        t_admin_guard(tok)
        if not SKIP_APPROVAL:
            t_approval(admin_tok)
        t_diagnosis()
        t_audit()
    t_zipkin()

    print("\n" + "=" * 60)
    np = sum(1 for s, *_ in results if s == "PASS")
    print(f"回归结果：{np} PASS / {len(results) - np} FAIL")
    for s, n, d in results:
        print(f"  [{s}] {n} | {d}")
    sys.exit(0 if np == len(results) else 1)


if __name__ == "__main__":
    main()
