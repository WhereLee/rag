#!/bin/bash
# 部署辅助：安装构建工具（Maven + Node.js）
set -e
echo "== apt install maven nodejs npm =="
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq maven nodejs npm 2>&1 | tail -2
echo "== versions =="
mvn -version | head -1
node --version
npm --version
echo "== DONE =="
