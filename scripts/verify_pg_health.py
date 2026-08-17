"""快速验证 PG 表数量 + FastAPI /health。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rag-python" / "src"))

from db import pg_store

r = pg_store.query_one("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema='public'")
dims = pg_store.query_one("SELECT atttypmod FROM pg_attribute WHERE attrelid='kb_chunk'::regclass AND attname='embedding'")
assert r["n"] >= 14, r
print(f"[PASS] pg: tables={r['n']} embed_dim={dims['atttypmod']}")
pg_store.close()

# FastAPI TestClient（不占端口）
from fastapi.testclient import TestClient
from api.app import app

c = TestClient(app)
resp = c.get("/health")
body = resp.json()
print(f"[/health] status={resp.status_code} body={body}")
assert resp.status_code == 200 and body["checks"]["postgres"] == "up" and body["checks"]["redis"] == "up"
print("[PASS] fastapi health")
