"""
评估管线：黄金集驱动的 RAGAS 式评估。

指标：
- Context Recall@k：top-k 中是否存在包含全部证据关键词的 chunk（检索层）
- MRR：首个命中 chunk 的排名倒数均值（检索层）
- Refuse Accuracy：拒答题被正确拒答的比例（全链路）
- Faithfulness / Answer Relevancy：LLM-as-judge（可选，消耗 API）

设计说明：证据关键词匹配是自建设施下的可复现近似（chunk_id 会随重入库变化，
关键词锚定内容本身更稳健）；judge 指标按需开启控制成本。
"""
import json
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List

import config
from db import pg_store
from llm.mimo_client import get_client
from retrieval.hybrid import hybrid_search
from agent.qa_service import ask, NO_ANSWER_TEXT

logger = logging.getLogger("rag.eval")

QUESTIONS_FILE = config.PROJECT_ROOT / "eval" / "questions.json"

JUDGE_PROMPT = """你是 RAG 系统评估员。根据【问题】【参考资料】【参考答案】【系统回答】打分。
输出 JSON：{"faithfulness": 0-1, "relevancy": 0-1, "comment": "<一句话点评>"}
- faithfulness：系统回答是否忠于参考资料，无编造
- relevancy：系统回答是否切题、覆盖参考答案要点"""


def seed_questions() -> int:
    """把 questions.json 灌入 eval_question（幂等：按 question 文本去重，
    已存在的题同步 in_regression 标记）。"""
    data = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    added = 0
    for q in data:
        reg = bool(q.get("regression"))
        exists = pg_store.query_one("SELECT id FROM eval_question WHERE question=%s",
                                    (q["question"],))
        if exists:
            pg_store.execute(
                "UPDATE eval_question SET in_regression=%s WHERE id=%s",
                (reg, exists["id"]))
            continue
        pg_store.execute(
            """INSERT INTO eval_question (question, reference_answer, dimension,
                                          meta, in_regression)
               VALUES (%s,%s,%s,%s,%s)""",
            (q["question"], q.get("reference_answer", ""), q["dimension"],
             json.dumps({"evidence_keywords": q.get("evidence_keywords", [])},
                        ensure_ascii=False), reg))
        added += 1
    logger.info("eval questions seeded: +%d", added)
    return added


def _chunk_matches(content: str, keywords: List[str]) -> bool:
    return all(kw in content for kw in keywords) if keywords else False


def _chunk_any_match(content: str, keywords: List[str]) -> bool:
    """含任意证据关键词（context precision 用：相关块至少覆盖部分证据）。"""
    return any(kw in content for kw in keywords) if keywords else False


def _load_chunk_contents(chunk_ids: list[int]) -> Dict[int, str]:
    if not chunk_ids:
        return {}
    rows = pg_store.query(
        "SELECT id, content FROM kb_chunk WHERE id = ANY(%s)", (chunk_ids,))
    return {r["id"]: r["content"] for r in rows}


def _judge_answer(question: str, context: str, reference: str, answer: str) -> Dict:
    prompt = (f"【问题】{question}\n【参考资料】{context[:3000]}\n"
              f"【参考答案】{reference}\n【系统回答】{answer}")
    return get_client().chat_json(
        [{"role": "user", "content": JUDGE_PROMPT + "\n\n" + prompt}],
        thinking=False, max_tokens=2048)   # 2048：防 reasoning 挤占导致 JSON 截断


def run_eval(name: str = "", regression_only: bool = False,
             with_judge: bool = False, top_k: int = 0,
             engine: str = "baseline",
             exclude_types: tuple = (),
             use_rerank: bool = True,
             concurrency: int = 4) -> Dict:
    """跑一轮完整评估，落库 eval_run/eval_result，返回聚合指标。

    engine: baseline=基础问答管线；agent=LangGraph 主图（E3/E4 实验用）
    exclude_types: 检索时排除的 chunk 类型（E5 实验用，量化 VLM 结构化块价值）
    use_rerank: 是否启用 reranker 精排（E2 实验用，量化精排净增量）
    concurrency: 并发 worker 数（默认 4，MiMo RPM=100 下安全）
    """
    top_k = top_k or config.FINAL_TOP_K
    sql = "SELECT * FROM eval_question"
    if regression_only:
        sql += " WHERE in_regression = TRUE"
    questions = pg_store.query(sql + " ORDER BY id")
    if not questions:
        raise ValueError("评估集为空，请先 seed_questions")

    run_id = pg_store.query_one(
        "INSERT INTO eval_run (name, config) VALUES (%s,%s) RETURNING id",
        (name or f"run-{time.strftime('%Y%m%d-%H%M%S')}",
         json.dumps({"top_k": top_k, "embed_model": config.EMBED_MODEL_DIR,
                     "vector_column": getattr(config, "VECTOR_COLUMN", "embedding"),
                     "reranker": config.RERANK_MODEL_DIR, "engine": engine,
                     "regression_only": regression_only, "with_judge": with_judge,
                     "exclude_types": list(exclude_types),
                     "use_rerank": use_rerank, "concurrency": concurrency},
                    ensure_ascii=False)))["id"]

    if engine == "agent":
        from agent.main_graph import run_agent_eval

    def _ask(qtext: str) -> Dict:
        if engine == "agent":
            return run_agent_eval(qtext)
        return ask(qtext, top_k=top_k)

    def _process_one(q) -> Dict:
        """处理单个问题（线程安全）。返回 {dim, scores}。"""
        dim = q["dimension"]
        keywords = (q.get("meta") or {}).get("evidence_keywords", [])
        result = hybrid_search(q["question"], top_k=top_k,
                               exclude_types=exclude_types,
                               use_rerank=use_rerank)
        hits = result["hits"]
        hit_ids = [h["chunk_id"] for h in hits]
        contents = _load_chunk_contents(hit_ids)

        # 检索命中：首个包含全部关键词的 chunk 排名
        hit_rank = None
        for rank, cid in enumerate(hit_ids, 1):
            if _chunk_matches(contents.get(cid, ""), keywords):
                hit_rank = rank
                break

        scores: Dict = {}
        answer = ""
        if dim == "refuse":
            qa = _ask(q["question"])
            answer = qa["answer"]
            scores["refused"] = bool(qa.get("refused")) or _looks_refused(answer)
        else:
            scores["recall_hit"] = hit_rank is not None
            scores["rr"] = (1.0 / hit_rank) if hit_rank else 0.0
            # context precision（低成本近似）：top_k 中含任意证据关键词的块占比。
            # 衡量检索噪声；非 RAGAS 的 LLM 判定版，注释明示口径。
            if keywords and hit_ids:
                relevant = sum(1 for cid in hit_ids
                               if _chunk_any_match(contents.get(cid, ""), keywords))
                scores["context_precision"] = round(relevant / len(hit_ids), 4)
            if with_judge:
                qa = _ask(q["question"])
                answer = qa["answer"]
                ctx = "\n".join(contents.get(cid, "")[:400] for cid in hit_ids[:5])
                try:
                    j = _judge_answer(q["question"], ctx,
                                      q.get("reference_answer") or "", answer)
                    scores["faithfulness"] = float(j.get("faithfulness", 0))
                    scores["relevancy"] = float(j.get("relevancy", 0))
                except Exception as e:
                    logger.warning("judge failed q%s: %s", q["id"], e)

        pg_store.execute(
            """INSERT INTO eval_result (run_id, question_id, scores,
                                        retrieved_chunk_ids, answer)
               VALUES (%s,%s,%s,%s,%s)""",
            (run_id, q["id"], json.dumps(scores, ensure_ascii=False),
             hit_ids, answer))
        return {"dim": dim, "scores": scores}

    # 并发执行（生产者-消费者：题目列表 → ThreadPool workers）
    all_results = []
    done_count = 0
    _lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_process_one, q) for q in questions]
        for f in as_completed(futures):
            try:
                all_results.append(f.result())
            except Exception as e:
                logger.error("eval question failed: %s", e)
            with _lock:
                done_count += 1
                if done_count % 5 == 0 or done_count == len(questions):
                    logger.info("eval progress: %d/%d", done_count, len(questions))

    # 聚合指标
    per_dim: Dict[str, Dict[str, list]] = {}
    for r in all_results:
        dim = r["dim"]
        scores = r["scores"]
        d = per_dim.setdefault(dim, {"recall": [], "rr": [], "refuse": [],
                                     "faith": [], "relev": [], "cp": []})
        if dim == "refuse":
            d["refuse"].append(1.0 if scores.get("refused") else 0.0)
        else:
            d["recall"].append(1.0 if scores.get("recall_hit") else 0.0)
            d["rr"].append(scores.get("rr", 0.0))
        if "context_precision" in scores:
            d["cp"].append(scores["context_precision"])
        if "faithfulness" in scores:
            d["faith"].append(scores["faithfulness"])
            d["relev"].append(scores["relevancy"])

    metrics: Dict = {"total": len(questions), "top_k": top_k, "by_dimension": {}}
    all_recall, all_rr, all_refuse, all_cp = [], [], [], []
    for dim, d in per_dim.items():
        m = {}
        if d["recall"]:
            m["context_recall"] = round(sum(d["recall"]) / len(d["recall"]), 4)
            m["mrr"] = round(sum(d["rr"]) / len(d["rr"]), 4)
            m["n"] = len(d["recall"])
            all_recall += d["recall"]
            all_rr += d["rr"]
        if d["cp"]:
            m["context_precision"] = round(sum(d["cp"]) / len(d["cp"]), 4)
            all_cp += d["cp"]
        if d["refuse"]:
            m["refuse_accuracy"] = round(sum(d["refuse"]) / len(d["refuse"]), 4)
            m["n"] = len(d["refuse"])
            all_refuse += d["refuse"]
        if d["faith"]:
            m["faithfulness"] = round(sum(d["faith"]) / len(d["faith"]), 4)
            m["relevancy"] = round(sum(d["relev"]) / len(d["relev"]), 4)
        metrics["by_dimension"][dim] = m
    if all_recall:
        metrics["context_recall"] = round(sum(all_recall) / len(all_recall), 4)
        metrics["mrr"] = round(sum(all_rr) / len(all_rr), 4)
    if all_refuse:
        metrics["refuse_accuracy"] = round(sum(all_refuse) / len(all_refuse), 4)
    if all_cp:
        metrics["context_precision"] = round(sum(all_cp) / len(all_cp), 4)

    pg_store.execute("UPDATE eval_run SET metrics=%s WHERE id=%s",
                     (json.dumps(metrics, ensure_ascii=False), run_id))
    logger.info("eval run %s done: %s", run_id, metrics)
    return {"run_id": run_id, "metrics": metrics}


def _looks_refused(answer: str) -> bool:
    markers = ["未找到", "没有找到", "无法回答", "不在", "没有相关", "未包含"]
    return any(m in answer for m in markers)


def compare_runs(run_a: int, run_b: int) -> Dict:
    a = pg_store.query_one("SELECT metrics FROM eval_run WHERE id=%s", (run_a,))
    b = pg_store.query_one("SELECT metrics FROM eval_run WHERE id=%s", (run_b,))
    if not a or not b:
        raise ValueError("run 不存在")
    ma, mb = a["metrics"], b["metrics"]
    diff = {}
    for key in ("context_recall", "mrr", "refuse_accuracy"):
        if key in ma and key in mb:
            diff[key] = round(mb[key] - ma[key], 4)
    return {"run_a": run_a, "run_b": run_b, "metrics_a": ma, "metrics_b": mb,
            "delta_b_minus_a": diff}
