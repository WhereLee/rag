"""检索层并发压测（进程内直调 retrieve，服务器真实环境跑）。

用法（服务器 4 核形态）：
  cd rag-python/src && .venv/bin/python ../../scripts/bench_retrieval.py \
      --concurrency 4 --duration 30 --cascade off
  .venv/bin/python ../../scripts/bench_retrieval.py \
      --concurrency 4 --duration 30 --cascade on

指标：QPS / p50 p95 p99 / 降级率（reranked=False 且非级联跳过 = rerank 排队超时降级）
      / 级联跳过率（rerank_skipped=True 占比）
"""
import argparse
import json
import random
import threading
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rag-python" / "src"))

from retrieval.retriever import retrieve  # noqa: E402

QUERIES_FILE = Path(__file__).resolve().parents[1] / "rag-python" / "eval" / "questions.json"


def load_queries() -> list[str]:
    with open(QUERIES_FILE, encoding="utf-8") as f:
        data = json.load(f)
    qs = [item["question"] for item in data]
    # 扩展变体：真实查询不会全是问题句式，加入短问/关键词式
    qs += [q[:8] for q in qs[:8]]
    return qs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--cascade", choices=["on", "off"], default="off")
    args = ap.parse_args()

    queries = load_queries()
    stop = threading.Event()
    lat: list[float] = []
    lock = threading.Lock()
    stats = {"total": 0, "downgraded": 0, "skipped": 0, "empty": 0}

    def worker():
        while not stop.is_set():
            q = random.choice(queries)
            t0 = time.perf_counter()
            chunks = retrieve(None, q, top_k=5, cascade=(args.cascade == "on"))
            dt = (time.perf_counter() - t0) * 1000
            with lock:
                lat.append(dt)
                stats["total"] += 1
                if not chunks:
                    stats["empty"] += 1
                    continue
                c0 = chunks[0]
                if c0.rerank_skipped:
                    stats["skipped"] += 1
                elif not c0.reranked:
                    stats["downgraded"] += 1

    workers = [threading.Thread(target=worker, daemon=True) for _ in range(args.concurrency)]
    print(f"== bench start: concurrency={args.concurrency} duration={args.duration}s "
          f"cascade={args.cascade} queries={len(queries)} ==", flush=True)
    t_start = time.perf_counter()
    for w in workers:
        w.start()
    time.sleep(args.duration)
    stop.set()
    for w in workers:
        w.join(timeout=10)

    elapsed = time.perf_counter() - t_start
    n = stats["total"]
    qps = n / elapsed
    lat.sort()
    def pct(p):
        return round(lat[int(len(lat) * p)] if lat else 0, 1)
    print("== bench result ==")
    print(f"requests={n} elapsed={elapsed:.1f}s QPS={qps:.2f}")
    print(f"latency ms: p50={pct(0.5)} p95={pct(0.95)} p99={pct(0.99)} max={round(lat[-1],1) if lat else 0}")
    print(f"downgraded={stats['downgraded']} ({stats['downgraded']/max(n,1)*100:.2f}%)  "
          f"cascade_skipped={stats['skipped']} ({stats['skipped']/max(n,1)*100:.2f}%)  "
          f"empty={stats['empty']}")


if __name__ == "__main__":
    main()
