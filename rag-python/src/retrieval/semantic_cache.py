"""
语义缓存（Redis）：相似问题命中历史答案，高频重复问题零 LLM 成本。

多租户：Redis key 按用户隔离（rag:semcache:{user_id}），避免跨用户缓存命中。
实现：查询 embedding 与缓存内所有条目暴力余弦比较（缓存规模 <1000，O(n) 可接受）；
阈值内命中直接返回。入库后 invalidate（知识变化，旧答案作废）。

淘汰策略：MAX_ENTRIES 满时按 ts 淘汰最旧 20%（LRU 近似）。
"""
import json
import logging
import time

import numpy as np
import redis

import config

logger = logging.getLogger("rag.semcache")

KEY_PREFIX = "rag:semcache"   # 多租户：实际 key = KEY_PREFIX:{user_id}


def _key(user_id: int | None) -> str:
    """生成多租户缓存 key。user_id=None 时用于全局操作（如全量清除）。"""
    if user_id is None:
        return f"{KEY_PREFIX}:*"   # 用于全局操作
    return f"{KEY_PREFIX}:{user_id}"
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


def _evict_oldest(r: redis.Redis, key: str) -> None:
    """按 ts 淘汰最旧 EVICT_RATIO 比例条目（LRU 近似）。失败静默，不影响主流程。"""
    try:
        entries = r.hgetall(key)
        if len(entries) < MAX_ENTRIES:
            return
        decoded = [(k, _decode(v)) for k, v in entries.items()]
        valid = [(k, d) for k, d in decoded if d is not None]
        valid.sort(key=lambda kv: kv[1].get("ts", 0.0))
        n_evict = max(1, int(len(valid) * EVICT_RATIO))
        keys_to_drop = [k for k, _ in valid[:n_evict]]
        if keys_to_drop:
            r.hdel(key, *keys_to_drop)
            logger.info("semcache evicted %d oldest entries", len(keys_to_drop))
    except Exception as e:
        logger.warning("semcache evict failed: %s", e)


def lookup(qvec: np.ndarray, user_id: int | None = None) -> dict | None:
    try:
        r = _redis()
        key = _key(user_id)
        entries = r.hgetall(key)
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
            logger.info("semantic cache hit: sim=%.4f user=%s", best_sim, user_id)
            # 命中刷新 ts（LRU 近似：命中越新越晚被淘汰）
            try:
                best_val["ts"] = time.time()
                r.hset(key, f"{best_val.get('query', '')[:60]}:{len(best_val.get('query', ''))}",
                       json.dumps(best_val, ensure_ascii=False))
            except Exception:
                pass
            return best_val.get("payload")
    except Exception as e:
        logger.warning("semcache lookup failed: %s", e)
    return None


def store(query: str, qvec: np.ndarray, payload: dict, user_id: int | None = None):
    try:
        r = _redis()
        key = _key(user_id)
        if r.hlen(key) >= MAX_ENTRIES:
            _evict_oldest(r, key)
        item = {"query": query,
                "embedding": qvec.astype(np.float32).tolist(),
                "payload": payload,
                "ts": time.time()}
        r.hset(key, f"{query[:60]}:{len(query)}", json.dumps(item, ensure_ascii=False))
    except Exception as e:
        logger.warning("semcache store failed: %s", e)


def invalidate(user_id: int | None = None):
    """清除语义缓存。user_id=None 时清除所有用户的缓存（全局操作）。"""
    try:
        r = _redis()
        if user_id is None:
            # 全局清除：扫描所有 rag:semcache:* key
            for key in r.scan_iter(f"{KEY_PREFIX}:*"):
                r.delete(key)
            logger.info("semantic cache invalidated (all users)")
        else:
            r.delete(_key(user_id))
            logger.info("semantic cache invalidated: user=%s", user_id)
    except Exception as e:
        logger.warning("semcache invalidate failed: %s", e)
