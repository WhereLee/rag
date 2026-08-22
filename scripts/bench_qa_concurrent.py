#!/usr/bin/env python3
"""DeepSeek 高并发问答压测：并发度 4/8/16 三档，每档 45s
输出：QPS、成功率、TTFT p50/p95、总时长 p50/p95、错误分布
"""
import json
import sys
import threading
import time
import urllib.request

GATEWAY = "http://127.0.0.1:8082"
USERNAME = "seeduser"
PASSWORD = "SeedUser123"

QUERIES = [
    "文档智能解析技术规范的核心技术要求有哪些？",
    "系统的架构是怎么设计的？",
    "历史档案数字化试点通知的内容是什么？",
    "检索流程中 rerank 的作用是什么？",
    "系统支持哪些文件格式？",
    "RAG 检索的召回与重排策略是什么？",
]

def login():
    req = urllib.request.Request(
        f"{GATEWAY}/api/auth/login",
        data=json.dumps({"username": USERNAME, "password": PASSWORD}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as rp:
        return json.loads(rp.read())["token"]

def ask_sses(token, query, timeout=90):
    """发一次问答请求，解析 SSE：返回 (ttft_s, total_s, rejected)"""
    body = json.dumps({"query": query, "session_id": ""}).encode()
    req = urllib.request.Request(
        f"{GATEWAY}/api/qa/ask", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    t0 = time.perf_counter()
    ttft = None
    rejected = None
    with urllib.request.urlopen(req, timeout=timeout) as rp:
        for raw in rp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:])
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "meta":
                ttft = time.perf_counter() - t0
                rejected = ev.get("rejected", False)
            elif ev.get("type") == "done":
                return ttft, time.perf_counter() - t0, rejected
    return ttft, time.perf_counter() - t0, rejected

def run(concurrency, duration):
    token = login()
    stop = time.time() + duration
    lock = threading.Lock()
    rows = {"total": 0, "ok": 0, "err": 0, "rejected": 0,
            "ttft": [], "total_t": []}
    qidx = 0

    def worker():
        nonlocal qidx
        while time.time() < stop:
            with lock:
                q = QUERIES[qidx % len(QUERIES)]
                qidx += 1
            try:
                ttft, total, rejected = ask_sses(token, q)
                with lock:
                    rows["total"] += 1
                    if rejected:
                        rows["rejected"] += 1
                    if ttft is not None:
                        rows["ttft"].append(ttft)
                        rows["total_t"].append(total)
                        rows["ok"] += 1
                    else:
                        rows["err"] += 1
            except Exception:
                with lock:
                    rows["total"] += 1
                    rows["err"] += 1

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    t0 = time.perf_counter()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    elapsed = time.perf_counter() - t0

    def pct(arr, p):
        if not arr:
            return 0.0
        s = sorted(arr)
        return s[min(len(s) - 1, int(len(s) * p))]

    print(f"== 并发 {concurrency} / {duration}s ==")
    print(f"总请求 {rows['total']} | 完成 {rows['ok']} | 失败 {rows['err']} | 拒答 {rows['rejected']}")
    print(f"有效 QPS: {rows['ok'] / elapsed:.2f} (窗口 {elapsed:.0f}s)")
    print(f"TTFT   p50={pct(rows['ttft'], 0.5):.2f}s p95={pct(rows['ttft'], 0.95):.2f}s")
    print(f"总时长 p50={pct(rows['total_t'], 0.5):.2f}s p95={pct(rows['total_t'], 0.95):.2f}s")

if __name__ == "__main__":
    for c in (4, 8, 16):
        run(c, 45)
