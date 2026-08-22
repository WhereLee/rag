#!/usr/bin/env python3
"""日常混合流量模拟：登录低频 + 文件浏览 + 偶尔上传(即传即删) + 问答为主
- 登录：每 60s 1 次（真实频率，避开 IP 限流）
- 上传：每 60s 1 次小文件，上传后立即删除（测链路不留语料）
- 浏览/问答检索：其余并发按权重随机（浏览 60% / 问答检索 40%，问答=检索层直调，
  LLM 段受 100RPM 配额单独用 bench_qa 数据补充）
输出：各类型 QPS/延迟分位/成功率 + 总体
"""
import json
import os
import random
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0, "/opt/rag/rag-python/src")

GATEWAY = "http://127.0.0.1:8082"
USERNAME = "seeduser"
PASSWORD = "SeedUser123"
QUERIES = [  # 黄金集抽样（有真实答案的直问）
    "量子计算目前在哪些领域落地较快？",
    "FastAPI 是什么？",
    "文档解析支持哪些文件格式？",
    "什么是智能文档管理？",
    "RAG 检索增强生成的核心思想是什么？",
    "企业文档管理系统有哪些功能？",
]


def login():
    req = urllib.request.Request(
        f"{GATEWAY}/api/auth/login",
        data=json.dumps({"username": USERNAME, "password": PASSWORD}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as rp:
        return json.loads(rp.read())["token"]


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
        with urllib.request.urlopen(req, timeout=timeout) as rp:
            rp.read()
        return time.perf_counter() - t0, rp.status
    except Exception as e:
        return time.perf_counter() - t0, getattr(e, "code", 0)


def retrieve_bench(query):
    """检索层直调（模拟问答的检索阶段，cascade 生产配置）"""
    from retrieval.retriever import retrieve
    t0 = time.perf_counter()
    try:
        chunks = retrieve(None, query, top_k=5, cascade=True)
        return time.perf_counter() - t0, 200
    except Exception:
        return time.perf_counter() - t0, 500


def upload_and_delete(token, path="/tmp/mixed_probe.txt"):
    """上传小文件（multipart）→ 删除"""
    with open(path, "w", encoding="utf-8") as f:
        f.write("混合流量探针文件\n" * 20)
    boundary = "----MixedBench" + str(random.randint(100000, 999999))
    body = []
    with open(path, "rb") as f:
        data = f.read()
    body.append(f"--{boundary}\r\n".encode())
    body.append(b'Content-Disposition: form-data; name="file"; filename="mixed_probe.txt"\r\n')
    body.append(b"Content-Type: text/plain\r\n\r\n")
    body.append(data)
    body.append(f"\r\n--{boundary}\r\n".encode())
    body.append(b'Content-Disposition: form-data; name="dir_id"\r\n\r\n\r\n')
    body.append(f"--{boundary}--\r\n".encode())
    payload = b"".join(body)
    req = urllib.request.Request(
        f"{GATEWAY}/api/files/upload", data=payload, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as rp:
            resp = json.loads(rp.read())
        file_id = resp.get("file_id") or resp.get("id")
        lat = time.perf_counter() - t0
        if file_id:
            http_call(f"{GATEWAY}/api/files/{file_id}", token=token, method="DELETE")
        return lat, 200
    except Exception as e:
        return time.perf_counter() - t0, getattr(e, "code", 0)


def run(duration, concurrency):
    token = login()
    stop = time.time() + duration
    lock = threading.Lock()
    stats = {}  # type -> list[lat], errors
    login_done = {"at": 0}
    upload_done = {"at": 0}

    def record(typ, lat, code):
        with lock:
            s = stats.setdefault(typ, {"lats": [], "err": 0})
            if 200 <= code < 300:
                s["lats"].append(lat)
            else:
                s["err"] += 1

    def worker():
        while time.time() < stop:
            if time.time() - login_done["at"] >= 60:
                login_done["at"] = time.time()
                lat, code = http_call(f"{GATEWAY}/api/auth/login", method="POST",
                                      body={"username": USERNAME, "password": PASSWORD})
                record("登录", lat, code)
            if time.time() - upload_done["at"] >= 60:
                upload_done["at"] = time.time()
                lat, code = upload_and_delete(token)
                record("上传+删除", lat, code)
            if random.random() < 0.6:
                lat, code = http_call(f"{GATEWAY}/api/files?page=1&page_size=10", token=token)
                record("文件浏览", lat, code)
            else:
                lat, code = retrieve_bench(random.choice(QUERIES))
                record("问答检索", lat, code)

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(json.dumps({"时长_s": duration, "并发": concurrency}, ensure_ascii=False))
    total_req = total_err = 0
    for typ, s in sorted(stats.items()):
        n = len(s["lats"])
        total_req += n + s["err"]
        total_err += s["err"]
        if not s["lats"]:
            print(json.dumps({"类型": typ, "请求": n + s["err"], "错误": s["err"],
                              "错误率": 1.0}, ensure_ascii=False))
            continue
        lats = sorted(s["lats"])
        p = lambda x: lats[min(len(lats) - 1, int(len(lats) * x))]
        print(json.dumps({
            "类型": typ, "请求": n + s["err"], "错误": s["err"],
            "错误率": round(s["err"] / (n + s["err"]), 4),
            "qps": round(n / duration, 2),
            "p50_ms": round(p(0.5) * 1000, 1), "p95_ms": round(p(0.95) * 1000, 1),
            "p99_ms": round(p(0.99) * 1000, 1)}, ensure_ascii=False))
    print(json.dumps({"总体": {"请求": total_req, "错误": total_err,
                               "错误率": round(total_err / total_req, 4) if total_req else None,
                               "qps": round(total_req / duration, 2)}}, ensure_ascii=False))


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--duration", type=int, default=120)
    args = ap.parse_args()
    print(f"== 日常混合流量模拟（{args.duration}s，并发 {args.concurrency}） ==")
    run(args.duration, args.concurrency)
    print("== DONE ==")


if __name__ == "__main__":
    main()
