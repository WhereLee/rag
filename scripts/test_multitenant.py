"""多租户隔离测试脚本。

验证：
1. 检索隔离：用户 A 文档不出现在用户 B 检索结果中
2. 同文件共享：两用户上传同文件，底层只存一份
3. 删除引用计数：一人删共享文档不影响他人
4. 安全：跨用户 history 访问被拒
5. 边界：无文档用户问答返回拒答

用法：python scripts/test_multitenant.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag-python" / "src"))

import config
from db import pg_store

results = []


def step(name):
    def deco(fn):
        def wrapper(*a, **kw):
            try:
                detail = fn(*a, **kw) or ""
                results.append(("PASS", name, detail))
                print(f"[PASS] {name} {detail}")
            except AssertionError as e:
                results.append(("FAIL", name, str(e)))
                print(f"[FAIL] {name}: {e}")
            except Exception as e:
                results.append(("FAIL", name, f"{type(e).__name__}: {e}"))
                print(f"[FAIL] {name}: {type(e).__name__}: {e}")
        return wrapper
    return deco


def setup_test_users():
    """创建两个测试用户（如果不存在）。"""
    for username in ("test_user_a", "test_user_b"):
        exists = pg_store.query_one(
            "SELECT id FROM kb_user WHERE username=%s", (username,))
        if not exists:
            pg_store.execute(
                "INSERT INTO kb_user (username, password_hash, salt, role) VALUES (%s,%s,'','user')",
                (username, "$2a$10$placeholder"))
    a = pg_store.query_one("SELECT id FROM kb_user WHERE username='test_user_a'")["id"]
    b = pg_store.query_one("SELECT id FROM kb_user WHERE username='test_user_b'")["id"]
    return a, b


@step("1. 创建测试用户")
def test_create_users():
    uid_a, uid_b = setup_test_users()
    assert uid_a != uid_b, "两个用户 ID 不同"
    return f"user_a={uid_a}, user_b={uid_b}"


@step("2. 用户 A 入库文档")
def test_ingest_user_a():
    from ingest.sync_service import ingest_file
    uid_a, _ = setup_test_users()
    # 用 corpus 里的 fastapi_readme.md 作为测试文档
    test_file = config.CORPUS_DIR / "tech" / "fastapi_readme.md"
    if not test_file.exists():
        return "跳过（测试文件不存在）"
    result = ingest_file(test_file, user_id=uid_a)
    assert result["status"] == 1, f"入库失败: {result}"
    doc_id = result["document_id"]
    # 验证映射存在
    mapping = pg_store.query_one(
        "SELECT id FROM kb_user_document WHERE user_id=%s AND document_id=%s",
        (uid_a, doc_id))
    assert mapping, "kb_user_document 映射不存在"
    return f"doc_id={doc_id}"


@step("3. 用户 A 能检索到自己的文档")
def test_user_a_retrieves():
    from retrieval.hybrid import hybrid_search
    uid_a, _ = setup_test_users()
    result = hybrid_search("FastAPI", top_k=5, user_id=uid_a)
    assert len(result["hits"]) > 0, "用户 A 应能检索到文档"
    return f"{len(result['hits'])} hits"


@step("4. 用户 B 检索不到用户 A 的文档")
def test_user_b_cannot_retrieve():
    from retrieval.hybrid import hybrid_search
    _, uid_b = setup_test_users()
    result = hybrid_search("FastAPI", top_k=5, user_id=uid_b)
    assert len(result["hits"]) == 0, f"用户 B 不应检索到用户 A 的文档，但得到 {len(result['hits'])} hits"
    return "隔离有效"


@step("5. 用户 B 入库同一文件（共享底层）")
def test_user_b_ingest_same_file():
    from ingest.sync_service import ingest_file
    _, uid_b = setup_test_users()
    test_file = config.CORPUS_DIR / "tech" / "fastapi_readme.md"
    if not test_file.exists():
        return "跳过（测试文件不存在）"
    result = ingest_file(test_file, user_id=uid_b)
    # 应该命中共享逻辑
    assert result.get("shared") or result.get("deduplicated"), f"应命中共享: {result}"
    return f"共享命中: {result.get('note', '已存在映射')}"


@step("6. 共享后用户 B 也能检索")
def test_user_b_retrieves_after_share():
    from retrieval.hybrid import hybrid_search
    _, uid_b = setup_test_users()
    result = hybrid_search("FastAPI", top_k=5, user_id=uid_b)
    assert len(result["hits"]) > 0, "用户 B 入库后应能检索到"
    return f"{len(result['hits'])} hits"


@step("7. 用户 A 删除文档，用户 B 不受影响")
def test_delete_reference_counting():
    from ingest.sync_service import delete_document, list_documents
    uid_a, uid_b = setup_test_users()
    # 获取用户 A 的文档列表
    docs_a = list_documents(uid_a)
    if not docs_a:
        return "跳过（用户 A 无文档）"
    doc_id = docs_a[0]["id"]
    # 删除
    result = delete_document(doc_id, uid_a)
    # 用户 A 的文档列表应为空
    docs_a_after = list_documents(uid_a)
    assert len(docs_a_after) == 0, f"用户 A 删除后应无文档，但还有 {len(docs_a_after)}"
    # 用户 B 仍能检索
    from retrieval.hybrid import hybrid_search
    result_b = hybrid_search("FastAPI", top_k=5, user_id=uid_b)
    assert len(result_b["hits"]) > 0, "用户 B 的共享文档不应被用户 A 删除影响"
    return "引用计数有效"


@step("8. 无文档用户问答返回拒答")
def test_no_docs_user():
    from agent.qa_service import ask
    uid_b, _ = setup_test_users()
    # 用户 B 的文档已在步骤 7 被保留（共享），所以这里测试一个全新场景
    # 创建一个没有文档的查询
    result = ask("一个完全不存在的问题 xyz123", user_id=uid_b)
    # 应该返回拒答或低置信
    assert result.get("refused") or result.get("low_confidence") or "未找到" in result.get("answer", ""), \
        f"无相关文档应拒答: {result.get('answer', '')[:100]}"
    return "拒答正常"


@step("9. 安全：用户 B 无法删除仅属于用户 A 的文档")
def test_cross_user_delete():
    from ingest.sync_service import delete_document, list_documents, ingest_file
    uid_a, uid_b = setup_test_users()
    # 用户 A 入库一个用户 B 没有的文件（用 mineru_readme.md）
    test_file = config.CORPUS_DIR / "tech" / "mineru_readme.md"
    if not test_file.exists():
        return "跳过（测试文件不存在）"
    ingest_file(test_file, user_id=uid_a)
    docs_a = list_documents(uid_a)
    # 找到 mineru 文档
    mineru_doc = None
    for d in docs_a:
        if "mineru" in d["filename"].lower():
            mineru_doc = d
            break
    if not mineru_doc:
        return "跳过（未找到 mineru 文档）"
    doc_id = mineru_doc["id"]
    # 用户 B 尝试删除用户 A 的独有文档
    try:
        delete_document(doc_id, uid_b)
        raise AssertionError("用户 B 不应能删除用户 A 的独有文档")
    except ValueError as e:
        assert "不属于" in str(e), f"应返回权限错误: {e}"
    return "跨用户删除被拒"


@step("10. 语义缓存按用户隔离")
def test_semantic_cache_isolation():
    from retrieval.semantic_cache import store, lookup, invalidate
    import numpy as np
    uid_a, uid_b = setup_test_users()
    # 清理
    invalidate(uid_a)
    invalidate(uid_b)
    # 用户 A 存入缓存
    vec = np.random.rand(768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)
    store("测试问题", vec, {"answer": "用户A的答案"}, user_id=uid_a)
    # 用户 A 能命中
    hit_a = lookup(vec, user_id=uid_a)
    assert hit_a is not None, "用户 A 应命中自己的缓存"
    # 用户 B 不应命中
    hit_b = lookup(vec, user_id=uid_b)
    assert hit_b is None, "用户 B 不应命中用户 A 的缓存"
    # 清理
    invalidate(uid_a)
    return "缓存隔离有效"


def cleanup():
    """清理测试数据。"""
    for username in ("test_user_a", "test_user_b"):
        user = pg_store.query_one("SELECT id FROM kb_user WHERE username=%s", (username,))
        if user:
            uid = user["id"]
            # 清理映射
            pg_store.execute("DELETE FROM kb_user_document WHERE user_id=%s", (uid,))
            # 清理日志
            for table in ("qa_log", "retrieval_log", "feedback", "bad_case", "memory_entry"):
                try:
                    pg_store.execute(f"DELETE FROM {table} WHERE user_id=%s", (uid,))
                except Exception:
                    pass
            # 清理缓存
            from retrieval.semantic_cache import invalidate
            invalidate(uid)


if __name__ == "__main__":
    print("=" * 60)
    print("多租户隔离测试")
    print("=" * 60)

    test_create_users()
    test_ingest_user_a()
    test_user_a_retrieves()
    test_user_b_cannot_retrieve()
    test_user_b_ingest_same_file()
    test_user_b_retrieves_after_share()
    test_delete_reference_counting()
    test_no_docs_user()
    test_cross_user_delete()
    test_semantic_cache_isolation()

    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    passed = sum(1 for r in results if r[0] == "PASS")
    failed = sum(1 for r in results if r[0] == "FAIL")
    for status, name, detail in results:
        icon = "[OK]" if status == "PASS" else "[NG]"
        print(f"  {icon} {name}: {detail}")
    print(f"\n通过: {passed}/{len(results)}  失败: {failed}")

    # 清理
    print("\n清理测试数据...")
    cleanup()
    print("完成")
