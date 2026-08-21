#!/bin/bash
# 部署辅助：创建 rag 服务账号 + 注册 4 个 systemd 服务
set -e

echo "== create rag user =="
sudo useradd -m rag 2>/dev/null || true
sudo usermod -aG ubuntu rag
sudo chmod -R g+rwX /opt/rag/data /opt/rag/logs 2>/dev/null || sudo chmod g+rwX /opt/rag/data
sudo chown -R ubuntu:ubuntu /opt/rag

echo "== write systemd units =="
sudo tee /etc/systemd/system/rag-python.service > /dev/null <<'EOF'
[Unit]
Description=RAG Python Main Service (8090)
After=postgresql.service redis-server.service
Wants=postgresql.service redis-server.service

[Service]
User=rag
Group=ubuntu
WorkingDirectory=/opt/rag/rag-python/src
EnvironmentFile=/opt/rag/.env
ExecStart=/opt/rag/rag-python/.venv/bin/uvicorn api.app:app --host 127.0.0.1 --port 8090
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/rag-qa.service > /dev/null <<'EOF'
[Unit]
Description=RAG QA Service (8091)
After=postgresql.service redis-server.service
Wants=postgresql.service redis-server.service

[Service]
User=rag
Group=ubuntu
WorkingDirectory=/opt/rag/rag-python/src
EnvironmentFile=/opt/rag/.env
ExecStart=/opt/rag/rag-python/.venv/bin/uvicorn qa.app:app --host 127.0.0.1 --port 8091
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/rag-worker.service > /dev/null <<'EOF'
[Unit]
Description=RAG Parse Worker
After=postgresql.service redis-server.service
Wants=postgresql.service redis-server.service

[Service]
User=rag
Group=ubuntu
WorkingDirectory=/opt/rag/rag-python/src
EnvironmentFile=/opt/rag/.env
ExecStart=/opt/rag/rag-python/.venv/bin/python -m ingest.worker
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/rag-gateway.service > /dev/null <<'EOF'
[Unit]
Description=RAG Java Gateway (8082)
After=postgresql.service redis-server.service rag-python.service rag-qa.service
Wants=postgresql.service redis-server.service

[Service]
User=rag
Group=ubuntu
WorkingDirectory=/opt/rag/rag-java
EnvironmentFile=/opt/rag/.env
ExecStart=/usr/bin/java -Xmx1g -jar /opt/rag/rag-java/target/rag-gateway-0.1.0.jar
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "== enable =="
sudo systemctl daemon-reload
sudo systemctl enable rag-python rag-qa rag-worker rag-gateway 2>&1 | tail -4
echo "== DONE =="
