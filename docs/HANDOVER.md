# 智能文档问答系统 — 交接文档

> 编写日期：2026-08-17
> 编写者：AI Agent（全程参与 Phase 0 - Phase 7 + 实验收尾）
> 目的：完整记录项目从需求讨论到交付的全过程，供后续接手者（人类或其他 Agent）理解上下文。

---

## 一、项目起源与原始需求

### 1.1 用户背景

- 应届毕业生，软件工程 Java 方向，准备找工作
- 已有 RAG 开发经验（前一个项目 `pytxt/rag`：小说问答系统，含混合检索、Rerank、记忆系统）
- 本地环境：i5-12500H（6P+8E）/ 16GB 内存 / 无独显 / Python 3.13 / JDK 17
- 本地已有资产：PostgreSQL 18 + pgvector、Redis、Zipkin、多个 ONNX INT8 模型

### 1.2 原始需求（2026-08-17 讨论确认）

用户的核心诉求：

1. **不要学生级别的简化**——要求按企业级工程深度做，关注效率、日志追查、并发控制、容错降级
2. **不要应付面试的心态**——项目需要经得起面试官多轮深入追问
3. **希望了解行业现状**——2026 年 8 月 AI Agent 领域的热门技术、企业标准
4. **合理引入 Agent 技术栈**——Function Calling、MCP、LangGraph 等，在有真实业务场景的项目中自然融入
5. **确定人机边界**——Agent 不应是全权决策者，关键操作需要人工审批（HITL）

### 1.3 项目定位的演变

**初始方向（已否决）：小说知识库 + 知识图谱**
- 与早期另一个 Agent 讨论的方案，详见 `docs/discussion-summary.md`
- 否决原因：独立开发无法获得高质量用户反馈数据，小说域评估无法闭环

**最终定位：智能文档问答系统**
- 经典命题，同质化严重，但破局点在工程深度而非功能数量
- 三个差异化：评估数据驱动、反馈闭环机制、解析质量护城河
- 文档：`docs/architecture.md` v2.0、`docs/development-plan.md` v2.0

### 1.4 关键技术选型决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| LLM 供应商 | 小米 MiMo API（mimo-v2.5） | DeepSeek 涨价；mimo-v2.5 全模态一个模型覆盖全部需求 |
| Agent 框架 | LangGraph (Python) | 生态最成熟，interrupt/Checkpointer/SubGraph 原生支持 |
| 网关 | Spring Boot 3.5 (Java) | 企业级框架天然适合鉴权/限流/审计 |
| 解析器 | pymupdf + MiMo VLM（自写路由） | 成本 1/10（vs MinerU 全家桶），代码全自写，叙事更强 |
| 向量模型 | bge-base-zh-v1.5 ONNX INT8 | 768 维，CPU 友好，E1 实验证明与 ritrieve 持平 |
| Reranker | bge-reranker-v2-m3 ONNX INT8 | E2 实验证明是排序质量最大单点（MRR +0.517） |
| 数据库 | PostgreSQL 18 + pgvector | 向量 + 业务 + checkpoint 全在一个库 |
| 评估方式 | 证据关键词锚定（非 chunk_id） | chunk_id 随重入库漂移，关键词锚定内容本身更稳健 |

---

## 二、系统架构

### 2.1 整体架构

```
客户端 → Java 薄网关 :8082（JWT/限流/审计/SSE透传）
         ↓
         Python 服务 :8090（FastAPI）
         ├── /api/ingest    解析路由四通道 → 切块 → 向量化 → pgvector
         ├── /api/rag       混合检索：向量(HNSW) + BM25 → RRF → Rerank → 阈值
         ├── /api/agent     LangGraph 主图：route/decompose/grade/generate/reflect
         ├── /api/eval      黄金集评估：Recall/MRR/Refuse/Judge
         ├── /api/feedback  坏例归因 → 回归集升级
         ├── /api/admin     Prompt 版本管理 + HITL 审批图
         ├── /api/diagnosis 诊断 Agent
         └── MCP server     对外暴露检索/问答工具（stdio）
         
基础设施：PostgreSQL 18+pgvector / Redis / Zipkin :9411
模型：MiMo API（LLM/VLM）/ bge-base（Embedding）/ bge-reranker（Rerank）
      全部本地 ONNX INT8 CPU 推理
```

### 2.2 核心模块清单（35 个 Python 源文件）

| 模块 | 文件 | 职责 |
|------|------|------|
| **入库** | `ingest/sync_service.py` | 入库编排：hash 幂等 → 解析 → 切块 → 向量化 → 落库 |
| | `ingest/pdf_parser.py` | PDF 解析（pymupdf 文本直提 + 表格区域检测） |
| | `ingest/doc_parser.py` | Markdown/图片解析 |
| | `ingest/vlm.py` | MiMo VLM 调用（表格结构化/扫描件转录/图片描述） |
| | `ingest/page_analyzer.py` | 页级检测器（文本密度/表格特征/扫描判定） |
| | `ingest/chunker.py` | 切块器（段落策略 + overlap + 上下文注入） |
| **检索** | `retrieval/hybrid.py` | 混合检索：向量 + BM25 → RRF → Rerank → 阈值分级 |
| | `retrieval/embedder.py` | ONNX Embedding 推理（多模型单例，mean pooling + L2 归一化） |
| | `retrieval/reranker.py` | ONNX Reranker 推理（信号量 1 并发 + 超时降级） |
| | `retrieval/bm25_index.py` | BM25 内存索引（jieba 分词，版本机制自动重建） |
| | `retrieval/semantic_cache.py` | Redis 语义缓存（余弦 >0.95 命中，入库后失效） |
| **Agent** | `agent/main_graph.py` | LangGraph 主图（route 4 档 / CRAG / 反思 / 思考档位） |
| | `agent/qa_service.py` | 基础问答服务（Phase 2 基线，语义缓存 + 记忆召回） |
| | `agent/approval_graph.py` | HITL 审批图（suggest → interrupt → execute/rollback） |
| | `agent/diagnosis.py` | 诊断 Agent（采集指标 → LLM 分析 → 报告） |
| | `agent/state.py` | AgentState 定义（TypedDict） |
| **LLM** | `llm/mimo_client.py` | MiMo 客户端（思考档位/重试/流式/JSON 输出/预算递增） |
| | `llm/prompt_loader.py` | Prompt 加载器（DB 优先 + 安全填充 + 审批刷新） |
| **评估** | `eval/evaluator.py` | 评估管线（并发 4 worker / 关键词锚定 / judge） |
| **反馈** | `feedback/attributor.py` | 负反馈归因（retrieval vs generation，规则 + LLM） |
| **记忆** | `memory/memory.py` | 会话记忆 + 长期记忆（LLM 提炼 + 语义召回） |
| **MCP** | `mcp_server/server.py` | MCP stdio server（4 工具：search/ask/list/status） |
| **观测** | `observability/tracing.py` | OTEL 追踪（Zipkin exporter） |
| | `observability/logging_setup.py` | 结构化日志（控制台 INFO + 文件 DEBUG JSON） |
| **数据** | `db/pg_store.py` | psycopg3 连接池封装（min5/max20） |
| **API** | `api/app.py` | FastAPI 应用（health + 路由挂载） |
| | `api/ingest_api.py` | 入库 API（upload / ingest-path / status / documents） |
| | `api/rag_api.py` | 问答 API（同步 + SSE 流式 + 历史） |
| | `api/agent_api.py` | Agent API（run / experiment 开关） |
| | `api/eval_api.py` | 评估 API（seed / run / runs / compare） |
| | `api/feedback_api.py` | 反馈 API（submit / bad-cases / attribute / confirm） |
| | `api/prompt_api.py` | Prompt 管理 + HITL 审批 API |
| | `api/diagnosis_api.py` | 诊断 API（trigger / latest / history） |
| **配置** | `config.py` | 全局配置（12-factor .env） |

---

## 三、开发阶段与完成状态

### Phase 0：环境与骨架 ✅

- `rag_kb` 数据库 + pgvector 扩展 + 全部表
- 项目骨架 + config.py + 结构化日志 + FastAPI health
- psycopg3 连接池 + MiMo 客户端 + 三个本地模型就位

### Phase 1：入库管线 ✅

- 四通道路由解析（文本直提 / 表格 VLM / 扫描件 VLM / 图片 VLM）
- 解析缓存（hash 落盘）+ 切块器 + 向量化入库
- 8 文档 420 块入库

### Phase 2：混合检索 + 基础问答 + 黄金评估集 ✅

- 向量 + BM25 → RRF → Rerank → 阈值分级
- 40 题黄金集四维度（factual 17 / table 8 / cross_page 7 / refuse 8）
- 评估器 v1（Context Recall / MRR / Refuse Accuracy）
- 实验 E1 / E2 / E5 完成

### Phase 3：LangGraph Agentic RAG ✅

- 主图：route(4 档) / decompose / grade(CRAG ≤2 轮) / generate(思考档位) / reflect(≤1 次)
- PostgresSaver checkpointer
- 实验 E3 / E4 / agent-base 完成（并发版）

### Phase 4：记忆系统 + 反馈闭环 ✅

- 会话记忆（滑动窗口）+ 长期记忆（LLM 提炼 + 语义召回）
- 语义缓存（Redis，入库后失效）
- 反馈 → 归因 → 回归集升级

### Phase 5：HITL 审批 + Prompt 管理 ✅

- 7 个提示词入 prompt_registry
- 审批图：suggest → interrupt → execute/rollback
- 回归门禁（自动跑新旧对比）

### Phase 6：可观测性 + 诊断 Agent + MCP ✅

- OTEL → Zipkin 全链 trace
- 诊断 Agent（指标采集 → LLM 分析 → 报告）
- MCP stdio server（4 工具）

### Phase 7：Java 薄网关 + 收尾 ✅

- Spring Boot 3.5：JWT / 限流 / 审计 / SSE 透传
- 全链路回归 15 项全过
- README + 实验报告索引

---

## 四、实验结果汇总

### 4.1 检索层实验

| 实验 | run | 变量 | Context Recall | MRR | Refuse Acc | 结论 |
|------|-----|------|:---:|:---:|:---:|------|
| 基线 | 1 | bge-base + rerank | 1.0 | 0.9167 | 1.0 | — |
| E1 | 6 | ritrieve 1792 维 | 1.0 | 0.9141 | 1.0 | 持平，默认 bge-base |
| E2 | 9 | 关 rerank | 1.0 | **0.3998** | 1.0 | **精排是最大单点** |
| E5 | 8 | 排除 VLM 块 | 1.0 | 0.9479 | 1.0 | 当前语料下 VLM 块非必需 |

### 4.2 生成层实验（Agent 消融）

| 实验 | run | 变量 | MRR | faith(table) | faith(factual) | faith(cross_page) | 结论 |
|------|-----|------|:---:|:---:|:---:|:---:|------|
| agent-base | 16 | 全开 | 0.9167 | 1.0 | 0.99 | 0.87 | 基线 |
| E3 关思考 | 17 | force_thinking=False | 0.9167 | 0.825 ↓ | 0.894 ↓ | 0.886 | **思考提升忠实度** |
| E4 关反思 | 19 | disable_reflect=True | 0.8906 | 0.8125 ↓ | 0.9 ↓ | **0.757 ↓** | **反思对复杂题关键** |

### 4.3 并发改造

- 评估器从串行改为 ThreadPoolExecutor(4)
- 40 题耗时：~80 分钟 → ~20 分钟（**4 倍提速**）
- main_graph.py 新增 `get_eval_graph()`（无 checkpointer，线程安全）
- evaluator.py 新增 `concurrency` 参数（默认 4）

---

## 五、关键踩坑与修复

| # | 问题 | 修复 | 位置 |
|---|------|------|------|
| 1 | MiMo reasoning 挤占 max_tokens：finish=length 且 content 为空 | 预算逐级加倍（cap 8192）重试；chat_json 不传 response_format | `llm/mimo_client.py` |
| 2 | E1 维度不匹配：查询编码器与向量列必须配对 | COLUMN_EMBEDDER 映射自动配对 | `retrieval/hybrid.py` |
| 3 | PostgresSaver 必须 autocommit 连接 | psycopg.connect(autocommit=True) 常驻连接 | `agent/main_graph.py` |
| 4 | mcp 2.0 破坏性变更：FastMCP 移除 | 迁移到 MCPServer API | `mcp_server/server.py` |
| 5 | 评估 chunk_id 漂移 | 证据关键词锚定内容本身 | `eval/evaluator.py` |
| 6 | route 误拒 bug：route 节点将知识库内问题判为 out_of_scope | 修复 route prompt（加检索先验 + 宽松原则） | `llm/prompt_loader.py` |
| 7 | judge 偶发 JSON 截断 | reasoning 挤占 JSON 预算，max_tokens 设为 2048 | `eval/evaluator.py` |
| 8 | LangGraph checkpointer 线程不安全 | 评估专用 eval_graph（无 checkpointer） | `agent/main_graph.py` |

---

## 六、如何运行

### 6.1 环境准备

```powershell
# 前置：PostgreSQL 18（含 pgvector + pg_trgm）、Redis、Zipkin（可选）
# 配置：rag/.env 中填入 MIMO_API_KEY / PG_DSN / REDIS_URL

# 建库建表
python scripts\init_db.py

# 生成语料
python scripts\gen_corpus.py

# 启动 Python 服务
cd rag-python\src
python -m uvicorn api.app:app --port 8090

# 入库语料
curl -X POST http://localhost:8090/api/ingest/ingest-path -H "Content-Type: application/json" -d "{\"path\": \".../data/corpus\"}"

# 灌评估集 + 跑基线
curl -X POST http://localhost:8090/api/eval/seed
curl -X POST http://localhost:8090/api/eval/run

# 启动 Java 网关
cd rag-java
mvn clean package -DskipTests
java -jar target\rag-gateway-0.1.0.jar
```

### 6.2 运行实验

```powershell
cd c:\Users\lrs\Desktop\py\rag
# 单个实验
python scripts\run_experiments.py e1       # E1: ritrieve 对照
python scripts\run_experiments.py e2       # E2: 关 rerank
python scripts\run_experiments.py e3-off   # E3: 关思考
python scripts\run_experiments.py e4-off   # E4: 关反思
python scripts\run_experiments.py e5       # E5: 排除 VLM 块
python scripts\run_experiments.py agent-base  # agent 基线

# 查看结果
python scripts\_latest_status.py
```

### 6.3 全链路回归

```powershell
cd c:\Users\lrs\Desktop\py\rag
python scripts\regression_e2e.py
```

---

## 七、文档索引

| 文件 | 内容 |
|------|------|
| `docs/architecture.md` | 技术架构文档 v2.0（531 行，含全部表结构/模块设计/决策依据） |
| `docs/development-plan.md` | 开发计划 v2.0（Phase 0-7 详细任务清单） |
| `docs/FINAL-REPORT.md` | 最终交付报告（指标/回归测试/踩坑记录） |
| `docs/discussion-summary.md` | 早期讨论记录（小说图谱方向，已否决，仅历史留存） |
| `docs/experiments/README.md` | 实验报告索引（E1/E2/E5 详细结论） |
| `README.md` | 项目 README（架构图/快速启动/API 一览/已知约束） |
| `EVALUATION_GUIDE.md` | 评估执行指南（供其他 agent 执行实验用） |

---

## 八、未完成/可改进项

### 8.1 已知未完成

- **E2 reranker 模型对照**：原计划 v2-m3 vs v2-gte，因模型下载/ONNX 导出链路不可行，改为"精排开/关"对照
- **E3/E4 实验报告回填**：`docs/experiments/README.md` 中 E3/E4 栏仍写"待实验链完成"，需回填本次数据
- **README 实验表**：`README.md` 中 E3/E4/E5 结论栏仍写"待补"，需回填

### 8.2 可改进方向

- **评估器并发度提升**：当前 4 worker，可尝试 8（MiMo RPM=100 仍有空间）
- **前端对话页**：最小前端（React/Vue）未做，当前仅 API 级验证
- **A2A Agent Card**：诊断 Agent 的 A2A 暴露未实现（可选）
- **更大语料验证**：当前 420 chunks，更大规模下 VLM 块价值可能显现
- **多用户记忆隔离**：当前 memory_entry 按 user_id 隔离但仅支持 "default"

---

## 九、用户偏好与注意事项

### 9.1 用户工作风格

- **动手前必须确认需求**：每次执行操作前必须先确认具体要做什么，得到明确确认后才可动手
- **直接反馈和挑战性讨论**：不要顺着说，要像同事一样提出质疑和挑战
- **方案偏离时暂停汇报**：遇到阻碍不要自行切换备选方案，先汇报再决策
- **测试阶段只记录不修复**：遇到问题形成清单，不立即修复

### 9.2 环境约束

- 纯 CPU 环境（16GB 内存，无 GPU）：一切本地推理必须 CPU 友好（ONNX INT8）
- 磁盘有限：C 余 19G / D 余 33G / F 余 60G
- MiMo 为远程 API：网络抖动时需重试兜底
- PowerShell 环境：不支持 `&&`，用 `;` 分隔

---

## 十、一句话总结

这是一个**以工程深度取胜的智能文档问答系统**：从解析路由到 Agentic RAG，从黄金评估集到反馈闭环，从 HITL 审批到全链路可观测——每一层的价值都有数据证明。
