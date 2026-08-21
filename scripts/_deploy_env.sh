#!/bin/bash
# 部署辅助：组装生产 .env（保留 MIMO 密钥，重新生成生产密钥）
set -e
cd /opt/rag
PGPW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
INTERNAL_KEY=$(openssl rand -hex 24)
JWT_SECRET=$(openssl rand -hex 32)
sudo chown ubuntu:ubuntu /opt/rag/.env
cat > /opt/rag/.env <<EOF
PG_DSN=postgresql://rag_app:$PGPW@127.0.0.1:5432/rag_kb
REDIS_URL=redis://localhost:6379/0
INTERNAL_API_KEY=$INTERNAL_KEY
GATEWAY_JWT_SECRET=$JWT_SECRET
SPRING_DATASOURCE_USERNAME=rag_app
SPRING_DATASOURCE_PASSWORD=$PGPW
GATEWAY_TRUSTED_PROXIES=127.0.0.1
$(cat /tmp/mimo_keys.txt)
EOF
echo "== .env keys (values hidden) =="
sed 's/=.*/=<set>/' /opt/rag/.env
echo "== DONE =="
