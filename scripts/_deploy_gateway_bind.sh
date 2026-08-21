#!/bin/bash
# 部署辅助：修正 Java 网关仅监听 127.0.0.1
set -e
sudo sed -i 's|ExecStart=/usr/bin/java -Xmx1g -jar /opt/rag/rag-java/target/rag-gateway-0.1.0.jar|ExecStart=/usr/bin/java -Xmx1g -jar /opt/rag/rag-java/target/rag-gateway-0.1.0.jar --server.address=127.0.0.1|' /etc/systemd/system/rag-gateway.service
sudo systemctl daemon-reload
sudo systemctl restart rag-gateway
sleep 6
echo "== gateway status =="
systemctl is-active rag-gateway
echo "== 8082 listening =="
sudo ss -tlnp | grep ':8082 ' | awk '{print $4}'
echo "== DONE =="
