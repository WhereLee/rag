# -*- coding: utf-8 -*-
"""黄金集检索评估：四组对比（纯向量 / 纯 BM25 / RRF / RRF+rerank）输出 Recall@5。
用法: python rag-python/eval/run_retrieval_eval.py --user-id 123 [--top-k 5] [--out logs/xxx.json]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retrieval.retriever import _bm25_search, _vector_search, retrieve

GOLDEN = Path(__file__).resolve().parent / "golden_retrieval.json"


def recall_at_5(hits_files, expect_files):
    return any(e in hits_files for e in expect_files)


def run(user_id, top_k=5, out_path=None):
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    questions = data["questions"]
    stats = {"vector": 0, "bm25": 0, "rrf": 0, "rrf_rerank": 0}
    per_query = []

    for q in questions:
        expect = q["expect_files"]
        vec = _vector_search(user_id, q["query"], top_n=top_k)
        bm = _bm25_search(user_id, q["query"], top_n=top_k)
        rrf = retrieve(user_id, q["query"], top_k=top_k, use_rerank=False)
        full = retrieve(user_id, q["query"], top_k=top_k, use_rerank=True)

        def files(items):
            return [i["filename"] if isinstance(i, dict) else i.filename for i in items]

        hits = {
            "vector": recall_at_5(files(vec), expect),
            "bm25": recall_at_5(files(bm), expect),
            "rrf": recall_at_5(files(rrf), expect),
            "rrf_rerank": recall_at_5(files(full), expect),
        }
        for k, v in hits.items():
            stats[k] += int(v)
        per_query.append({"id": q["id"], "query": q["query"], "category": q.get("category"),
                          "expect": expect, **hits})

    n = len(questions)
    print(f"\n黄金集 Recall@{top_k}（{n} 条）")
    print(f"{'方案':<14}{'命中':>5}{'Recall@5':>10}")
    for k in ("vector", "bm25", "rrf", "rrf_rerank"):
        print(f"{k:<14}{stats[k]:>5}{stats[k] / n:>10.2%}")

    cat_stats = {}
    for q in per_query:
        c = q.get("category", "-")
        cat_stats.setdefault(c, {"n": 0, "rrf_rerank": 0})
        cat_stats[c]["n"] += 1
        cat_stats[c]["rrf_rerank"] += int(q["rrf_rerank"])
    print("\n分类型（RRF+rerank）")
    for c, s in cat_stats.items():
        print(f"{c:<10}{s['rrf_rerank']}/{s['n']} = {s['rrf_rerank'] / s['n']:.0%}")

    result = {"user_id": user_id, "top_k": top_k, "n": n, "stats": stats,
              "per_query": per_query}
    if out_path:
        Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        print(f"\n结果已存: {out_path}")
    return stats, per_query


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", type=int, required=True)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    run(args.user_id, args.top_k, args.out or None)
