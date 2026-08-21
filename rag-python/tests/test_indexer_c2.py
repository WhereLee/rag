"""C2 单元测试：embedding 运行时（真实推理验证维度/归一化）+ 标题前缀注入 + 空块处理。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ingest.chunker import chunk_nodes
from ingest.embedder import embed_batch
from ingest.indexer import EMBED_MODEL, embed_texts
from ingest.parser.base import DocumentNode


def P(text, **meta):
    return DocumentNode("paragraph", text, meta)


def H(text, level=1):
    return DocumentNode("heading", text, {"level": level})


# ---------- 标题前缀注入（纯逻辑，不依赖模型） ----------

class TestEmbedTexts:
    def test_prefix_injection(self):
        chunks = chunk_nodes([H("第一章", 1), H("1.1 背景", 2), P("正文内容" * 30)])
        texts = embed_texts(chunks)
        assert len(texts) == 1
        assert texts[0].startswith("第一章 > 1.1 背景 正文内容")

    def test_no_heading_plain(self):
        chunks = chunk_nodes([P("无标题正文" * 30)])
        assert embed_texts(chunks) == ["无标题正文" * 30]

    def test_count_matches(self):
        nodes = [H("甲", 1), P("段落一" * 30), P("段落二" * 30),
                 DocumentNode("table", "| a |\n|---|\n| b |", {})]
        chunks = chunk_nodes(nodes)
        assert len(embed_texts(chunks)) == len(chunks)


# ---------- 真实 embedding 推理（模型加载一次，验证输出契约） ----------

class TestEmbedBatch:
    def test_dim_and_count(self):
        vecs = embed_batch(["这是第一个测试文本，用于验证嵌入输出。",
                            "第二个测试文本，验证批量处理。",
                            "第三段测试文本，确认数量与维度正确。"])
        assert len(vecs) == 3
        assert all(len(v) == 768 for v in vecs)

    def test_normalized(self):
        import math
        vecs = embed_batch(["归一化验证文本"])
        norm = math.sqrt(sum(x * x for x in vecs[0]))
        assert abs(norm - 1.0) < 1e-4

    def test_similarity_ordering(self):
        # 同义文本应比无关文本更近（验证模型语义方向正确）
        import math
        a = embed_batch(["数据库用于存储数据"])[0]
        b = embed_batch(["数据库管理系统"])[0]
        c = embed_batch(["今天天气很好"])[0]

        def cos(x, y):
            return sum(i * j for i, j in zip(x, y))

        assert cos(a, b) > cos(a, c)

    def test_empty(self):
        assert embed_batch([]) == []


# ---------- 模型配置 ----------

class TestConfig:
    def test_embed_model_name(self):
        assert EMBED_MODEL == "bge-base-zh-v1.5-onnx-int8"
