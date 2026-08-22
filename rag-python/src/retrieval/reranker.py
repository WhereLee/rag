"""
Reranker（CrossEncoder，ONNX INT8，bge-reranker-v2-m3）。

- 全局单例（569MB 权重全进程只加载一份）
- 信号量限流：默认 2 并发（4 核 = 2 线程 × 2 实例饱和；CPU 共享池 CFS 调度）
- 排队超时 → RerankBusyError（上层降级 RRF 排序，与 pytxt 生产策略一致）
"""
import logging
import threading
from typing import List, Tuple

import numpy as np

import config

logger = logging.getLogger("rag.reranker")

_session = None
_tokenizer = None
_lock = threading.Lock()
_semaphore = threading.Semaphore(config.RERANK_CONCURRENCY)


class RerankBusyError(RuntimeError):
    """排队硬超时——上层报错返回（2026-08-23 定夺：不再降级 RRF）"""


def _load():
    global _session, _tokenizer
    if _session is None:
        with _lock:
            if _session is None:
                import onnxruntime as ort
                from transformers import AutoTokenizer
                path = config.MODELS_DIR / config.RERANK_MODEL_DIR
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = config.RERANK_THREADS
                opts.inter_op_num_threads = 1
                if config.RERANK_ARENA_STRATEGY:
                    # 内存实验（2026-08-23）：kSameAsRequested = arena 按需扩展
                    opts.add_session_config_entry("session.arena_extend_strategy",
                                                  config.RERANK_ARENA_STRATEGY)
                logger.info("加载 Reranker: %s", config.RERANK_MODEL_DIR)
                _session = ort.InferenceSession(
                    str(path / "model.onnx"), sess_options=opts,
                    providers=["CPUExecutionProvider"])
                _tokenizer = AutoTokenizer.from_pretrained(str(path))
    return _session, _tokenizer


def rerank(query: str, passages: List[str], wait_seconds: int = 0) -> List[Tuple[int, float]]:
    """
    对 (query, passage) 打分。返回 [(原下标, logits)]，按分数降序。
    logits < 0 视为不相关（CrossEncoder 约定）。
    wait_seconds=0 表示使用全局 RERANK_TIMEOUT。
    """
    if not passages:
        return []
    timeout = wait_seconds or config.RERANK_TIMEOUT
    if not _semaphore.acquire(timeout=timeout):
        raise RerankBusyError(f"rerank 排队超过 {timeout}s，系统繁忙请重试")
    try:
        session, tokenizer = _load()
        pairs = [(query, p) for p in passages]
        inputs = tokenizer([p[0] for p in pairs], [p[1] for p in pairs],
                           padding=True, truncation=True, max_length=512,
                           return_tensors="np")
        in_names = [i.name for i in session.get_inputs()]
        feed = {n: inputs[n].astype(np.int64) for n in in_names if n in inputs}
        outputs = session.run(None, feed)
        logits = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
        order = np.argsort(-logits)
        return [(int(i), float(logits[i])) for i in order]
    finally:
        _semaphore.release()
