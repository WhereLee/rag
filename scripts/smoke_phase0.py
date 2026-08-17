"""Phase 0 冒烟测试：PG 连接池 / MiMo 双档位 / 本地模型推理。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rag-python" / "src"))

import config  # noqa: E402
from observability.logging_setup import setup_logging  # noqa: E402

setup_logging("INFO")
results = []


def check(name, fn):
    t0 = time.perf_counter()
    try:
        detail = fn()
        cost = int((time.perf_counter() - t0) * 1000)
        results.append((name, "PASS", f"{detail} ({cost}ms)"))
        print(f"[PASS] {name}: {detail} ({cost}ms)")
    except Exception as e:
        results.append((name, "FAIL", str(e)))
        print(f"[FAIL] {name}: {e}")


# 1. PG 连接池 + 表结构
def t_pg():
    from db import pg_store
    row = pg_store.query_one("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema='public'")
    dims = pg_store.query_one("""SELECT atttypmod FROM pg_attribute
        WHERE attrelid='kb_chunk'::regclass AND attname='embedding'""")
    assert row["n"] >= 14, f"tables={row['n']}"
    return f"tables={row['n']}, embed_dim={dims['atttypmod']}"


check("postgres pool", t_pg)


# 2. Redis
def t_redis():
    import redis
    r = redis.Redis.from_url(config.REDIS_URL, socket_timeout=2)
    r.set("rag:smoke", "1", ex=10)
    assert r.get("rag:smoke") == b"1"
    return "ping/set/get ok"


check("redis", t_redis)


# 3. MiMo 关思考（轻任务档位）
def t_mimo_nothink():
    from llm.mimo_client import get_client
    r = get_client().chat(
        [{"role": "user", "content": "回复两个字：正常"}],
        thinking=False, max_tokens=200)
    assert "正常" in r.content, f"content={r.content!r}"
    return f"content={r.content!r} in={r.token_in} out={r.token_out} reason_tok~"


check("mimo no-thinking", t_mimo_nothink)


# 4. MiMo 开思考（生成档位）
def t_mimo_think():
    from llm.mimo_client import get_client
    r = get_client().chat(
        [{"role": "user", "content": "17*23等于几？只回答数字"}],
        thinking=True, max_tokens=2000)
    assert "391" in r.content, f"content={r.content!r}"
    return f"answer={r.content.strip()!r} reasoning_len={len(r.reasoning)}"


check("mimo thinking", t_mimo_think)


# 5. MiMo JSON 模式
def t_mimo_json():
    from llm.mimo_client import get_client
    obj = get_client().chat_json(
        [{"role": "user", "content": '返回JSON：{"category": "问题类型"}，把“帮我查一下合同条款”分类为 factual 或 analytical'}],
        thinking=False, max_tokens=300)
    assert isinstance(obj, dict) and obj, f"obj={obj}"
    return f"json={obj}"


check("mimo json mode", t_mimo_json)


# 6. MiMo 流式
def t_mimo_stream():
    from llm.mimo_client import get_client
    pieces = list(get_client().stream(
        [{"role": "user", "content": "数到5，只输出数字"}], thinking=False, max_tokens=200))
    text = "".join(pieces)
    assert len(pieces) > 1 and text, f"pieces={len(pieces)}"
    return f"chunks={len(pieces)} text={text.strip()!r}"


check("mimo stream", t_mimo_stream)


# 7. bge-base embedding
def t_bge():
    from retrieval.embedder import get_embedder
    e = get_embedder("bge-base-zh-v1.5-onnx-int8")
    v = e.encode(["合同解除的条件是什么", "今天天气不错"])
    assert v.shape == (2, 768), f"shape={v.shape}"
    import numpy as np
    sim = float(np.dot(v[0], v[1]))
    return f"shape={v.shape} cross_sim={sim:.3f}"


check("bge-base int8", t_bge)


# 8. ritrieve embedding
def t_ritrieve():
    from retrieval.embedder import get_embedder
    e = get_embedder("ritrieve-zh-v1-onnx-int8")
    v = e.encode(["合同解除的条件是什么"])
    assert v.shape[1] == 1792, f"shape={v.shape}"
    return f"shape={v.shape}"


check("ritrieve int8", t_ritrieve)


# 9. reranker
def t_rerank():
    from retrieval.reranker import rerank
    scored = rerank("合同解除的条件", [
        "当事人协商一致，可以解除合同。约定解除条件成就时，解除权人可以解除合同。",
        "今天中午吃面条比较合适。"])
    assert scored[0][0] == 0 and scored[0][1] > scored[1][1], f"scored={scored}"
    return f"relevant={scored[0][1]:.2f} irrelevant={scored[1][1]:.2f}"


check("reranker int8", t_rerank)

print("\n===== SMOKE SUMMARY =====")
fails = [r for r in results if r[1] == "FAIL"]
for name, status, detail in results:
    print(f"{status} {name}: {detail}")
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
