# 部署手册（4G 单机 · 云服务器）

> 目标形态：单台 4G 内存云服务器（Linux），nginx 反代 + 前后端同机部署。
> 文档对照 `docs/坑位记录.md` 中所有 **部署必修 / 部署注意** 条目逐条落实。

## 一、架构与端口

```
浏览器 ──> nginx(80/443) ──> Java 网关(8082) ──> Python 主服务(8090, api.app)
                        │                        └──> Python 问答服务(8091, qa.app)
                        └──> 前端静态资源（dist/）
                              │
                              └──> PostgreSQL(5432, rag_kb)
```

| 组件 | 端口 | 启动方式 | 说明 |
|------|------|----------|------|
| PostgreSQL | 5432 | 系统服务 | 数据库，含 pgvector |
| Python 主服务 | 8090 | uvicorn `api.app:app` | 上传/解析/检索/Agent/评估/反馈 |
| Python 问答服务 | 8091 | uvicorn `qa.app:app` | 问答链路（L1/L2 存档、多轮、思考流式） |
| Java 网关 | 8082 | `start-gateway.ps1`（Windows）/ java -jar（Linux） | 鉴权/限流/审计/SSE 转发 |
| 前端 | 80/443 | nginx 静态 + 反代 | `npm run build` 产物 dist/ |

**只暴露 80/443**：8082/8090/8091/5432 全部绑定 127.0.0.1 或防火墙内网，禁止公网直连（Python 服务有 InternalAuthMiddleware 兜底，但纵深防御不依赖单层）。

## 二、4G 内存预算

| 组件 | 估算 |
|------|------|
| PostgreSQL 18 | ~512MB（shared_buffers=128MB + 连接开销） |
| Java 网关 | ~1.5GB（堆 1GB + 元空间/线程/堆外） |
| Python 主服务 + 问答服务 | ~1.0GB（两个进程共享模型加载峰值：bge-base ~100MB + reranker-base ~266MB + 进程开销；rerank 已从 v2-m3 543MB 切换为 base，黄金集质量零损失） |
| nginx + 系统 | ~400MB |
| **合计** | **~3.8GB（留峰值余量极小，勿再叠加常驻组件）** |

**调优要点**：
- Java：`-Xmx1g` 起，`-XX:MaxMetaspaceSize=256m`；必要时降堆到 768m。
- Python：`ONNX_THREADS=4`（4G 机器降核数省内存峰值）、`PG_POOL_MIN=2 PG_POOL_MAX=10`。
- PostgreSQL：`shared_buffers=128MB`、`max_connections=50`（默认 100 连接会吃掉大量内存）。
- 不部署 Redis（当前限流为进程内 Map；多实例扩展时再引入，见坑位记录 #1 演进）。

## 三、前置准备

```bash
# 系统依赖（Ubuntu 22.04 示例）
apt install -y postgresql postgresql-contrib nginx openjdk-17-jdk python3.12 python3.12-venv
# pgvector 扩展
apt install -y postgresql-16-pgvector   # 版本随 PG 版本

# Python 环境
cd rag/rag-python && python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .

# 前端构建
cd rag/rag-frontend && npm ci && npm run build   # 产物在 dist/

# Java 打包
cd rag/rag-java && mvn -q package -DskipTests     # 产物 target/rag-gateway-0.1.0.jar
```

**模型文件**（本地推理，必须随部署拷贝）：
- `rag-python/models/bge-base-zh-v1.5-onnx-int8/`（embedding）
- `rag-python/models/bge-reranker-base-onnx-int8/`（rerank，默认；v2-m3 模型可留作 env 回退）
- 首次启动会构建 jieba 缓存，需 `models/` 目录可写。

## 四、环境变量（生产 .env 模板）

```bash
# --- LLM ---
MIMO_API_KEY=sk-xxx                    # 必填；缺失时仅 WARN（LLM 不可用）
MIMO_BASE_URL=https://api.xiaomimimo.com/v1
MIMO_MODEL=mimo-v2.5

# --- 数据层 ---
PG_DSN=postgresql://rag_app:强密码@127.0.0.1:5432/rag_kb   # 必填；无默认值，缺失启动 fail-fast
REDIS_URL=redis://localhost:6379/0     # 预留（当前未用）

# --- 内部鉴权（Java 与 Python 必须相同）---
INTERNAL_API_KEY=生产随机串            # Python 侧
GATEWAY_INTERNAL_API_KEY=生产随机串    # Java 侧（优先读它，回退 INTERNAL_API_KEY）

# --- Java 网关 ---
GATEWAY_JWT_SECRET=生产随机串(≥32字节)  # 必填；无默认值，缺失启动 fail-fast
SPRING_DATASOURCE_PASSWORD=强密码       # 必填；无默认值，缺失启动 fail-fast
# 可信反向代理：**必须**填 nginx 所在主机 IP/CIDR（如 10.0.0.5 或 127.0.0.1），
# 否则 nginx 反代后限流会退化为"所有用户同 IP"（安全但过严）
GATEWAY_TRUSTED_PROXIES=127.0.0.1
GATEWAY_UPLOAD_QUOTA_BYTES=2147483648  # 2GB 配额
GATEWAY_CORS_ORIGINS=https://你的域名

# --- Python ---
API_PORT=8090
SPRING_PROFILES_ACTIVE=prod             # 开启 Python 侧生产安全校验（root 密码等直接拒绝启动）
ONNX_THREADS=4
PG_POOL_MIN=2
PG_POOL_MAX=10
```

**生产密码生成**：`openssl rand -hex 32`（JWT/内部 key）、`openssl rand -base64 24`（DB 密码）。

## 五、数据库初始化

```bash
cd rag
# 1. 建库 + 基础表（kb_user/user_file/kb_chunk/目录/会话等）
python scripts/init_db.py
# 2. 分块表 + 问答存档表（rag_chunk/qa_cache/memory_entry 等，幂等可重跑）
PGCLIENTENCODING=UTF8 psql -h 127.0.0.1 -U rag_app -d rag_kb -f scripts/init_chunk.sql
# 3. 管理员账号（仅初始化脚本创建，注册接口不接受 role=admin）
#    按 init_db.sql 注释中的示例创建；生产必须立即改默认密码
```

**坑位对照**：数据库账号不要用 postgres 超级用户跑应用——建专用 `rag_app` 账号并只授权 rag_kb 库（`GRANT ALL ON DATABASE rag_kb TO rag_app;` + 表级授权）。

## 六、服务启动（systemd，Linux）

```ini
# /etc/systemd/system/rag-python.service（主服务 8090）
[Unit]
Description=RAG Python Main Service
After=postgresql.service network.target

[Service]
User=rag
WorkingDirectory=/opt/rag/rag-python/src
EnvironmentFile=/opt/rag/.env
ExecStart=/opt/rag/rag-python/.venv/bin/uvicorn api.app:app --host 127.0.0.1 --port 8090
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/rag-qa.service（问答服务 8091，同上模式）
ExecStart=/opt/rag/rag-python/.venv/bin/uvicorn qa.app:app --host 127.0.0.1 --port 8091
```

```ini
# /etc/systemd/system/rag-gateway.service（Java 网关 8082）
[Service]
User=rag
WorkingDirectory=/opt/rag/rag-java
EnvironmentFile=/opt/rag/.env
ExecStart=/usr/bin/java -Xmx1g -jar /opt/rag/rag-java/target/rag-gateway-0.1.0.jar
Restart=on-failure
```

**启动顺序**：PostgreSQL → rag-python → rag-qa → rag-gateway → nginx reload。
（Python 两个服务先起：模型加载慢；网关依赖它们可用才转发成功。）

## 七、nginx 反代配置

```nginx
server {
    listen 80;
    server_name 你的域名;
    # HTTPS 建议 Let's Encrypt（certbot），此处省略证书段

    # 前端静态资源
    root /opt/rag/rag-frontend/dist;
    index index.html;
    location / { try_files $uri $uri/ /index.html; }   # Vue SPA 路由

    # API 统一反代到 Java 网关
    location /api/ {
        proxy_pass http://127.0.0.1:8082;
        proxy_http_version 1.1;
        # SSE 必须关缓冲、长超时（问答流式最长 2 分钟 + 思考阶段）
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # 追加真实客户端 IP（客户端伪造链会保留在左侧，网关从右往左取第一个非代理 IP）
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

**联动**：`GATEWAY_TRUSTED_PROXIES` 必须包含 nginx 所在主机 IP（上面示例单机同服即 127.0.0.1），
否则 `ClientIpResolver` 认为直连不是可信代理 → 忽略 XFF → 所有用户限流维度都是 127.0.0.1（限流退化为全局）。

## 八、部署后验证清单（逐条过）

| # | 验证项 | 方法 |
|---|--------|------|
| 1 | 网关安全配置生效 | 日志出现"安全配置检查通过"；删除任一必填变量启动应 fail-fast |
| 2 | Python 安全校验生效 | `SPRING_PROFILES_ACTIVE=prod` 时日志无"安全配置警告" |
| 3 | 伪造 XFF 绕不过限流 | 连续 4 次注册换 4 个伪造 XFF，第 4 次必须 429（`scripts/debug/verify_rag_xff_ratelimit.py`） |
| 4 | SSE 流式正常 | 浏览器问答页提问：思考区流式 + 正文逐字出现 + 引用正常 |
| 5 | 上传链路 | 上传 pdf/txt → 解析状态轮询 → 完成 |
| 6 | 问答存档 | 同问两次：第二次 meta.cached=true（L1 直返） |
| 7 | 跨用户隔离 | B 用户（无 A 的文件）问 A 的问题不得复用/不得泄露 A 内容 |
| 8 | 配额与限流 | 超配额上传 4xx；连续注册第 4 次 429 |
| 9 | 公网端口 | 仅 80/443 可达；8082/8090/8091/5432 外部不可访问 |
| 10 | 内存基线 | 全服务启动后 `free -m` 可用余量 ≥ 300MB；连续问答 10 轮无 OOM |
| 11 | rerank 配置生效 | 问答日志/检索结果使用 base 模型（黄金集 32 题 MRR ≥ 0.88）；`RERANK_MODEL_DIR` env 可切回 v2-m3 |

## 九、已知部署注意（详见坑位记录）

- 坑位 #2/#3/#11/#22/#27（PS 编码）：Windows 维护脚本 UTF-8 无 BOM 乱码——Linux 部署不涉及，但 Windows 运维机写脚本仍注意。
- 坑位 #25（跨语言召回盲区）：中文问题查英文文档召回弱——已知演进项，不影响部署。
- 坑位 #29（断言过时）：验收脚本语义随系统演进——回归前先读脚本注释确认预期。
- 进程守护：systemd Restart=on-failure；Python 服务崩溃后模型重新加载约 30-60s，期间网关返回"问答服务暂不可用"。
