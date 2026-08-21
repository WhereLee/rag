# 智能文档问答系统（RAG）

企业级 RAG 系统：**上传（分片/秒传/配额）→ 异步解析管线（PDF/VLM/表格/图片）→ 混合检索（向量+BM25+RRF+rerank）→ 多轮问答（L1/L2 存档 + 长期记忆 + 思考流式）→ 反馈闭环（点赞/点踩 → bad case 归因 → 回归集升级）**。

- **Python 服务**：FastAPI 双服务——主服务 :8090（上传/解析/Agent/评估/反馈/管理）+ 问答服务 :8091（多轮问答/L1/L2 存档/思考流式）
- **Java 网关**：Spring Boot 3.5 :8082——JWT 鉴权 / IP 限流（可信代理 XFF 解析）/ 审计 / SSE 透传 / 管理 API 代理
- **前端**：Vue 3 + Element Plus（Vite dev :3000 / 生产构建 + nginx）
- **基础设施**：PostgreSQL + pgvector（块/双向量/存档/记忆/会话）、Redis（预留）、Zipkin（OTEL trace，可选）
- **模型**：LLM/VLM 走 MiMo API（mimo-v2.5，含 reasoning）；Embedding（bge-base-zh）与 Reranker（bge-reranker-v2-m3）本地 CPU ONNX int8

## 架构

```
浏览器 ──> nginx(:80/443) ──> 前端静态（dist/）
                       └──> Java 网关 :8082（鉴权/限流/审计/SSE 透传/代理白名单）
                               ├──> Python 主服务 :8090（上传/解析/Agent/评估/反馈/管理）
                               └──> Python 问答服务 :8091（多轮问答/L1L2存档/思考流式）
                                       └──> PostgreSQL :5432（pgvector）
```

**多租户**：user_id 一律从 JWT 解析（网关注入 X-User-Id + X-Gateway-Sign 签名，Python 校验后才信任）；文件/目录/会话/缓存/记忆全部按 user_id 隔离；检索 SQL 层过滤，B 用户物理查不到 A 的块。

**问答链路**（`/api/qa/ask`，SSE 事件流：meta → thinking → delta → done）：
1. **L1 精确存档**：查询归一化（全角转半角/空白压缩/尾部标点去除）→ MD5 查 `qa_cache`，命中直返（零检索零 LLM），支持**跨用户复用**（秒传同 blob 内容边界校验，防泄露）
2. **检索**：向量 top50 + BM25 top50 → RRF 融合 → rerank 精排（BM25 强命中保护：低分但词项命中的块降级保留，两路信号都弱才拒答）
3. **L2 语义参考**：query embedding 余弦 sim≥0.9 的历史存档作为 few-shot 注入；长期记忆（focus/未解疑问）recall 注入
4. **生成**：思考流式透出（前端可关）、正文按 delta 流式；拒答不存档（含 LLM 输出"资料中未找到"的兜底检测）；停止生成时部分回答落 qa_log 但**不写缓存**
5. **反馈闭环**：回答可点赞/点踩（点踩附原因）→ bad_case 快照 → LLM 归因异步化（retrieval/generation）→ 人工确认升级回归集

## 目录结构

```
rag/
├── rag-python/src/
│   ├── ingest/        解析管线（parser 新管线: pdf/docx/xlsx/pptx/txt_md/image/vlm + clean 清洗 + chunker 切块 + worker 异步任务 + indexer 落库）
│   ├── retrieval/     混合检索（向量+BM25+RRF+rerank+强命中保护+embedder）
│   ├── qa/            问答服务（L1/L2 存档、跨用户复用、多轮会话、思考流式、中断兜底）
│   ├── agent/         LangGraph Agentic RAG（route/decompose/grade/generate/reflect + prompt_guard + approval_graph + circuit_breaker）
│   ├── llm/           mimo_client（流式/思考/JSON 模式）
│   ├── memory/        长期记忆（focus 提取 + recall + 会话历史）
│   ├── feedback/      反馈归因 attributor（异步归因 + bad case 升级回归集）
│   ├── eval/          评估（黄金集 40 题：factual/table/cross_page/refuse）
│   ├── api/           主服务路由（ingest/rag/agent/eval/feedback/prompt/diagnosis）
│   ├── mcp_server/    MCP stdio server（暴露检索/问答工具）
│   ├── observability/ OTEL tracing + 结构化日志
│   └── db/            psycopg 连接池
├── rag-java/          Spring Boot 3.5 网关（JWT/限流/审计/SSE 透传/代理白名单/可信代理 XFF）
├── rag-frontend/      Vue 3（登录/文档管理/目录/会话/问答/Admin：Prompt 管理+审批+评估）
├── scripts/           初始化/语料生成/实验/验收（init_db.py、start-gateway.ps1、debug/verify_rag_*.py）
├── docs/              architecture / development-plan / DEPLOYMENT（部署手册）/ 坑位记录 / 面试素材积累
└── data/              corpus（语料）/ parsed（解析缓存）/ jobs（worker 任务文件）
```

## 快速启动（本地开发）

前置：PostgreSQL（pgvector）、Python 3.13、JDK 17+、Node 18+；`rag/.env` 配置 MIMO_API_KEY / PG_DSN / INTERNAL_API_KEY 等（参考 `docs/DEPLOYMENT.md` 环境变量表）。

```powershell
# 1. 初始化数据库（建 rag_kb + 基础表 + 分块/存档表）
python scripts\init_db.py
PGCLIENTENCODING=UTF8 psql -h localhost -U postgres -d rag_kb -f scripts\init_chunk.sql

# 2. 启动 Python 双服务（各开一个终端，必须在 src 目录下）
cd rag-python\src
python -m uvicorn api.app:app --port 8090     # 主服务（上传/解析/评估/反馈）
python -m uvicorn qa.app:app --port 8091      # 问答服务（多轮/存档/思考流式）

# 3. 启动 Java 网关（必须走脚本注入 .env 安全配置，直接 mvn 会 fail-fast）
powershell -File scripts\start-gateway.ps1

# 4. 启动前端（开发模式，代理到网关）
cd rag-frontend; npm install; npm run dev    # http://localhost:3000
```

生产部署（nginx 反代 + systemd + 内存预算）见 **`docs/DEPLOYMENT.md`**。

## 主要 API（经网关 8082，JWT 鉴权）

| 路由 | 说明 |
|---|---|
| `/api/auth/register` `/login` | 注册（IP 限流 3 次/分）/ 登录（限流 + 连续失败锁定） |
| `/api/files/**` `/api/dirs/**` | 文件（分片上传/秒传/列表分页/回收站）/ 单层目录 |
| `/api/qa/ask` | 问答 SSE 流式（thinking/delta/done，done 带 qa_log_id） |
| `/api/qa/sessions/**` | 会话（目录绑定/多轮历史/摘要） |
| `/api/chat/ask-stream` | 旧链路对话（Agent 版，历史兼容） |
| `/api/feedback`（经 admin 代理） | 反馈提交（点赞/点踩+原因）/ bad case 列表/归因/确认 |
| `/api/admin/**` | Prompt 版本管理 + HITL 审批 + 评估运行 |

Python 直连（8090/8091）需 X-User-Id + X-Internal-Key/X-Gateway-Sign 校验，仅供内部脚本/运维。

## 测试与验收

- **单元/集成**：`python -m pytest rag-python/tests`（135 项）+ `mvn test`（Java 网关 17 项）
- **功能验收脚本**（`scripts/debug/verify_rag_*.py`）：P1 目录 / P3 存档（normal+edge）/ P4 L2+记忆 / P5 跨用户复用 / 反馈闭环 / 停止生成 / 输入限制 / XFF 限流，每个脚本含正常 + 边界用例
- **浏览器 E2E**：Browser 子代理全链路（上传→解析轮询→问答→思考区→反馈→停止）
- **评估体系**：黄金集 40 题（`scripts/run_experiments.py`，E1/E3/E4/E5 实验记录于 `docs/experiments/`）

## 关键设计决策

1. **两级问答存档（L1 精确 + L2 语义）**：同问题直返零成本；近似问题注入历史回答参考；文件变更（重解析/软删）事件驱动失效 + 幂等重建；跨用户复用带"同 blob 内容边界"安全校验（坑位/面试素材见 docs）
2. **拒答双保险**：rerank 阈值 + BM25 强命中保护（召回取并集、拒答取交集）；拒答不存档（含 LLM 拒答文案兜底检测）
3. **思考流式 + 开关**：reasoning 逐块透出（不落库）；`max_tokens=8192` 同时覆盖思考+正文（预算不足正文被挤空）；开关默认开（忠实度 E3 消融：table 1.0→0.825）
4. **停止生成**：AbortController → 网关断开传播 → Python GeneratorExit → finally 兜底（部分回答落 qa_log、不写缓存）
5. **反馈闭环**：点赞/点踩 → bad_case 快照 → LLM 归因异步化（20-60s 不阻塞提交）→ 人工确认升级回归集（HITL）
6. **限流信任边界**：默认不信任 X-Forwarded-For（防伪造绕过）；配置可信代理后按标准代理链语义解析（从右往左取第一个非代理 IP）
7. **安全默认值 fail-fast**：Java/Python 双端敏感配置无代码内默认值，缺失拒绝启动（防部署静默使用默认密码）

## 已知约束

- 本地 CPU 推理：reranker 单次约 1 秒级，高并发下检索延迟上升
- MiMo 为远程 API：网络波动时思考阶段可能挂起（1-4 分钟属深度推理正常范围）；judge 指标按需开关控制成本
- 限流为进程内实现（单机形态）；多实例扩展时迁移 Redis 共享计数（演进路径见 `docs/坑位记录.md` #1）
- 中文问题查英文文档召回偏弱（已知演进项，见坑位 #25）

详细设计见 `docs/architecture.md`，部署见 `docs/DEPLOYMENT.md`，历史决策与坑位见 `docs/坑位记录.md`。
