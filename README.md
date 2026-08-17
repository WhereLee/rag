# 智能文档问答系统（RAG）

一个端到端的生产级 RAG 系统：**多格式文档解析（含扫描件/表格/图片）→ 混合检索 → LangGraph Agentic 问答 → 评估/反馈/审批闭环 → 可观测**。

- **Python 服务**（FastAPI, 端口 8090）：解析、检索、Agent、评估、Prompt 管理、诊断、MCP
- **Java 薄网关**（Spring Boot 3, 端口 8082）：JWT 鉴权、滑动窗口限流、审计、SSE 透传
- **基础设施**：PostgreSQL 18 + pgvector（向量 + checkpoint + 全部业务表）、Redis（语义缓存）、Zipkin（OTEL trace 导出）
- **模型**：LLM/VLM 走 MiMo API（mimo-v2.5，唯一多模态档位）；Embedding 与 Reranker 纯本地 CPU ONNX int8 推理

## 架构总览

```
                          ┌──────────────────────┐
 客户端 ──JWT──▶          │  Java 薄网关 :8082    │  鉴权/限流/审计/SSE透传
                          └──────────┬───────────┘
                                     ▼
┌────────────────────────────────────────────────────────────────────┐
│                    Python 服务 :8090 (FastAPI)                      │
│                                                                    │
│  /api/ingest    解析路由四通道 ──▶ 切块 ──▶ 向量化 ──▶ pgvector     │
│     A 文本直提(PDF文本密度≥50字)                                    │
│     B 直提 + VLM 表格结构化(矢量线条检测)                            │
│     C VLM 整页转录(扫描件)        IMG VLM 语义描述(图片)             │
│                                                                    │
│  /api/rag       混合检索：向量(HNSW) + BM25(jieba) ──RRF(k=60)──▶   │
│                 bge-reranker-v2-m3 精排 ──▶ 阈值分级(reject/low)    │
│                                                                    │
│  /api/agent     LangGraph 主图：route(4档)/decompose/grade(CRAG     │
│                 ≤2轮重检索)/generate(思考档位)/reflect(仅complex)    │
│                                                                    │
│  /api/eval      黄金集40题：Context Recall / MRR / Refuse Acc /     │
│                 LLM-as-judge                                       │
│  /api/feedback  坏例归因(检索/Prompt/模型三分) ──▶ 回归集            │
│  /api/admin     Prompt 版本管理 + HITL 审批图(回归对比→interrupt)    │
│  /api/diagnosis 诊断 Agent：trace/慢调用/坏例聚合分析                │
│  MCP server     对外暴露检索/问答工具（stdio）                       │
└──────────┬──────────────────────┬──────────────────┬───────────────┘
           ▼                      ▼                  ▼
   PostgreSQL 18+pgvector      Redis 语义缓存      Zipkin :9411
   (chunks/双向量列/checkpoint)                    (OTEL trace)
```

## 目录结构

```
rag/
├── rag-python/
│   ├── src/
│   │   ├── ingest/        解析路由四通道（vlm/page_analyzer/pdf_parser/
│   │   │                  doc_parser/chunker/sync_service）
│   │   ├── retrieval/     hybrid(RRF+rerank+阈值)、bm25_index、
│   │   │                  semantic_cache
│   │   ├── agent/         main_graph(LangGraph)、qa_service、
│   │   │                  approval_graph(HITL)、diagnosis
│   │   ├── llm/           mimo_client（预算递增防 reasoning 挤占）、
│   │   │                  prompt_loader（DB 优先 + 安全填充）
│   │   ├── eval/          evaluator（关键词锚定命中判定 + judge）
│   │   ├── memory/        会话记忆（滑动摘要 + 事实抽取）
│   │   ├── feedback/      坏例归因 attributor
│   │   ├── mcp_server/    MCP stdio server
│   │   ├── observability/ OTEL tracing（Zipkin exporter）+ 结构化日志
│   │   ├── db/            psycopg 连接池
│   │   └── api/           FastAPI 路由（ingest/rag/agent/eval/feedback/
│   │                      admin(prompts+approvals)/diagnosis）
│   ├── eval/questions.json   黄金评估集 40 题（factual/table/cross_page/refuse）
│   └── models/            bge-base-zh / ritrieve-zh / bge-reranker-v2-m3
│                          （均为 ONNX int8，CPU 推理）
├── rag-java/              Spring Boot 3.5 薄网关（JWT/限流/审计/SSE）
├── scripts/               init_db / gen_corpus / run_experiments / smoke
├── data/corpus/           语料（真实 README + 自造白皮书/规范/扫描件/图片）
├── data/parsed/vlm_cache/ VLM 结果按图片 hash 落盘缓存（重入库零成本）
└── docs/                  architecture.md / development-plan.md / experiments/
```

## 快速启动

前置：PostgreSQL（含 pgvector、pg_trgm 扩展）、Redis、Zipkin（可选）、Python 3.13、JDK 17+。

```powershell
# 0. 环境配置：rag/.env 中填入 MIMO_API_KEY / PG_DSN / REDIS_URL
# 1. 建库建表（rag_kb 库 + 全部表 + 双向量列）
python scripts\init_db.py
# 2. 生成自造语料（白皮书/技术规范/扫描通知/图片）
python scripts\gen_corpus.py
# 3. 启动 Python 服务（必须从 src 目录启动）
cd rag-python\src
python -m uvicorn api.app:app --port 8090
# 4. 入库语料（解析+向量化）
curl -X POST http://localhost:8090/api/ingest/ingest-path -H "Content-Type: application/json" -d "{\"path\": \".../data/corpus\"}"
# 5. 灌评估集并跑基线
curl -X POST http://localhost:8090/api/eval/seed
curl -X POST http://localhost:8090/api/eval/run
# 6. 启动 Java 网关
cd rag-java
mvn clean package -DskipTests
java -jar target\rag-gateway-0.1.0.jar
```

网关使用示例：

```powershell
# 注册/登录
curl -X POST http://localhost:8082/api/auth/register -H "Content-Type: application/json" -d "{\"username\":\"u1\",\"password\":\"p1\"}"
$tok = (curl -s -X POST http://localhost:8082/api/auth/login -H "Content-Type: application/json" -d "{\"username\":\"u1\",\"password\":\"p1\"}" | ConvertFrom-Json).token
# 问答（走鉴权/限流/审计 → 透传 Python）
curl -X POST http://localhost:8082/api/chat/ask -H "Authorization: Bearer $tok" -H "Content-Type: application/json" -d "{\"query\":\"白皮书里调研了多少家企业？\"}"
```

## API 一览（Python 8090）

| 路径 | 说明 |
|---|---|
| `POST /api/ingest/upload` · `ingest-path` · `GET status/documents` | 文档入库管线 |
| `POST /api/rag/ask` · `GET /api/rag/history/{sid}` | 问答（stream=true 为 SSE） |
| `POST /api/agent/run` · `/api/agent/experiment` | Agent 图直调 / 实验开关 |
| `POST /api/eval/seed` · `/run` · `GET /runs` · `/compare` | 评估 |
| `GET /api/feedback/bad-cases` · `POST .../attribute` · `/confirm` | 反馈闭环 |
| `GET /api/admin/prompts` · `POST /api/admin/prompts/{code}/change` | Prompt 版本管理 |
| `GET /api/admin/approvals` · `POST /api/admin/approvals/{id}/resume` | HITL 审批 |
| `POST /api/diagnosis/trigger` · `GET latest/history` | 诊断报告 |

MCP server：`python -m mcp_server.server`（stdio，暴露 search / ask 工具）。

## 实验体系

全部实验由 `scripts/run_experiments.py` 进程内直调（不经 HTTP，避免 uvicorn 干扰），结果落 `eval_run` 并输出 JSON。40 题黄金集：factual 17 / table 8 / cross_page 7 / refuse 8。命中判定用证据关键词全包含（规避 chunk_id 随重入库漂移）。

| 实验 | 变量 | 结论 |
|---|---|---|
| E1 | embedding2 列（ritrieve-zh 1792 维）vs bge-base 768 维 | 见 `docs/experiments/` |
| E3 | 关闭思考（force_thinking=False）vs 默认 | 待补 |
| E4 | 关闭反思（disable_reflect）vs 默认 | 待补 |
| E5 | 检索排除 table/image 块（量化 VLM 结构化块价值） | 待补 |

> 注：E2（reranker v2-gte 对照）因模型下载/ONNX 导出链路在受限网络下不可行，如实留档为未执行项。

## 关键设计决策与踩坑

1. **MiMo reasoning 挤占正文**：`enable_thinking` 开启时 reasoning tokens 计入 `max_tokens` 预算，预算不足返回 `finish=length` 且 content 为空。修复：预算逐级加倍（cap 8192）重试，不计入失败重试；`chat_json` 不传 `response_format`，靠提示词约束 + 宽容解析。
2. **双向量列 AB 对照**：`embedding`（bge-base）与 `embedding2`（ritrieve 1792 维）并存，查询编码器按 `VECTOR_COLUMN` 经 `COLUMN_EMBEDDER` 映射自动配对，避免维度不匹配。
3. **PostgresSaver 必须 autocommit 连接**：`from_conn_string` 是上下文管理器不能直接用，改 `psycopg.connect(autocommit=True)` 常驻连接 + `saver.setup()`。
4. **HITL 审批量化依据**：submit 变更时自动对回归集跑新旧 Prompt 双评估，`interrupt` 携带对比指标，审批人凭数据决策；resume 用 `Command(resume=...)`。
5. **VLM 结果 hash 缓存**：解析结果按页图 hash 落盘 JSON，重复入库零 API 成本；扫描件/表格题的答案可追溯到 VLM 转录/结构化块。
6. **评估不依赖 chunk_id**：证据关键词锚定内容本身，重入库后评估仍可复现。

## 已知约束

- 纯 CPU 环境：reranker 信号量限 1 并发，超时降级跳过精排。
- MiMo 为远程 API：网络抖动时预算递增重试兜底；judge 指标按需开启控制成本。
- 网关限流为进程内滑动窗口（单机演示够用，多实例应换 Redis 令牌桶）。
