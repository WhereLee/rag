"""
PostgreSQL 连接池封装（psycopg3）。

- ThreadedConnectionPool 语义等价：psycopg_pool.ConnectionPool（线程安全）
- min=5 / max=20（与 pytxt 生产经验一致；PG 每连接约 10MB，20 连接安全低于 max_connections=100）
- `with pg_store.connect() as conn:` 自动 commit/rollback + 归还连接
"""
import logging
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

import config

logger = logging.getLogger("rag.db")

_pool: ConnectionPool | None = None


def _create_pool() -> ConnectionPool:
    return ConnectionPool(
        conninfo=config.PG_DSN,
        min_size=config.PG_POOL_MIN,
        max_size=config.PG_POOL_MAX,
        kwargs={"row_factory": dict_row},
        open=False,
    )


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = _create_pool()
        _pool.open(wait=True)
        logger.info("PG pool opened (min=%d, max=%d)", config.PG_POOL_MIN, config.PG_POOL_MAX)
    return _pool


@contextmanager
def connect():
    """借出一个连接；正常退出 commit，异常 rollback。"""
    pool = get_pool()
    with pool.connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query(sql: str, params=None) -> list[dict]:
    with connect() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall()


def query_one(sql: str, params=None) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=None):
    with connect() as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount


def close():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
