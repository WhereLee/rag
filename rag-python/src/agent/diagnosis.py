"""
诊断 Agent：读监控数据 → LLM 分析 → 生成报告。

冷启动解法：数据源包含 eval_run（评估流量），不依赖真实用户。
指标（架构文档 §2.2）：
- 检索质量：命中率、低置信比例、top_score 分布
- 链路性能：各阶段耗时 P50/P95、总时延
- Token 消耗：总量、按路由档位
- 异常：拒答率、bad case 归因分布
"""
import json
import logging
from typing import Dict

from db import pg_store
from llm.mimo_client import get_client, LLMError
from llm.prompt_loader import fill

logger = logging.getLogger("rag.diagnosis")


def collect_metrics() -> Dict:
    m: Dict = {}
    # 问答总量与档位分布
    rows = pg_store.query(
        """SELECT route, count(*) AS n, avg(total_ms)::int AS avg_ms,
                  sum(token_in + token_out)::bigint AS tokens
           FROM qa_log GROUP BY route""")
    m["qa_by_route"] = [dict(r) for r in rows]
    total = pg_store.query_one("SELECT count(*) AS n FROM qa_log")
    m["qa_total"] = total["n"]
    refused = pg_store.query_one(
        """SELECT count(*) AS n FROM qa_log
           WHERE answer LIKE '%未找到%' OR answer LIKE '%无法回答%'""")
    m["refuse_rate"] = round(refused["n"] / max(m["qa_total"], 1), 4)
    # token 总消耗
    tok = pg_store.query_one(
        "SELECT sum(token_in)::bigint AS tin, sum(token_out)::bigint AS tout FROM qa_log")
    m["tokens"] = {"in": tok["tin"] or 0, "out": tok["tout"] or 0}
    # 检索质量
    low = pg_store.query_one(
        "SELECT count(*) AS n FROM retrieval_log WHERE low_confidence")
    rl = pg_store.query_one("SELECT count(*) AS n FROM retrieval_log")
    m["retrieval"] = {"total": rl["n"],
                      "low_confidence_rate": round(low["n"] / max(rl["n"], 1), 4)}
    empty = pg_store.query_one("SELECT count(*) AS n FROM retrieval_log WHERE hit_count=0")
    m["retrieval"]["empty_rate"] = round(empty["n"] / max(rl["n"], 1), 4)
    # 阶段耗时（P50/P95，近 200 条）
    stages = pg_store.query(
        """SELECT stage_ms FROM retrieval_log
           WHERE stage_ms IS NOT NULL ORDER BY id DESC LIMIT 200""")
    if stages:
        import statistics
        flat: Dict[str, list] = {}
        for r in stages:
            for k, v in (r["stage_ms"] or {}).items():
                flat.setdefault(k, []).append(v)
        m["stage_latency"] = {
            k: {"p50": int(statistics.median(v)),
                "p95": int(sorted(v)[int(len(v) * 0.95)] if len(v) > 1 else v[0])}
            for k, v in flat.items()}
    # 反馈与 bad case
    fb = pg_store.query_one(
        """SELECT count(*) FILTER (WHERE rating=-1) AS neg,
                  count(*) FILTER (WHERE rating=1) AS pos FROM feedback""")
    m["feedback"] = {"positive": fb["pos"] or 0, "negative": fb["neg"] or 0}
    bc = pg_store.query(
        """SELECT attribution, count(*) AS n FROM bad_case
           GROUP BY attribution""")
    m["bad_case_attribution"] = [dict(r) for r in bc]
    # 评估运行（冷启动数据源）
    runs = pg_store.query(
        "SELECT id, name, metrics, created_at FROM eval_run ORDER BY id DESC LIMIT 5")
    m["recent_eval_runs"] = [
        {"id": r["id"], "name": r["name"], "metrics": r["metrics"],
         "created_at": str(r["created_at"])} for r in runs]
    # 缓存命中
    cache = pg_store.query_one(
        "SELECT count(*) FILTER (WHERE cache_hit) AS n FROM qa_log")
    m["cache_hit_rate"] = round((cache["n"] or 0) / max(m["qa_total"], 1), 4)
    return m


def generate_report() -> Dict:
    metrics = collect_metrics()
    try:
        obj = get_client().chat_json(
            [{"role": "user", "content": fill(
                "diagnosis",
                metrics=json.dumps(metrics, ensure_ascii=False, default=str))}],
            thinking=True, max_tokens=4096)
        report = {
            "summary": obj.get("summary", ""),
            "anomalies": obj.get("anomalies", []),
            "suggestions": obj.get("suggestions", []),
        }
    except LLMError as e:
        logger.error("diagnosis llm failed: %s", e)
        report = {"summary": f"LLM 分析失败（指标已采集）: {e}",
                  "anomalies": [], "suggestions": []}
    rid = pg_store.query_one(
        """INSERT INTO diagnosis_report (summary, metrics, anomalies, suggestions)
           VALUES (%s,%s,%s,%s) RETURNING id""",
        (report["summary"], json.dumps(metrics, ensure_ascii=False, default=str),
         json.dumps(report["anomalies"], ensure_ascii=False),
         json.dumps(report["suggestions"], ensure_ascii=False)))["id"]
    return {"report_id": rid, **report, "metrics": metrics}


def latest_report() -> Dict | None:
    r = pg_store.query_one(
        "SELECT * FROM diagnosis_report ORDER BY id DESC LIMIT 1")
    return dict(r) if r else None


def history(limit: int = 20) -> list[dict]:
    rows = pg_store.query(
        f"SELECT id, summary, created_at FROM diagnosis_report ORDER BY id DESC LIMIT {int(limit)}")
    for r in rows:
        r["created_at"] = str(r["created_at"])
    return rows
