"""
Embedding 推理（ONNX Runtime，CPU INT8）。

- 多模型支持：bge-base-zh-v1.5（768 维）/ ritrieve-zh-v1（1792 维，ONNX 已内嵌 Dense 层）
- 全局单例（按模型目录），ORT session 线程安全
- mean pooling + L2 归一化
- P 核线程绑定在 config 中通过 OMP_NUM_THREADS 设置
"""
import logging
import threading
import queue
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

import config

logger = logging.getLogger("rag.embedder")

_instances: dict[str, "Embedder"] = {}
_lock = threading.Lock()


def get_embedder(model_dir: str = "") -> "Embedder":
    """按模型目录获取全局单例。"""
    model_dir = model_dir or config.EMBED_MODEL_DIR
    if model_dir not in _instances:
        with _lock:
            if model_dir not in _instances:
                logger.info("加载 Embedder: %s", model_dir)
                _instances[model_dir] = Embedder(model_dir)
    return _instances[model_dir]


class Embedder:
    def __init__(self, model_dir: str, batch_size: int = 0):
        self.model_dir = model_dir
        self.path = config.MODELS_DIR / model_dir
        self.batch_size = batch_size or config.EMBED_BATCH_SIZE
        self._load()

    def _load(self):
        import onnxruntime as ort
        from transformers import AutoTokenizer
        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = config.EMBED_THREADS
        sess_opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(self.path / "model.onnx"), sess_options=sess_opts,
            providers=["CPUExecutionProvider"])
        self._tokenizer = AutoTokenizer.from_pretrained(str(self.path))
        self._input_names = [i.name for i in self._session.get_inputs()]
        out_shape = self._session.get_outputs()[0].shape
        self.dim = out_shape[-1] if isinstance(out_shape[-1], int) else None
        logger.info("Embedder ready: %s, dim=%s", self.model_dir, self.dim)

    def _pool(self, hidden: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
        """mean pooling（bge 与 ritrieve 均为 mean_tokens）。"""
        mask = np.expand_dims(attention_mask, -1)
        summed = np.sum(hidden * mask, axis=1)
        counts = np.clip(np.sum(attention_mask, axis=1, keepdims=True), 1e-9, None)
        return summed / counts

    def _infer_batch(self, texts: list[str]) -> np.ndarray:
        inputs = self._tokenizer(texts, padding=True, truncation=True,
                                 max_length=512, return_tensors="np")
        feed = {}
        for name in self._input_names:
            if name in inputs:
                feed[name] = inputs[name].astype(np.int64)
        outputs = self._session.run(None, feed)
        out = outputs[0]
        if out.ndim == 3:  # token 级输出 → mean pooling
            emb = self._pool(out, inputs["attention_mask"].astype(np.float32))
        else:              # 模型内已 pooling（如 ritrieve 的 'embedding' 输出）
            emb = out
        emb = emb.astype(np.float32)
        norms = np.linalg.norm(emb, axis=1, keepdims=True)
        return emb / np.maximum(norms, 1e-12)

    def encode(self, texts: list[str],
               progress_cb: Optional[Callable[[int, int], None]] = None) -> np.ndarray:
        """批量编码（生产者预分词流水线）。返回 (N, dim)。"""
        total = len(texts)
        if total == 0:
            return np.zeros((0, self.dim or config.EMBED_DIM), dtype=np.float32)

        batches = [texts[i:i + self.batch_size] for i in range(0, total, self.batch_size)]
        tok_queue: queue.Queue = queue.Queue(maxsize=3)

        def producer():
            for batch in batches:
                inputs = self._tokenizer(batch, padding=True, truncation=True,
                                         max_length=512, return_tensors="np")
                tok_queue.put(inputs)
            tok_queue.put(None)

        t = threading.Thread(target=producer, daemon=True)
        t.start()

        results, done, start = [], 0, time.perf_counter()
        while True:
            inputs = tok_queue.get()
            if inputs is None:
                break
            feed = {n: inputs[n].astype(np.int64) for n in self._input_names if n in inputs}
            outputs = self._session.run(None, feed)
            out = outputs[0]
            if out.ndim == 3:
                emb = self._pool(out, inputs["attention_mask"].astype(np.float32))
            else:
                emb = out
            emb = emb.astype(np.float32)
            norms = np.linalg.norm(emb, axis=1, keepdims=True)
            results.append(emb / np.maximum(norms, 1e-12))
            done += len(feed[self._input_names[0]])
            if progress_cb:
                progress_cb(done, total)
        t.join()
        elapsed = time.perf_counter() - start
        logger.info("encode done: %d texts in %.1fs (%.0f texts/s)",
                    total, elapsed, total / max(elapsed, 1e-6))
        return np.vstack(results)

    def encode_query(self, text: str) -> np.ndarray:
        """单条查询编码。"""
        return self.encode([text])[0]
