#!/bin/bash
# 部署辅助：nginx 站点配置（静态 + /api 反代 + SSE）
set -e
sudo tee /etc/nginx/sites-available/rag > /dev/null <<'EOF'
server {
    listen 80;
    server_name _;

    root /opt/rag/rag-frontend/dist;
    index index.html;

    client_max_body_size 50m;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8082;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
EOF
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/rag /etc/nginx/sites-enabled/rag
sudo nginx -t
sudo systemctl reload nginx
echo "== verify static =="
curl -s -m 5 http://127.0.0.1/ | head -3
echo "== verify api =="
curl -s -m 5 http://127.0.0.1/api/health; echo
echo "== DONE =="
