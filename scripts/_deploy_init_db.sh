#!/bin/bash
# 部署辅助：初始化数据库（幂等，可重复执行）
# 用法：bash scripts/_deploy_init_db.sh
set -e
echo "== 1/5 PG service =="
systemctl is-active postgresql

echo "== 2/5 create user (idempotent) =="
PW=$(openssl rand -hex 16)
# 已存在则忽略 CREATE 错误，统一用 ALTER 保证密码与 .env 一致
sudo -u postgres psql -q -c "CREATE ROLE rag_app LOGIN PASSWORD '$PW';" 2>/dev/null || true
sudo -u postgres psql -v ON_ERROR_STOP=1 -c "ALTER ROLE rag_app WITH PASSWORD '$PW';"

echo "== 3/5 create database (idempotent) =="
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='rag_kb'" | grep -q 1; then
  sudo -u postgres psql -c "CREATE DATABASE rag_kb OWNER rag_app;"
fi

echo "== 4/5 extensions =="
sudo -u postgres psql -d rag_kb -c "CREATE EXTENSION IF NOT EXISTS vector;"
sudo -u postgres psql -d rag_kb -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"

echo "== 5/5 write .env + verify =="
sudo -u postgres psql -d rag_kb -c "\dx" | grep -E 'vector|pg_trgm'
echo "PG_DSN=postgresql://rag_app:$PW@127.0.0.1:5432/rag_kb" | sudo tee /opt/rag/.env > /dev/null
PGPASSWORD="$PW" psql -h 127.0.0.1 -U rag_app -d rag_kb -tAc "SELECT 'connection_ok';"
echo "== DONE =="
