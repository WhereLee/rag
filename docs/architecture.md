# 智能文档问答系统 — 技术架构文档

> 版本：v2.0（全面修订版）
> 日期：2026-08-17
> 修订说明：替代 v1.0（小说知识库方向）。经本机资产核查与 2026-08 行业调研后重定位：
> 放弃小说图谱路线（独立开发无法获得高质量用户反馈数据），转向经典命题
> 「智能文档问答」，以**工程深度**而非功能广度建立差异化。

---

## 一、项目定位

### 1.1 一句话定位

> 一个企业级工程深度的智能文档问答系统：多格式文档（PDF/Word/图片）智能解析入库，
> 混合检索 + Agentic RAG 问答，配套黄金评估集、反馈闭环、Human-in-the-Loop 审批
> 与全链路可观测性。

### 1.2 为什么这个定位站得住

文档问答是求职市场的"超级经典案例"，同质化严重。破局点不在功能数量，而在三个
大多数学生项目没有的东西：

1. **一切优化有数据背书**——自建黄金评估集（60-80 题，标注答案来源 chunk），
   任何改动（切块策略/模型/参数）都用 RAGAS 式指标说话；
2. **反馈闭环机制**——👍/👎 → Bad Case 库 → 自动归因（检索问题 vs 生成问题）
   → 升级回归集。独立开发拿不到真实用户数据，就把"如果有用户，系统如何进化"
   做成完整机制；
3. **解析质量作为第一护城河**——RAG 效果 70% 由解析决定。按页路由的混合解析器
   （文本直提 / VLM 表格页 / VLM 扫描件）本身就是可展开的工程决策叙事。

### 1.3 面试叙事线

> "这个项目的核心不是又一个文档问答，而是我把它当成一个**需要持续运营的生产系统**
> 来建设。第一，解析层我做了按页路由——95% 的普通页面零成本直提，只有表格页和
> 扫描件才调用多模态大模型，成本是全量 VLM 解析的十分之一；第二，我从第一天就建了
> 黄金评估集，所有优化（embedding 选型、reranker 对比、反思开关）都有对照实验数据；
> 第三，我设计了反馈闭环——负样本自动归因到管道阶段，确认有效的 bad case 升级为
> 回归集，prompt 变更必须过回归门禁才能生效，这让 Human-in-the-Loop 审批有了量化
> 依据而不是走形式。"

---

## 二、本机资产基线（2026-08 实测）

| 资产 | 实况 | 用途 |
|------|------|------|
| 硬件 | i5-12500H（6P+8E）/ 16GB / 无独显 | 一切本地推理必须 CPU 友好 |
| LLM | 小米 MiMo API（OpenAI 兼容） | 唯一 LLM 供应商（DeepSeek 弃用） |
| └ mimo-v2.5 | 全模态（文本+图像），1M 上下文 | 文档解析/图表理解/答案生成，**项目主力** |
| └ mimo-v2.5-pro | 纯文本旗舰 | 备用（无图像能力，默认不用） |
| └ enable_thinking | 思考模式开关已实测生效 | 思考档位路由的成本/质量抓手 |
| Embedding | ritrieve_zh_v1（ONNX int8 313MB，D:\Projects\xs） | 候选一（检索专用） |
| | bge-base-zh-v1.5 ONNX int8（pytxt） | 候选二（基线） |
| | bge-large-zh-v1.5（HF 缓存，pytorch） | 候选三（需导出 ONNX） |
| Reranker | bge-reranker-v2-m3 ONNX int8（pytxt） | 基线；v2-gte 作为对照实验 |
| PostgreSQL | 18.3 + pgvector 0.8.2 + pg_trgm | 向量库 + 业务库（新库 rag_kb） |
| Redis | 5.0.14（F:\Redis，端口 6379） | 缓存/锁/语义缓存 |
| 磁盘 | C 余 19G / D 余 33G / F 余 60G | 大文件（解析器依赖/模型）放 F |

**硬约束**：无本地 LLM（ollama/lmstudio 为空）、无本地视觉模型、无 GPU。
所有多模态与生成能力走 MiMo API，所有向量/精排推理走本地 ONNX INT8。

---

## 三、架构总览

```
┌────────────────────────────────────────────────────────────────┐
│  接入层（后期）                                                  │
│  Java 薄网关（Spring Boot）：鉴权/限流/审计/SSE 转发 + 管理 API   │
└───────────────────────────┬────────────────────────────────────┘
                            │ HTTP（内部）
┌───────────────────────────▼────────────────────────────────────┐
│  Python 服务（FastAPI + LangGraph）                              │
│                                                                │
│  ┌─ 入库管线 ────────────────────────────────────────────────┐  │
│  │ 上传 → 解析路由（按页决策）→ 切块 → 向量化 → 入库          │  │
│  │   ├─ 文本直提（pymupdf，95% 页面）                        │  │
│  │   ├─ VLM 表格页（MiMo-V2.5 → 结构化 markdown）            │  │
│  │   ├─ VLM 扫描件（MiMo-V2.5 整页解析）                     │  │
│  │   └─ 图片文件（MiMo-V2.5 语义描述 + 文字抽取 双路）        │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  ┌─ 问答管线（LangGraph Agentic RAG）────────────────────────┐  │
│  │ 复杂度路由 → [检索 / 拆解多路检索 / 图谱? 否] → 质量评估    │  │
│  │ → 生成（思考档位）→ 反思自检 → 引用标注输出                 │  │
│  │ 不合格 → 查询改写重检索（CRAG 式）→ 仍不合格 → 明确拒答     │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  ┌─ 记忆系统 ────────────────────────────────────────────────┐  │
│  │ 会话记忆（滑动窗口+摘要） + 长期记忆（语义召回用户关注点）   │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌─ 反馈闭环 ────────────────────────────────────────────────┐  │
│  │ 👍/👎 + 纠错 → Bad Case 库 → 归因分析 → 回归集升级          │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌─ 评估体系 ────────────────────────────────────────────────┐  │
│  │ 黄金集（60-80题） + RAGAS 式指标 + 回归门禁（审批前置）      │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌─ 管理 Agent ──────────────────────────────────────────────┐  │
│  │ 诊断图（指标采集→分析→报告） + 审批图（HITL interrupt）      │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌─ MCP Server（对外集成）───────────────────────────────────┐  │
│  │ 知识库工具暴露给任意外部 MCP Client                         │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌─ 可观测性 ────────────────────────────────────────────────┐  │
│  │ OTEL 全链路追踪 + 结构化日志 + 指标（token/延迟/命中率）     │  │
│  └──────────────────────────────────────────────────────────┘  │
└───────────────────────────┬────────────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────────────┐
│  数据层                                                          │
│  PostgreSQL rag_kb：向量(pgvector HNSW) + 业务 + LangGraph 状态  │
│  Redis：语义缓存、分布式锁、会话                                  │
│  文件系统：原始文档、解析产物、模型文件（F 盘）                    │
└────────────────────────────────────────────────────────────────┘
```

---

## 四、入库管线详细设计

### 4.1 解析路由（核心设计）

逐页决策，原则：**用成本最低的、质量足够的方法**。

```
PDF 输入
 ├─ 逐页检测：文本层字符数 / 页面面积 密度
 │   ├─ 密度达标 且 无表格特征 → 通道A：pymupdf 文本直提（零 API 成本）
 │   ├─ 密度达标 且 有表格特征 → 通道B：文本直提 + 表格区域裁剪
 │   │        → 表格图发 MiMo-V2.5 → 结构化 markdown 表格替换
 │   └─ 密度不足（扫描件）     → 通道C：整页渲染 PNG → MiMo-V2.5 全页解析
 ├─ 表格特征检测：文本块行列对齐度 + 线条矢量检测（pymupdf drawings）
 └─ 每页记录 parse_channel（入库留痕，评估分维度用）

图片输入（png/jpg）
 ├─ MiMo-V2.5 → 语义描述（"这张图展示了..."）
 └─ 图中文字抽取（同一次调用要求输出两部分）→ 双路入库

Word/Markdown → 结构化直读（python-docx / markdown 库），不走 VLM
```

**设计要点**：
- 解析结果统一为**页级中间表示**（page_no、text_blocks、tables、images、channel），
  切块器只消费中间表示，解析与切块解耦；
- VLM 调用带**结构化输出约束**（JSON schema），解析失败重试 2 次后降级纯文本；
- 解析产物落盘缓存（同一文档重复入库不重复调 VLM，按文件 hash 索引）。

### 4.2 切块策略

- 文本块：段落优先，≤500 字整段，>500 字按句子边界二分；块间 overlap 60 字；
- **上下文注入**（Contextual Retrieval 思想）：每个 chunk 头部注入一行
  `《文档标题》第N章/第M页：`，成本为零、对跨页问题帮助显著；
- 表格块：整表为一个 chunk（markdown 格式），超长表按行组分块并保留表头；
- 图片块：语义描述 + 抽取文字合并为一个 chunk，metadata 记原图路径；
- 所有 chunk 记录 `chunk_type ∈ {text, table, image}` 与来源页码（引用标注用）。

### 4.3 向量化与入库

- Embedding：本地 ONNX INT8 推理（复用 pytxt 的生产者-消费者流水线 + P 核绑定）；
- 默认模型由基线实验决定（ritrieve_zh_v1 vs bge-base，见 §九）；
- 写入 `kb_chunk`（含 embedding vector(768)），HNSW 余弦索引；
- BM25 索引：jieba 分词 + rank_bm25 内存索引，按文档集构建，
  启动时加载 + 新文档入库时增量重建（文档量 <100 时重建成本可接受，留档说明）。

---

## 五、问答管线详细设计（LangGraph）

### 5.1 主图结构

```
                 ┌──────────┐
                 │  START   │
                 └────┬─────┘
                 ┌────▼─────┐
                 │  Route   │ 复杂度/类型路由（轻量 LLM，关思考）
                 └────┬─────┘
        ┌─────────────┼─────────────┐
   simple(直答)   standard(标准)   complex(复杂)     out_of_scope
        │             │              │                  │
        │        ┌────▼────┐    ┌────▼─────┐       ┌────▼────┐
        │        │Retrieve │    │Decompose │       │ Guard   │→ END
        │        └────┬────┘    └────┬─────┘       └─────────┘
        │             │         ┌────▼─────┐
        │             │         │Retrieve  │×N 子查询
        │             │         └────┬─────┘
        │             ├──────────────┤
        │        ┌────▼─────┐
        │        │ GradeCtx │ 检索质量评估（CRAG 式，关思考）
        │        └────┬─────┘
        │      合格 │        │ 不合格（≤2 次）
        │           │   ┌────▼──────┐
        │           │   │ Rewrite + │──→ 回到 Retrieve
        │           │   │ ReRetrieve│
        │           │   └────┬──────┘
        │           │        │ 仍不合格
        │           │   ┌────▼──────┐
        │           │   │ NoAnswer  │ 明确告知"文档中未找到"
        │           │   └───────────┘
        │      ┌────▼─────┐
        └─────▶│ Generate │（思考档位：simple 关 / 其余开）
               └────┬─────┘
               ┌────▼─────┐
               │ Reflect  │ 忠实度/相关性自检（仅 complex，防过度开销）
               └────┬─────┘
              通过  │   │ 不通过(≤1次) → 回 Generate 修正
               ┌────▼─────┐
               │   END    │ 带引用标注输出
               └──────────┘
```

### 5.2 关键设计决策

| 决策 | 内容 | 理由 |
|------|------|------|
| **Adaptive 路由** | simple/standard/complex 三档，只有 complex 走拆解+反思 | 延迟与 token 成本降 60%+，可量化卖点 |
| **GradeCtx 检索评估** | 轻量提示词给 top-k 打"能否支撑回答"分，低于阈值触发改写重检索 | CRAG 式纠错，比盲目生成后反思便宜 |
| **拒答能力** | 两次重检索仍不合格 → 明确说"文档中没有"，不编造 | 企业级标志，面试高频追问点 |
| **思考档位路由** | 分类/评分/改写：enable_thinking=false；生成/诊断：true | 单模型双档位，实测参数生效 |
| **反思只做一档** | 仅 complex 问答做反思，且最多重试 1 次 | 开销控制；效果由评估集开关对照验证 |
| **引用标注** | 答案中标注 [文档名 p.页码]，来源 chunk_id 随响应返回 | 可追溯，前端可跳转 |

### 5.3 混合检索管线（Retrieve 内部）

```
查询 → [可选]LLM 改写 1 个版本（仅 complex）
     → 向量检索 top24（pgvector HNSW）
     → BM25 检索 top24（jieba + 自定义词典）
     → RRF 融合（k=60）
     → Rerank 精排（bge-reranker-v2-m3 ONNX INT8，信号量限 1 并发，15s 超时降级 RRF）
     → 阈值判定：score < τ_reject → 空结果；< τ_low → 低置信标记
     → 返回 top8
```

**语义缓存**：查询 embedding 在 Redis 中近邻匹配（阈值内）直接返回历史答案，
高频重复问题零 LLM 成本。

---

## 六、记忆系统

| 层 | 内容 | 存储 |
|----|------|------|
| 会话记忆 | 最近 10 轮原文 + 更早轮次 LLM 摘要 | PG（按 session_id） |
| 长期记忆 | 用户关注文档、反复追问的主题、未解决疑问（LLM 定期提炼） | PG memory_entry + 向量索引 |

问答时：长期记忆语义召回 top3 注入系统提示（"该用户此前关注 X 文档的 Y 问题"）。

---

## 七、反馈闭环与评估体系（核心差异化）

### 7.1 黄金评估集

- 语料入库后**先建评估集再谈优化**（标尺先行）；
- 60-80 题，四维度：事实细节 / 表格图表 / 跨页综合 / 应当拒答（文档外问题）；
- 每题标注：问题、参考答案、答案来源 chunk_id、维度标签；
- 构造方式：人工精读 + LLM 辅助生成候选题 + 人工校验（记录构造过程本身）。

### 7.2 评估指标（RAGAS 式，LLM-as-judge 用 mimo-v2.5）

| 指标 | 含义 | 归属阶段 |
|------|------|---------|
| Context Recall@8 | 答案来源 chunk 是否进入 top8 | 检索 |
| MRR | 相关 chunk 平均排名 | 检索 |
| Faithfulness | 答案是否忠于检索内容（无编造） | 生成 |
| Answer Relevancy | 答案是否切题 | 生成 |
| Refuse Accuracy | 该拒答的题是否正确拒答 | 全链路 |

每次评估产出运行记录（eval_run + eval_result 表），支持任意两次运行对比。

### 7.3 反馈闭环

```
用户 👎 / 纠错
  → feedback 表 + bad_case 表（含完整链路快照：query/检索结果/答案）
  → 定期归因任务（诊断 Agent 的一部分）：
      来源 chunk 不在 top8 → 检索问题（改写/切块/embedding 候选）
      在 top8 但答案错误   → 生成问题（prompt/思考模式候选）
  → 人工确认有效的 bad case → 升级为回归集题目
  → 回归集 = prompt/参数变更审批的强制门禁
```

### 7.4 HITL 审批（LangGraph interrupt）

| 场景 | 流程 |
|------|------|
| Prompt 变更 | 提交 → Agent 跑回归集对比新旧版本 → interrupt 展示数据 → 人工决策 → 生效/回滚 |
| 索引重建 | 诊断 Agent 发现质量下降 → 建议 + interrupt → 确认后执行 |
| 参数调优（top_k/阈值） | 同上，附评估集对比数据 |

Checkpointer：PostgreSQL（psycopg3），中断状态跨请求恢复。

---

## 八、管理 Agent、可观测性、MCP

### 8.1 诊断 Agent（LangGraph 子图）

- 输入：retrieval_log / qa_trace / feedback 聚合指标；
- **冷启动解法**：评估集运行本身就是流量源——每次 eval_run 的数据即可供诊断，
  不依赖真实用户；
- 输出：诊断报告（summary + anomalies + suggestions）存 diagnosis_report 表；
- 触发：手动 + 每 N 次评估运行后自动。

### 8.2 可观测性

| 层 | 方案 |
|----|------|
| 追踪 | OpenTelemetry（Python SDK）→ 本地 Zipkin（复用短链项目已部署的 9411）；trace 贯穿 解析/检索/生成 各 span |
| 日志 | 结构化 JSON 日志；retrieval_log / qa_trace 表存细粒度阶段耗时 |
| 指标 | token 消耗（按文档/按任务类型）、各阶段 P50/P95、缓存命中率、拒答率 |

### 8.3 MCP Server（对外集成定位）

- 独立进程（stdio + SSE 双传输），暴露：`search_knowledge`、`list_documents`、
  `ask_knowledge`（走完整问答管线）、`document_status`；
- **明确不用于内部 Agent**（内部直调函数，避免协议开销）——定位是
  "任意 MCP Client（IDE Agent 等）可直接接入本知识库"，回答"为什么不直接调函数"；
- Agent Card / A2A：作为可选项放最后（协议完整性加分，非核心）。

---

## 九、模型实验计划（面试素材生产线）

| 实验 | 对照 | 产出 |
|------|------|------|
| E1 Embedding | ritrieve_zh_v1(int8) vs bge-base(int8) vs bge-large(需导出) | Context Recall / MRR 对比表，决定默认模型 |
| E2 Reranker | v2-m3 vs v2-gte(需下载导出) | 同上 |
| E3 思考档位 | complex 问答 开/关 enable_thinking | Faithfulness/延迟/token 三维对比 |
| E4 反思开关 | Reflect 节点 开/关 | 质量 vs 成本 |
| E5 解析通道 | 表格页 通道A(纯直提) vs 通道B(VLM) | 表格维度 Recall 对比 |

每个实验 = 一次 eval_run 对照 + 一页 markdown 报告，沉淀到 `docs/experiments/`。

---

## 十、数据库设计（新库 rag_kb，全新表名）

```sql
-- 文档与解析
CREATE TABLE kb_document (
    id BIGSERIAL PRIMARY KEY,
    filename VARCHAR(500) NOT NULL,
    doc_type VARCHAR(20) NOT NULL,          -- pdf/word/markdown/image
    file_hash CHAR(64) UNIQUE NOT NULL,     -- 解析缓存键
    page_count INT, char_count INT,
    status SMALLINT DEFAULT 0,              -- 0解析中 1已入库 2失败
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE kb_chunk (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT REFERENCES kb_document(id),
    chunk_type VARCHAR(10) NOT NULL,        -- text/table/image
    page_no INT, seq INT,
    content TEXT NOT NULL, chars INT,
    embedding vector(768),
    status SMALLINT DEFAULT 0,              -- 0待向量化 1已向量化
    meta JSONB
);
CREATE INDEX idx_chunk_embedding ON kb_chunk
    USING hnsw (embedding vector_cosine_ops) WHERE status = 1;
CREATE INDEX idx_chunk_doc ON kb_chunk(document_id);

-- 问答与日志
CREATE TABLE qa_session (
    id VARCHAR(50) PRIMARY KEY,
    created_at TIMESTAMP DEFAULT NOW(),
    summary TEXT                             -- 早期轮次摘要
);
CREATE TABLE qa_log (
    id BIGSERIAL PRIMARY KEY,
    session_id VARCHAR(50), trace_id VARCHAR(50),
    query TEXT, answer TEXT,
    route VARCHAR(20),                      -- simple/standard/complex
    chunk_ids BIGINT[],                     -- 引用来源
    total_ms INT, token_in INT, token_out INT, thinking BOOLEAN,
    cache_hit BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE retrieval_log (
    id BIGSERIAL PRIMARY KEY,
    trace_id VARCHAR(50), query TEXT,
    hit_count INT, top_score FLOAT, low_confidence BOOLEAN,
    stage_ms JSONB,                         -- {vector:..,bm25:..,rerank:..}
    created_at TIMESTAMP DEFAULT NOW()
);

-- 反馈闭环
CREATE TABLE feedback (
    id BIGSERIAL PRIMARY KEY,
    qa_log_id BIGINT REFERENCES qa_log(id),
    rating SMALLINT,                        -- 1 赞 / -1 踩
    correction TEXT,                        -- 用户纠错内容
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE bad_case (
    id BIGSERIAL PRIMARY KEY,
    qa_log_id BIGINT, query TEXT,
    attribution VARCHAR(20),                -- retrieval/generation/pending
    status VARCHAR(20) DEFAULT 'open',      -- open/confirmed/in_regression/rejected
    created_at TIMESTAMP DEFAULT NOW()
);

-- 评估体系
CREATE TABLE eval_question (
    id BIGSERIAL PRIMARY KEY,
    question TEXT NOT NULL, reference_answer TEXT,
    source_chunk_ids BIGINT[],
    dimension VARCHAR(20) NOT NULL,         -- factual/table/cross_page/refuse
    in_regression BOOLEAN DEFAULT FALSE
);
CREATE TABLE eval_run (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(100), config JSONB,        -- 运行时的管线配置快照
    metrics JSONB,                          -- 聚合指标
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE eval_result (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT REFERENCES eval_run(id),
    question_id BIGINT REFERENCES eval_question(id),
    scores JSONB, retrieved_chunk_ids BIGINT[],
    answer TEXT
);

-- Prompt 管理与审批
CREATE TABLE prompt_registry (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(50) UNIQUE NOT NULL,       -- route/rewrite/reflect/diagnosis...
    content TEXT NOT NULL, version INT DEFAULT 1,
    status SMALLINT DEFAULT 1,
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE prompt_approval (
    id BIGSERIAL PRIMARY KEY,
    prompt_code VARCHAR(50), old_content TEXT, new_content TEXT,
    eval_compare JSONB,                     -- 回归集新旧对比数据
    decision VARCHAR(20),                   -- pending/approved/rejected
    decided_at TIMESTAMP, created_at TIMESTAMP DEFAULT NOW()
);

-- 记忆 / 诊断报告
CREATE TABLE memory_entry (
    id BIGSERIAL PRIMARY KEY,
    user_id VARCHAR(50) DEFAULT 'default',
    mem_type VARCHAR(20),                   -- focus/open_question/preference
    content TEXT, embedding vector(768),
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE diagnosis_report (
    id BIGSERIAL PRIMARY KEY,
    summary TEXT, metrics JSONB, anomalies JSONB, suggestions JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

-- LangGraph Checkpointer 表由 langgraph-checkpoint-postgres(psycopg3) 自动创建
```

---

## 十一、语料集设计

| # | 类型 | 来源 | 覆盖分支 |
|---|------|------|---------|
| 1-2 | 技术文档 | 开源项目官方中文文档 PDF（文本型，代码块多） | 通道A |
| 3 | 行业白皮书 | 信通院等公开白皮书（大量图表+表格） | 通道B + 图片 |
| 4 | 标准规范 | 公开国家标准/技术规范（表格密集、结构严格） | 通道B |
| 5 | 扫描件 | 自渲染扫描版测试 PDF | 通道C |
| 6 | 独立图片 | 架构图/数据图（自制+白皮书提取） | 图片通道 |

全部为可公开分发内容，规避版权问题。总量控制在 200-500 页，
保证 CPU 环境建库时长可接受（VLM 页预估 <60 页）。

---

## 十二、项目目录结构

```
rag/
├── docs/                          # 方案文档 + 实验报告
│   ├── architecture.md            # 本文档
│   ├── development-plan.md
│   └── experiments/               # E1-E5 实验报告
├── data/
│   ├── corpus/                    # 原始语料（按类型分子目录）
│   └── parsed/                    # 解析产物缓存（按 file_hash）
├── rag-python/
│   ├── src/
│   │   ├── config.py              # 12-factor，读 ../.env
│   │   ├── llm/                   # mimo_client（思考档位封装）/ prompt_loader
│   │   ├── ingest/                # router / pdf_text / pdf_vlm / image_vlm / chunker
│   │   ├── retrieval/             # embedder / bm25_index / hybrid / reranker / rewriter / cache
│   │   ├── agent/                 # state / main_graph / nodes / diagnosis_graph / approval_graph
│   │   ├── memory/                # session + long-term
│   │   ├── feedback/              # collector / attributor
│   │   ├── eval/                  # evaluator / metrics / regression_gate
│   │   ├── observability/         # otel tracing / logging / stage_timer
│   │   ├── mcp/                   # server（独立进程入口）
│   │   ├── db/                    # pg_store(psycopg3 连接池) / migrations
│   │   └── api/                   # rag_api / agent_api / eval_api / feedback_api / diagnosis_api
│   ├── models/                    # ONNX INT8 模型（从本机已有资产复制）
│   ├── tests/
│   └── pyproject.toml
├── rag-java/                      # 薄网关（后期，可裁剪）
└── scripts/                       # init_db.sql / download_corpus.py / export_onnx.py
```

---

## 十三、技术栈总览

| 层 | 选型 | 说明 |
|----|------|------|
| LLM | MiMo mimo-v2.5（唯一） | OpenAI 兼容；思考档位路由；多模态解析 |
| Agent 编排 | LangGraph 1.x + langgraph-checkpoint-postgres | psycopg3，Checkpointer 持久化 |
| 服务框架 | FastAPI + SSE 流式 | |
| 解析 | pymupdf + python-docx + MiMo VLM | 无 MinerU、无 OCR |
| Embedding | ritrieve_zh_v1 / bge 系列 ONNX INT8（E1 定夺） | 本地 CPU |
| Rerank | bge-reranker-v2-m3 ONNX INT8（E2 可换 v2-gte） | 本地 CPU |
| BM25 | jieba + rank_bm25 | 内存索引 |
| 向量库 | PostgreSQL 18 + pgvector HNSW | 库 rag_kb |
| 缓存 | Redis（语义缓存/锁/会话） | |
| 评估 | 自建评估器 + LLM-as-judge（mimo-v2.5） | RAGAS 式指标 |
| 追踪 | OpenTelemetry → 本地 Zipkin(9411) | 复用短链项目设施 |
| Java 层 | Spring Boot 3.x 薄网关 | 鉴权/限流/审计/SSE 转发/管理 API |
| MCP | mcp Python SDK，对外集成定位 | stdio + SSE |
