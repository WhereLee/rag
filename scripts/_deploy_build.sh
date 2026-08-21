#!/bin/bash
# 部署辅助：配置镜像加速并构建（Java + 前端）
set -e
echo "== maven aliyun mirror =="
mkdir -p ~/.m2
cat > ~/.m2/settings.xml <<'EOF'
<settings>
  <mirrors>
    <mirror>
      <id>aliyun</id>
      <mirrorOf>central</mirrorOf>
      <url>https://maven.aliyun.com/repository/public</url>
    </mirror>
  </mirrors>
</settings>
EOF
echo "== npm taobao mirror =="
npm config set registry https://registry.npmmirror.com
echo "== mvn package =="
cd /opt/rag/rag-java && mvn -q package -DskipTests 2>&1 | tail -5
echo "== npm ci + build =="
cd /opt/rag/rag-frontend && npm ci 2>&1 | tail -3 && npm run build 2>&1 | tail -6
echo "== verify artifacts =="
ls -lh /opt/rag/rag-java/target/*.jar 2>/dev/null | head -2
ls /opt/rag/rag-frontend/dist 2>/dev/null | head -8
echo "== DONE =="
