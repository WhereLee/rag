# -*- coding: utf-8 -*-
"""经 Vite(3000) 的浏览器同路径冒烟回归（第一轮修复配套）。

背景：第一轮安全收口曾因 vite.config.js 的 /api/admin 仍指向 8090，
导致浏览器链路（Vite→网关→Python）404，而直连 curl 验证全绿——配置漂移盲区。
本脚本固定走 Vite 端口，任何 proxy 配置漂移都会在此暴露。

用法：python scripts/smoke_via_vite.py   （需 PG/Java网关/Python/Vite/worker 均运行）
"""
import json
import random
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:3000"  # 固定走 Vite，模拟浏览器同路径
TIMEOUT = 15


def req(method, path, token=None, body=None, form=None):
    headers = {}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if form is not None:
        boundary = "----smoke" + str(random.randint(100000, 999999))
        parts = []
        for name, filename, content in form:
            if filename is None:
                # 纯字段：无 filename，作为表单参数（Spring @RequestParam 匹配）
                head = (f'--{boundary}\r\n'
                        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                        + content + "\r\n").encode("utf-8")
            else:
                head = (f'--{boundary}\r\n'
                        f'Content-Disposition: form-data; name="{name}"; '
                        f'filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n'
                        + content + "\r\n").encode("utf-8")
            parts.append(head)
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        data = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=TIMEOUT) as resp:
            body_text = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body_text)
            except json.JSONDecodeError:
                return resp.status, body_text
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    u = f"vite_{random.randint(10000, 99999)}"
    ok = []
    # 1 注册
    code, _ = req("POST", "/api/auth/register", body={"username": u, "password": "pass1234"})
    assert code == 200, f"register: {code}"
    ok.append("register")
    # 2 登录
    code, login = req("POST", "/api/auth/login", body={"username": u, "password": "pass1234"})
    assert code == 200 and login.get("token"), f"login: {code} {login}"
    token = login["token"]
    ok.append("login")
    # 3 文档列表（配置漂移会在此 404）
    code, docs = req("GET", "/api/admin/proxy/api/ingest/documents", token=token)
    assert code == 200 and isinstance(docs, list), f"documents: {code} {docs}"
    ok.append("documents")
    # 4 上传（应 202 异步任务）
    content = "Vite 回归冒烟：异步任务与网关代理验证。包含租约、退避、死信与重试设计。"
    code, up = req("POST", "/api/admin/proxy/api/ingest/upload", token=token,
                   form=[("file", "smoke_lib.txt", content),
                         ("replace", None, "true")])
    assert code == 202 and up.get("job_id"), f"upload: {code} {up}"
    job_id = up["job_id"]
    ok.append(f"upload:{job_id}")
    # 5 轮询任务至终态
    status = None
    for _ in range(30):
        code, job = req("GET", f"/api/admin/proxy/api/ingest/jobs/{job_id}", token=token)
        assert code == 200, f"job: {code} {job}"
        status = job.get("status")
        if status in ("done", "dead", "failed"):
            break
        time.sleep(2)
    assert status == "done", f"job not done: {status} {job}"
    ok.append("job_done")
    # 6 问答（同步路径，验证检索+生成+trace_id）
    code, ans = req("POST", "/api/chat/ask", token=token,
                    body={"query": "这份文档介绍了什么？"})
    assert code == 200 and ans.get("answer") and ans.get("trace_id"), f"ask: {code} {ans}"
    ok.append(f"ask(trace={ans['trace_id'][:8]})")
    print("VITE-SMOKE PASSED:", " -> ".join(ok))


if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except (AssertionError, Exception) as e:
        print("VITE-SMOKE FAILED:", repr(e))
        sys.exit(1)