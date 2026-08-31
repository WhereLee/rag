"""隔离空间过滤单测（双项目集成任务1）：_scope_filter 的 personal/org 分支语义。

不依赖数据库——只验证 SQL 片段构造正确性（个人路径行为不变 + org 分支 + 互斥保护）。
"""
import pytest

from retrieval.retriever import _scope_filter


def test_personal_branch_keeps_legacy_behavior():
    """个人空间：过滤条件与旧版等价（附加 owner_type 显式声明，存量行默认值即 personal）。"""
    sql, params = _scope_filter(user_id=42, org_id=None)
    assert "uf.owner_type='personal'" in sql
    assert "uf.user_id=%s" in sql
    assert "org_id" not in sql
    assert params == [42]


def test_org_branch_filters_by_club():
    """组织空间：按社团（org_id）过滤，不触碰 user_id（org 文件 user_id 为 NULL）。"""
    sql, params = _scope_filter(user_id=None, org_id=7)
    assert "uf.owner_type='org'" in sql
    assert "uf.org_id=%s" in sql
    assert "user_id" not in sql
    assert params == [7]


def test_personal_and_org_mutually_exclusive():
    """互斥保护：同时指定两个空间是编程错误（防越权混查），直接报错。"""
    with pytest.raises(ValueError):
        _scope_filter(user_id=1, org_id=2)


def test_dir_id_appends_after_scope():
    """目录限定叠加在隔离空间之后（个人空间 + 目录）。"""
    sql, params = _scope_filter(user_id=42, org_id=None, dir_id=9)
    assert "uf.dir_id=%s" in sql
    assert params == [42, 9]


def test_org_branch_with_dir_id():
    """org 空间也支持目录限定（参数顺序：org_id 在前）。"""
    sql, params = _scope_filter(user_id=None, org_id=7, dir_id=9)
    assert params == [7, 9]


def test_no_scope_means_full_scan():
    """两个空间都不指定 → 无过滤片段（与旧版 user_id=None 全库行为一致）。"""
    sql, params = _scope_filter(user_id=None, org_id=None)
    assert sql == ""
    assert params == []
