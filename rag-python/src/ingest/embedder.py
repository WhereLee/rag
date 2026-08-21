"""Embedding 运行时（新链路薄封装）。

复用既有模型推理底座（retrieval.embedder 已实现：全局单例、批量推理、mean pooling、
L2 归一化、OMP 线程绑定）——模型加载/推理是技术底座而非业务逻辑，不重复实现。
新链路（rag_chunk 入库）只依赖 embed_batch 这一个接口。
"""
from __future__ import annotations

from typing import List

from retrieval.embedder import get_embedder


def embed_batch(texts: List[str]) -> List[List[float]]:
    """批量编码（内部批处理 32 + 归一化）。返回 (N, 768) 列表；失败抛异常由调用方标失败。"""
    if not texts:
        return []
    vectors = get_embedder().encode(texts)
    return [row.tolist() for row in vectors]
