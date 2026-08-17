"""
语义缓存（Redis）：相似问题命中历史答案，高频重复问题零 LLM 成本。

实现：查询 embedding 与缓存内所有条目暴力余弦比较（缓存规模 <1000，O(n) 可接受）；
阈值内命中直接返回。入库后 invalidate（知识变化，旧答案作废）。

淘汰策略：MAX_ENTRIES 满时按 ts 淘汰最旧 20%（LRU 近似），
替代旧版"满 500 全清"——后者导致命中率周期性归零。
"""
import json
import logging
import time

import numpy as np
import redis

import config

logger = logging.getLogger("rag.semcache")

KEY = "rag:semcache"
SIM_THRESHOLD = 0.95   # 高阈值：只拦几乎相同的问题，防语义漂移误命中
MAX_ENTRIES = 500
EVICT_RATIO = 0.2      # 满员时淘汰最旧的比例

_client: redis.Redis | None = None


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(config.REDIS_URL, socket_timeout=2)
    return _client


def _decode(item: str) -> dict | None:
    try:
        return json.loads(item)
    except (json.JSONDecodeError, TypeError):
        return None


def _evict_oldest(r: redis.Redis) -> None:
    """按 ts 淘汰最旧 EVICT_RATIO 比例条目（LRU 近似）。失败静默，不影响主流程。"""
    try:
        entries = r.hgetall(KEY)
        if len(entries) < MAX_ENTRIES:
            return
        decoded = [(k, _decode(v)) for k, v in entries.items()]
        valid = [(k, d) for k, d in decoded if d is not None]
        # 无 ts 的旧条目视为最旧（排在前面优先淘汰）
        valid.sort(key=lambda kv: kv[1].get("ts", 0.0))
        n_evict = max(1, int(len(valid) * EVICT_RATIO))
        keys_to_drop = [k for k, _ in valid[:n_evict]]
        if keys_to_drop:
            r.hdel(KEY, *keys_to_drop)
            logger.info("semcache evicted %d oldest entries", len(keys_to_drop))
    except Exception as e:
        logger.warning("semcache evict failed: %s", e)


def lookup(qvec: np.ndarray) -> dict | None:
    try:
        r = _redis()
        entries = r.hgetall(KEY)
        if not entries:
            return None
        best_sim, best_val = 0.0, None
        for _, raw in entries.items():
            item = _decode(raw)
            if item is None:
                continue
            try:
                vec = np.asarray(item["embedding"], dtype=np.float32)
            except KeyError:
                continue
            sim = float(np.dot(qvec, vec))
            if sim > best_sim:
                best_sim, best_val = sim, item
        if best_sim >= SIM_THRESHOLD and best_val:
            logger.info("semantic cache hit: sim=%.4f", best_sim)
            # 命中刷新 ts（LRU 近似：命中越新越晚被淘汰）
            try:
                best_val["ts"] = time.time()
                r.hset(KEY, f"{best_val.get('query', '')[:60]}:{len(best_val.get('query', ''))}",
                       json.dumps(best_val, ensure_ascii=False))
            except Exception:
                pass
            return best_val.get("payload")
    except Exception as e:
        logger.warning("semcache lookup failed: %s", e)
    return None


def store(query: str, qvec: np.ndarray, payload: dict):
    try:
        r = _redis()
        if r.hlen(KEY) >= MAX_ENTRIES:
            _evict_oldest(r)
        item = {"query": query,
                "embedding": qvec.astype(np.float32).tolist(),
                "payload": payload,
                "ts": time.time()}
        r.hset(KEY, f"{query[:60]}:{len(query)}", json.dumps(item, ensure_ascii=False))
    except Exception as e:
        logger.warning("semcache store failed: %s", e)


def invalidate():
    try:
        _redis().delete(KEY)
        logger.info("semantic cache invalidated")
    except Exception as e:
        logger.warning("semcache invalidate failed: %s", e)
