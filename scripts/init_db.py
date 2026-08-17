"""
创建 rag_kb 数据库并执行 init_db.sql。
用法：python scripts/init_db.py [--embed-dim 768]
"""
import argparse
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rag-python" / "src"))
import config  # noqa: E402

SQL_FILE = Path(__file__).resolve().parent / "init_db.sql"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--embed-dim", type=int, default=config.EMBED_DIM)
    parser.add_argument("--drop", action="store_true", help="先删除已存在的 rag_kb 库（危险）")
    args = parser.parse_args()

    # 连接默认 postgres 库建库
    base_dsn = config.PG_DSN.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(base_dsn, autocommit=True) as conn:
        cur = conn.execute("SELECT 1 FROM pg_database WHERE datname='rag_kb'")
        exists = cur.fetchone() is not None
        if exists and args.drop:
            conn.execute("DROP DATABASE rag_kb")
            exists = False
        if not exists:
            conn.execute("CREATE DATABASE rag_kb")
            print("[init_db] database rag_kb created")
        else:
            print("[init_db] database rag_kb already exists")

    sql = SQL_FILE.read_text(encoding="utf-8").replace("__EMBED_DIM__", str(args.embed_dim))
    with psycopg.connect(config.PG_DSN) as conn:
        conn.execute(sql)
        conn.commit()
    print(f"[init_db] tables ready (embed_dim={args.embed_dim})")


if __name__ == "__main__":
    main()
