#!/usr/bin/env python3
"""逐环节压测①：网关层 + 数据库 + 缓存
用法: bench_infra.py [--gateway] [--db] [--cache] [--concurrency N] [--duration S]
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

GATEWAY = "http://127.0.0.1:8082"
USERNAME = "seeduser"
PASSWORD = "SeedUser123"
PG_HOST = "127.0.0.1"
PG_USER = "rag_app"
PG_DB = "rag_kb"


def _pg_password():
    with open("/opt/rag/.env", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "://" in line and "rag_app" in line:
                return line.split("rag_app:")[1].split("@")[0]
    return ""


def run_pg(sql: str, db: str = PG_DB) -> list:
    env = dict(os.environ, PGPASSWORD=_pg_password())
    t0 = time.perf_counter()
    r = subprocess.run(
        ["psql", "-h", PG_HOST, "-U", PG_USER, "-d", db, "-t", "-A", "-c", sql],
        capture_output=True, text=True, env=env, timeout=30)
    elapsed = time.perf_counter() - t0
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:200])
    return elapsed, [ln for ln in r.stdout.splitlines() if ln.strip()]


def http_call(url, token=None, method="GET", body=None, timeout=30):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    else:
        data = None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return time.perf_counter() - t0, resp.status
    except Exception as e:
        return time.perf_counter() - t0, getattr(e, "code", 0)


def bench(label, fn, concurrency, duration):
    stop = time.time() + duration
    latencies, errors, lock = [], 0, threading.Lock()

    def worker():
        nonlocal errors
        while time.time() < stop:
            lat, code = fn()
            with lock:
                if 200 <= code < 300:
                    latencies.append(lat)
                else:
                    errors += 1

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = len(latencies) + errors
    if not latencies:
        return {"label": label, "total": total, "errors": errors, "error_rate": 1.0}
    latencies.sort()
    p = lambda x: latencies[min(len(latencies) - 1, int(len(latencies) * x))]
    return {
        "label": label, "total": total, "success": len(latencies), "errors": errors,
        "error_rate": round(errors / total, 4), "qps": round(len(latencies) / duration, 2),
        "p50_ms": round(p(0.5) * 1000, 1), "p95_ms": round(p(0.95) * 1000, 1),
        "p99_ms": round(p(0.99) * 1000, 1), "max_ms": round(latencies[-1] * 1000, 1),
    }


def bench_gateway(args):
    print(f"\n=== 网关层（并发 {args.concurrency}，时长 {args.duration}s） ===")
    # 登录接口受 IP 限流保护（3次/分钟，防爆破设计）——不压测，只测单发延迟
    try:
        lat, code = http_call(f"{GATEWAY}/api/auth/login", method="POST",
                              body={"username": USERNAME, "password": PASSWORD})
        print(json.dumps({"label": "登录(单发)", "latency_ms": round(lat * 1000, 1),
                          "http": code, "note": "限流保护接口，不做并发压测"}, ensure_ascii=False))
    except Exception as e:
        print(f"登录单发: {str(e)[:100]}")
    token = ""
    try:
        req = urllib.request.Request(f"{GATEWAY}/api/auth/login",
                                     data=json.dumps({"username": USERNAME, "password": PASSWORD}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as rp:
            token = json.loads(rp.read())["token"]
    except Exception:
        print("登录失败（可能限流窗口未恢复）")
    print(f"token_len={len(token)}")
    if not token:
        print("无 token，跳过网关压测")
        return
    r = bench("文件列表(GET)", lambda: http_call(f"{GATEWAY}/api/files?page=1&page_size=10", token=token),
              args.concurrency, args.duration)
    print(json.dumps(r, ensure_ascii=False))
    r = bench("问答代理鉴权(GET /api/qa/sessions)", lambda: http_call(f"{GATEWAY}/api/qa/sessions", token=token),
              args.concurrency, args.duration)
    print(json.dumps(r, ensure_ascii=False))


def bench_db(args):
    print(f"\n=== 数据库层（并发 {args.concurrency}，时长 {args.duration}s） ===")
    cases = [
        ("简单查询 count(*)", "SELECT count(*) FROM rag_chunk"),
        ("主键点查 by id", "SELECT id FROM rag_chunk WHERE id=1"),
        ("向量 HNSW top5", "SELECT id FROM rag_chunk ORDER BY embedding <-> (SELECT embedding FROM rag_chunk WHERE id=1) LIMIT 5"),
        ("失败块联查 issues", "SELECT count(*) FROM issue_items WHERE file_id=4"),
    ]
    for label, sql in cases:
        try:
            r = bench(label, lambda s=sql: (run_pg(s)[0], 200), args.concurrency, args.duration)
            print(json.dumps(r, ensure_ascii=False))
        except Exception as e:
            print(f"{label}: FAILED {str(e)[:120]}")


def bench_cache(args):
    print(f"\n=== 缓存层 Redis（并发 {args.concurrency}，时长 {args.duration}s） ===")
    try:
        import redis
        rc = redis.Redis(host="127.0.0.1", port=6379, db=0)
        rc.set("bench_probe", "1", ex=3600)

        def timed_get():
            t0 = time.perf_counter()
            rc.get("bench_probe")
            return time.perf_counter() - t0

        def timed_set():
            t0 = time.perf_counter()
            rc.set("bench_probe", "1", ex=3600)
            return time.perf_counter() - t0

        r = bench("Redis GET", lambda: (timed_get(), 200), args.concurrency, args.duration)
        print(json.dumps(r, ensure_ascii=False))
        r = bench("Redis SET", lambda: (timed_set(), 200), args.concurrency, args.duration)
        print(json.dumps(r, ensure_ascii=False))
    except Exception as e:
        print(f"Redis 压测失败: {str(e)[:200]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", action="store_true")
    ap.add_argument("--db", action="store_true")
    ap.add_argument("--cache", action="store_true")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--duration", type=int, default=15)
    args = ap.parse_args()
    if args.gateway:
        bench_gateway(args)
    if args.db:
        bench_db(args)
    if args.cache:
        bench_cache(args)
    print("\n== ALL DONE ==")


if __name__ == "__main__":
    main()
