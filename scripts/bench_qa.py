#!/usr/bin/env python3
"""逐环节压测③：问答全链路（经网关 /api/qa/ask，SSE 流式）
MiMo 外部配额 100 RPM 受限——小并发 + 少量请求，测阶段分解不测并发极限
输出：每请求 TTFT / 总时长 / 检索阶段 / LLM 生成阶段 / 引用数
"""
import json
import sys
import time
import urllib.request

GATEWAY = "http://127.0.0.1:8082"
USERNAME = "seeduser"
PASSWORD = "SeedUser123"

QUESTIONS = [
    "量子计算目前在哪些领域落地较快？",
    "什么是 RAG？",
    "FastAPI 是什么？",
    "文档解析支持哪些文件格式？",
]


def login():
    req = urllib.request.Request(
        f"{GATEWAY}/api/auth/login",
        data=json.dumps({"username": USERNAME, "password": PASSWORD}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as rp:
        return json.loads(rp.read())["token"]


def ask(token, query):
    """POST /api/qa/ask，解析 SSE 流，返回阶段耗时与元信息"""
    body = json.dumps({"query": query, "session_id": ""}).encode()
    req = urllib.request.Request(
        f"{GATEWAY}/api/qa/ask", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    t_start = time.perf_counter()
    t_meta = t_first_delta = t_done = None
    thinking_chars = delta_chars = 0
    citations = 0
    cache_ref = rejected = None
    context_tokens = None
    with urllib.request.urlopen(req, timeout=180) as rp:
        for raw in rp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            if ev.get("type") == "meta":
                t_meta = time.perf_counter()
                context_tokens = ev.get("context_tokens")
                cache_ref = ev.get("cache_ref")
                rejected = ev.get("rejected")
            elif ev.get("type") == "thinking":
                thinking_chars += len(ev.get("text", ""))
            elif ev.get("type") == "delta":
                if t_first_delta is None:
                    t_first_delta = time.perf_counter()
                delta_chars += len(ev.get("text", ""))
            elif ev.get("type") == "citations":
                citations = len(ev.get("citations", []) or [])
            elif ev.get("type") == "done":
                t_done = time.perf_counter()
    total = (t_done - t_start) * 1000 if t_done else None
    ttft = (t_first_delta - t_start) * 1000 if t_first_delta else None
    retrieval = (t_meta - t_start) * 1000 if t_meta else None
    gen = (t_done - t_meta) * 1000 if t_meta and t_done else None
    return {
        "query": query[:24], "total_ms": round(total, 1) if total else None,
        "ttft_ms": round(ttft, 1) if ttft else None,
        "retrieval_ms": round(retrieval, 1) if retrieval else None,
        "llm_gen_ms": round(gen, 1) if gen else None,
        "thinking_chars": thinking_chars, "delta_chars": delta_chars,
        "citations": citations, "cache_ref": cache_ref, "rejected": rejected,
        "context_tokens": context_tokens,
    }


def main():
    print("== 问答链路（MiMo 100RPM 受限，小并发 2 × 每问题 2 次） ==")
    token = login()
    print(f"token_len={len(token)}")
    results = []
    for q in QUESTIONS:
        for _ in range(2):
            try:
                r = ask(token, q)
                results.append(r)
                print(json.dumps(r, ensure_ascii=False))
            except Exception as e:
                print(f"{q[:16]}: FAILED {str(e)[:120]}")
            time.sleep(2)  # 间隔 2s，避免瞬时打爆配额
    if results:
        totals = [r["total_ms"] for r in results if r["total_ms"]]
        ttf = [r["ttft_ms"] for r in results if r["ttft_ms"]]
        ret = [r["retrieval_ms"] for r in results if r["retrieval_ms"]]
        gen = [r["llm_gen_ms"] for r in results if r["llm_gen_ms"]]
        avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
        print(json.dumps({"汇总": {
            "total_avg_ms": avg(totals), "ttft_avg_ms": avg(ttf),
            "retrieval_avg_ms": avg(ret), "llm_gen_avg_ms": avg(gen),
            "cache_hits": sum(1 for r in results if r["cache_ref"])}}, ensure_ascii=False))
    print("== DONE ==")


if __name__ == "__main__":
    main()
