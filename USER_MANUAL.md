# 智能文档问答系统 — 用户操作说明书

> **版本**：v1.0  
> **适用对象**：首次接触本系统的用户（开发者、面试官、技术评审）  
> **系统地址**：Python 服务 `http://localhost:8090` · Java 网关 `http://localhost:8082`

---

## 目录

1. [系统概览：它能做什么](#1-系统概览它能做什么)
2. [环境与依赖：启动前的准备](#2-环境与依赖启动前的准备)
3. [第一步：初始化数据库](#3-第一步初始化数据库)
4. [第二步：生成语料](#4-第二步生成语料)
5. [第三步：启动 Python 服务](#5-第三步启动-python-服务)
6. [第四步：文档入库（核心流程）](#6-第四步文档入库核心流程)
7. [第五步：问答 — 基础问答管线](#7-第五步问答--基础问答管线)
8. [第六步：问答 — Agentic RAG 主图](#8-第六步问答--agentic-rag-主图)
9. [第七步：评估体系 — 建立标尺](#9-第七步评估体系--建立标尺)
10. [第八步：反馈闭环 — 让系统自我进化](#10-第八步反馈闭环--让系统自我进化)
11. [第九步：Prompt 管理与 HITL 审批](#11-第九步prompt-管理与-hitl-审批)
12. [第十步：诊断与可观测性](#12-第十步诊断与可观测性)
13. [第十一步：Java 网关（鉴权/限流/审计）](#13-第十一步java-网关鉴权限流审计)
14. [第十二步：MCP Server（外部集成）](#14-第十二步mcp-server外部集成)
15. [附录 A：API 速查表](#附录-aapi-速查表)
16. [附录 B：实验体系](#附录-b实验体系)
17. [附录 C：常见问题](#附录-c常见问题)

---

## 1. 系统概览：它能做什么

本系统是一个**端到端的智能文档问答系统**，核心能力：

```
你扔进去一份 PDF / Word / Markdown / 图片
         ↓
系统自动解析（含扫描件 OCR、表格结构化、图片语义描述）
         ↓
切块 → 向量化 → 存入向量数据库
         ↓
你用自然语言提问，系统从文档中检索证据、生成带引用标注的回答
         ↓
答不好？点个踩 → 系统自动归因（检索问题 vs 生成问题）→ 升级回归测试集
         ↓
改 Prompt？必须过回归门禁 + 人工审批才能生效
```

**技术栈一句话**：Python (FastAPI + LangGraph) + Java (Spring Boot 网关) + PostgreSQL (pgvector) + Redis + MiMo API (LLM/VLM) + 本地 ONNX INT8 模型 (Embedding/Reranker)。

---

## 2. 环境与依赖：启动前的准备

### 2.1 硬件要求

| 项目 | 最低要求 | 说明 |
|------|---------|------|
| CPU | 4 核 | ONNX 推理绑 P 核，建议 6+ 核 |
| 内存 | 8 GB | Reranker 模型 569MB + Embedding 313MB 常驻内存 |
| 磁盘 | 5 GB 可用 | 模型文件 + 解析缓存 + 数据库 |
| GPU | **不需要** | 全部本地推理走 CPU ONNX INT8 |

### 2.2 软件依赖

| 软件 | 版本 | 用途 |
|------|------|------|
| Python | 3.13+ | 主服务运行时 |
| JDK | 17+ | Java 网关 |
| Maven | 3.9+ | Java 网关构建 |
| PostgreSQL | 18+ | 业务数据库 + 向量库 |
| Redis | 5.0+ | 语义缓存 |
| pgvector 扩展 | 0.8+ | PostgreSQL 向量检索 |
| pg_trgm 扩展 | — | PostgreSQL 模糊匹配 |

### 2.3 配置文件

在项目根目录 `rag/` 下创建 `.env` 文件：

```env
# 必填：MiMo API 密钥
MIMO_API_KEY=your_api_key_here

# 数据库（默认值已适配本地环境）
PG_DSN=postgresql://postgres:root@localhost:5432/rag_kb

# Redis（默认值已适配本地环境）
REDIS_URL=redis://localhost:6379/0

# 可选：Zipkin 追踪（不配则自动降级为 no-op）
ZIPKIN_ENDPOINT=http://localhost:9411/api/v2/spans
```

> **背后的原理**：系统采用 12-factor 配置原则，所有敏感信息（API Key、数据库密码）通过环境变量注入，不硬编码在代码中。`config.py` 在启动时从 `.env` 加载，任何配置变更只需改 `.env` 后重启服务。

---

## 3. 第一步：初始化数据库

### 操作

```powershell
cd C:\Users\lrs\Desktop\py\rag
python scripts\init_db.py
```

### 它做了什么

1. **创建数据库** `rag_kb`（如不存在）
2. **启用扩展**：`pgvector`（向量检索）+ `pg_trgm`（模糊匹配）
3. **创建全部业务表**：

| 表名 | 职责 |
|------|------|
| `kb_document` | 文档元信息（文件名、类型、hash、状态） |
| `kb_chunk` | 文档分块（内容、向量、类型、页码） |
| `qa_log` | 问答日志（查询、回答、路由档位、耗时、token） |
| `retrieval_log` | 检索日志（命中数、top 分数、各阶段耗时） |
| `feedback` | 用户反馈（赞/踩 + 纠错内容） |
| `bad_case` | 坏例库（含完整链路快照 + 归因结论） |
| `eval_question` | 黄金评估集（问题、参考答案、证据关键词、维度） |
| `eval_run` / `eval_result` | 评估运行记录与逐题结果 |
| `prompt_registry` | Prompt 版本注册表（7 个提示词，支持版本管理） |
| `prompt_approval` | Prompt 变更审批单（含回归对比数据） |
| `memory_entry` | 长期记忆（用户关注主题、未解决疑问） |
| `diagnosis_report` | 诊断报告 |

4. **创建向量索引**：`kb_chunk.embedding` 列上的 HNSW 余弦索引

### 背后的原理

- **双向量列设计**：`kb_chunk` 表有 `embedding`（768 维，bge-base）和 `embedding2`（1792 维，ritrieve）两列，用于 A/B 对照实验。查询时通过 `VECTOR_COLUMN` 配置自动选择对应列，避免维度不匹配。
- **HNSW 索引**：相比 IVF-flat，HNSW 在小规模数据（<10 万条）上检索更快，无需训练，适合本项目 420 块的规模。
- **file_hash 唯一约束**：文档按 SHA-256 hash 幂等——同一文件重复上传不会产生重复记录，这是成本控制的基础。

---

## 4. 第二步：生成语料

### 操作

```powershell
python scripts\gen_corpus.py
```

### 它做了什么

在 `data/corpus/` 下生成自造语料文件：

| 文件 | 类型 | 覆盖的解析通道 |
|------|------|--------------|
| 白皮书 PDF | 文本型 PDF，含大量图表和表格 | 通道 A（文本直提）+ 通道 B（VLM 表格） |
| 技术规范 PDF | 表格密集型文档 | 通道 B（VLM 表格结构化） |
| 扫描通知 PDF | 无文本层的扫描件 | 通道 C（VLM 整页转录） |
| 架构图 / 数据图 | 独立图片文件 | 图片通道（VLM 语义描述） |
| FastAPI / MinerU README | Markdown 技术文档 | MD 直读通道 |

### 背后的原理

- **为什么要自造语料**：独立开发拿不到企业真实文档，但评估体系必须有"真题"才能跑。自造语料的每个细节（数字、日期、表格结构）都是可控的，便于构造精确的评估集。
- **四种文档覆盖四条解析通道**：这是刻意设计——面试时可以展示"95% 页面零成本直提，只有表格和扫描件才调 VLM"的成本优势，需要有对应语料来证明。

---

## 5. 第三步：启动 Python 服务

### 操作

```powershell
cd C:\Users\lrs\Desktop\py\rag\rag-python\src
python -m uvicorn api.app:app --port 8090
```

> ⚠️ **必须从 `rag-python\src` 目录启动**，因为代码中的 import 路径是相对 `src` 的。

### 它做了什么

启动 FastAPI 服务，监听 `8090` 端口。启动过程中自动：

1. **加载配置**：从 `rag/.env` 读取环境变量
2. **初始化日志**：控制台 INFO + 文件 DEBUG JSON（`rag-python/logs/`）
3. **初始化 OTEL 追踪**：探测 Zipkin 是否可达，不可达则降级 no-op
4. **挂载 7 个路由模块**：`/api/ingest`、`/api/rag`、`/api/agent`、`/api/eval`、`/api/feedback`、`/api/admin`、`/api/diagnosis`

### 验证服务启动

```powershell
curl http://localhost:8090/health
```

返回 `{"service": "rag-doc-qa", "checks": {"service": "up", "postgres": "up", "redis": "up"}}` 即成功。

### 背后的原理

- **health 端点探活三层**：不仅检查服务自身 up，还探测 PostgreSQL 和 Redis 连通性。任何一层 down 都返回 503——这是企业级健康检查的标准做法。
- **OTEL 降级策略**：Zipkin 不可达时不会阻塞服务启动，而是静默降级为 no-op tracer。这避免了"忘记启动 Zipkin 导致整个服务起不来"的运维噩梦。

---

## 6. 第四步：文档入库（核心流程）

这是整个系统最核心的流程——把文档变成可检索的知识。

### 6.1 操作

**方式一：按路径入库（推荐，开发/运维用）**

```powershell
# 入库单个文件
curl -X POST http://localhost:8090/api/ingest/ingest-path \
  -H "Content-Type: application/json" \
  -d "{\"path\": \"C:/Users/lrs/Desktop/py/rag/data/corpus\"}"
```

**方式二：上传文件入库（生产用）**

```powershell
curl -X POST http://localhost:8090/api/ingest/upload \
  -F "file=@C:\path\to\your\document.pdf"
```

**方式三：替换已有文档**

```powershell
curl -X POST "http://localhost:8090/api/ingest/upload?replace=true" \
  -F "file=@updated_version.pdf"
```

### 6.2 查看入库状态

```powershell
# 查看所有文档
curl http://localhost:8090/api/ingest/documents

# 查看单个文档详情（含分块数）
curl http://localhost:8090/api/ingest/status/1

# 抽查文档分块（解析质量审查）
curl http://localhost:8090/api/ingest/documents/1/chunks?limit=10
```

### 6.3 它背后发生了什么（核心原理）

一个文件从上传到可检索，经历 **5 个阶段**：

```
文件 → [1] 幂等检查 → [2] 解析路由 → [3] 切块 → [4] 向量化 → [5] 入库
```

#### [1] 幂等检查（零成本去重）

系统先算文件的 SHA-256 hash，查数据库是否已有同 hash 记录。如果有，直接返回已有文档 ID，不重复处理。

**优化思路**：这是成本控制的第一道防线。VLM 调用（MiMo API）是有费用的，同一文件反复上传不应重复调 API。

#### [2] 解析路由（按页决策，核心设计）

这是本系统最精巧的设计之一。系统**逐页分析**每一页该走哪条通道：

```
PDF 第 N 页
  ├─ 提取文本层字符数 / 页面面积 → 计算"文本密度"
  │
  ├─ 密度 ≥ 50 字 且 无表格特征 → 通道 A：pymupdf 文本直提（零 API 成本）
  │
  ├─ 密度 ≥ 50 字 且 有表格特征 → 通道 B：文本直提 + 表格区域裁剪
  │     └→ 表格图片发给 MiMo-V2.5 → 返回结构化 markdown 表格
  │
  └─ 密度 < 50 字（扫描件）      → 通道 C：整页渲染为 PNG
        └→ PNG 发给 MiMo-V2.5 → 返回转录文本
```

**表格特征怎么检测**：系统用 pymupdf 提取页面中的矢量线条（`page.get_drawings()`），统计横线和竖线的数量。如果横线 ≥ 2 且竖线 ≥ 2，认为存在表格结构，算出这些线条的包围盒作为"表格区域"。

**图片处理**：对于页面中的大图（占页面积 > 15%），单独裁剪出来发给 VLM 做语义描述（"这张图展示了..."）+ 文字抽取。

**优化思路**：
- 95% 的普通 PDF 页面走通道 A（零成本），只有表格和扫描件才调 VLM——这是全量 VLM 解析成本的 **1/10**
- VLM 调用结果按图片内容 hash 落盘缓存（`data/parsed/vlm_cache/`），同一文档重复入库**零 API 成本**
- 每页记录 `parse_channel`（A/B/C），供评估时分维度统计——可以精确量化"表格页的问答质量是否因为 VLM 结构化而提升"

**对于 Word / Markdown / 图片文件**：
- Word：用 python-docx 逐段落 + 逐表格提取，每 20 个元素视为一"页"
- Markdown：按一级/二级标题切 section，每个 section 视为一"页"
- 独立图片：直接走 VLM（语义描述 + 文字抽取，一次调用同时输出两部分）

#### [3] 切块（段落策略 + 上下文注入）

解析得到的"页级中间表示"被送入切块器：

**文本块**：
- 段落优先，≤ 500 字的段落整块保留
- > 500 字的段落按句子边界（。！？等标点）二分
- 相邻块之间有 60 字的 **overlap**（重叠），防止跨块语义断裂

**表格块**：
- 整表作为一个 chunk（markdown 格式）
- 超长表格（> 800 字）按行组拆分，每组保留表头行

**图片块**：语义描述 + 图中文字合并为一个 chunk

**上下文注入（Contextual Retrieval）**：每个 chunk 头部自动注入一行：
```
《文档标题》 p.3：
```
这个看似简单的操作对检索质量帮助巨大——当用户问"白皮书第几页提到了部署率"，chunk 自带的页码信息让 embedding 模型能更好地理解上下文。

**优化思路**：
- overlap 60 字是经验值：太少则跨块问题答不好，太多则浪费向量空间
- 上下文注入的灵感来自 Anthropic 的 Contextual Retrieval 论文，零成本但对跨页问题效果显著
- 表格保留表头是为了让 chunk 自包含——拆分后的表格片段如果不带表头，语义完全丢失

#### [4] 向量化（本地 ONNX INT8 推理）

每个 chunk 的文本被送入本地 Embedding 模型（`bge-base-zh-v1.5`，768 维，ONNX INT8 格式）：

- **批量编码**：32 条一批，生产者-消费者流水线（预分词线程 + 推理线程并行）
- **mean pooling**：取所有 token embedding 的均值作为句子向量
- **L2 归一化**：向量归一化到单位球面，余弦相似度退化为内积，检索更快

**优化思路**：
- ONNX INT8 量化：模型从 FP32 压缩到 INT8，体积减小 75%，推理速度提升 2-3 倍，精度损失 < 1%
- 纯 CPU 推理：不依赖 GPU，在 i5-12500H 上 420 块向量化约 30 秒
- 线程绑定 P 核（`OMP_NUM_THREADS=8`）：i5-12500H 有 6P+8E 核，绑定 Performance 核避免 E 核拖慢推理

#### [5] 入库 + 后处理

向量写入 `kb_chunk` 表（含 `embedding` 列），同时：
- **BM25 索引重建**：标记版本号，下次查询时懒加载重建（jieba 分词 + BM25Okapi）
- **语义缓存失效**：知识库内容变了，旧的缓存答案作废

---

## 7. 第五步：问答 — 基础问答管线

### 操作

```powershell
# 同步问答
curl -X POST http://localhost:8090/api/rag/ask \
  -H "Content-Type: application/json" \
  -d "{\"query\": \"白皮书里调研了多少家企业？\"}"

# 流式问答（SSE）
curl -X POST http://localhost:8090/api/rag/ask \
  -H "Content-Type: application/json" \
  -d "{\"query\": \"白皮书里调研了多少家企业？\", \"stream\": true}"
```

### 返回格式

```json
{
  "answer": "根据《企业智能文档管理白皮书（2026年）》，调研覆盖了 1283 家企业 [1]。",
  "citations": [
    {"index": 1, "chunk_id": 42, "doc_name": "白皮书.pdf", "page_no": 3, "score": 0.8921}
  ],
  "low_confidence": false,
  "refused": false,
  "trace_id": "a1b2c3d4e5f6",
  "session_id": "s-12345678",
  "total_ms": 3200
}
```

### 它背后发生了什么

基础问答管线（`qa_service.py`）的完整流程：

```
用户问题
  ↓
[1] 语义缓存检查 → 命中？直接返回历史答案（零 LLM 成本）
  ↓ 未命中
[2] 混合检索 → [3] 生成回答 → [4] 日志落库 → [5] 缓存存储
```

#### [1] 语义缓存（Redis）

系统先把问题编码为向量，与 Redis 中缓存的所有历史问题做余弦比较。如果相似度 ≥ 0.95（几乎相同的问题），直接返回历史答案。

**优化思路**：
- 阈值 0.95 非常高——只拦截几乎一模一样的问题，防止"语义漂移"导致错误命中
- 缓存上限 500 条，满员时淘汰最旧的 20%（LRU 近似），比"满 500 全清"更平滑
- 知识库内容变化（入库/删除文档）时自动清空缓存——旧答案可能已不准确

#### [2] 混合检索管线

这是检索质量的核心，由三路信号融合：

```
查询 → 向量编码 → [向量路] pgvector HNSW 检索 top24
                  → [BM25路] jieba 分词 + BM25Okapi 检索 top24
                  → [融合] RRF (k=60) 融合两路排序
                  → [精排] bge-reranker-v2-m3 精排 top-K
                  → [阈值] score < -5.0 剔除 / < 0.0 标记低置信
                  → 返回 top8
```

**向量路**：把问题编码为 768 维向量，在 pgvector HNSW 索引上做余弦近邻搜索。擅长语义匹配（"部署率"能匹配到"部署比例"）。

**BM25 路**：jieba 分词后用 BM25Okapi 算关键词匹配分数。擅长精确匹配（"1283 家"这种精确数字）。

**RRF 融合**：Reciprocal Rank Fusion，公式为 `score(doc) = Σ 1/(k + rank_i)`。两路排序的分数量纲不同（向量余弦 0~1，BM25 分数无界），RRF 把它们统一到同一尺度。k=60 是论文推荐值。

**Reranker 精排**：用 `bge-reranker-v2-m3`（CrossEncoder，ONNX INT8）对候选集做 pairwise 打分。CrossEncoder 比 BiEncoder（Embedding）精度高得多，因为 query 和 passage 可以做 token 级交互。但速度慢，所以只对 RRF 筛出的候选集做精排。

**优化思路**：
- 实验 E2 证明精排是检索质量**最大的单点**——关掉 reranker 后 MRR 从 0.9167 暴跌到 0.3998
- Reranker 信号量限 1 并发（CPU 推理饱和单核以上收益递减），排队超时 15 秒自动降级为 RRF 排序——宁可精度低一点也不能阻塞问答
- 降级时标记 `low_confidence=True`，生成层据此强化拒答约束——诚实标记比假装信心满满更安全

#### [3] 生成回答

检索到的 top8 chunk 被格式化为"参考资料"注入 Prompt：

```
[1] (白皮书.pdf p.3)
《企业智能文档管理白皮书（2026年）》 p.3：
蓝图数字研究院于 2026 年 3 月 18 日发布...

[2] (技术规范.pdf p.12)
...
```

Prompt 要求 LLM：
1. 只用参考资料，不编造
2. 涉及数字/日期必须与资料完全一致
3. 在关键句末尾用 `[n]` 标注来源序号

**低置信信号注入**：如果检索降级（rerank 超时），Prompt 中会额外注入一段提示："以下参考资料与问题相关度较低，若证据不足必须明确说'未找到'，不得编造。"——这是防止幻觉的最后防线。

#### [4] 记忆系统

每次问答后，系统检查当前 session 的轮次。每 5 轮自动触发一次"长期记忆提炼"——LLM 从对话历史中抽取用户持续关注的主题和未解决的疑问，存入 `memory_entry` 表（带向量索引）。

下次问答时，系统会语义召回 top3 相关记忆注入 Prompt："该用户此前关注白皮书中的部署率数据"——实现跨会话的个性化。

---

## 8. 第六步：问答 — Agentic RAG 主图

### 操作

```powershell
# Agent 模式问答（走完整 LangGraph 主图）
curl -X POST http://localhost:8090/api/agent/run \
  -H "Content-Type: application/json" \
  -d "{\"query\": \"对比制造业和金融业的文档管理系统部署率，分析差异原因\"}"
```

### 它背后发生了什么（LangGraph 主图）

基础问答管线是"一把梭"——检索 → 生成。Agent 主图更智能，会根据问题复杂度**自适应选择处理路径**：

```
                    ┌──────────┐
                    │  START   │
                    └────┬─────┘
                    ┌────▼─────┐
                    │  Route   │ 复杂度/类型路由
                    └────┬─────┘
           ┌─────────────┼─────────────┐
      simple          standard       complex         out_of_scope
      (直答)          (标准)         (复杂)           (超范围)
        │               │              │                  │
        │          ┌────▼────┐    ┌────▼─────┐       ┌────▼────┐
        │          │Retrieve │    │Decompose │       │ Guard   │→ "超出知识库范围"
        │          └────┬────┘    └────┬─────┘       └─────────┘
        │               │         ┌────▼─────┐
        │               │         │Retrieve  │×N 子查询并行
        │               │         └────┬─────┘
        │               ├──────────────┤
        │          ┌────▼─────┐
        │          │ GradeCtx │ 检索质量评估
        │          └────┬─────┘
        │        合格↓      ↓不合格（≤2次重试）
        │               ┌────▼──────┐
        │               │ Rewrite + │──→ 回到 Retrieve
        │               │ ReRetrieve│
        │               └────┬──────┘
        │                    │ 仍不合格
        │               ┌────▼──────┐
        │               │ NoAnswer  │ "文档中未找到"
        │               └───────────┘
        │          ┌────▼─────┐
        └─────────▶│ Generate │
                   └────┬─────┘
                   ┌────▼─────┐
                   │ Reflect  │ 答案自检（仅 complex）
                   └────┬─────┘
                  通过↓    ↓不通过(≤1次) → 修正重生成
                   ┌────▼─────┐
                   │   END    │ 带引用标注输出
                   └──────────┘
```

#### Route 节点（复杂度路由）

用轻量 LLM 调用（关闭思考模式，省 token）判断问题属于哪一档：
- **simple**：简单事实查询（"白皮书的发布日期是？"）→ 直接检索 + 生成，不开思考
- **standard**：常规问题（"白皮书的核心发现是什么？"）→ 标准流程
- **complex**：复杂问题（"对比 A 和 B 的差异"）→ 拆解 + 多路检索 + 反思
- **out_of_scope**：与知识库无关（"今天天气怎么样？"）→ 直接拒答

**优化思路**：路由节点在做分类前，会先做一次**轻量检索**（top1，无精排），把命中的片段作为"先验"注入路由 prompt。这让路由能判断"这个问题在知识库里有没有相关内容"，大幅减少误拒（route 误把知识库内问题判为 out_of_scope 是早期的重大 bug）。

#### Decompose 节点（问题拆解）

complex 问题被拆成 2-3 个可独立检索的子问题。例如：
- 原问题："对比制造业和金融业的部署率差异"
- 子问题 1："制造业的文档管理系统部署率是多少？"
- 子问题 2："金融业的文档管理系统部署率是多少？"

**优化思路**：子查询检索**并行执行**（ThreadPoolExecutor），粗筛阶段不走精排（避免 reranker 信号量排队雪崩），合并候选后以原问题做**单次精排**——从 N 次 rerank 降为 1 次，省 CPU。

#### GradeCtx 节点（CRAG 式检索评估）

检索完成后，用 LLM 评估"检索到的资料能否支撑回答"。如果不够，触发查询改写 + 重新检索，最多重试 2 次。

**优化思路**：这是 CRAG（Corrective RAG）的核心思想——在生成之前就判断检索质量，比"生成完了再反思"便宜得多。反思要消耗一整轮生成 token，而检索评估只需要一次轻量 LLM 调用。

#### Generate 节点（思考档位路由）

- simple 档：关闭思考模式（`enable_thinking=False`），省 token，响应更快
- standard/complex 档：开启思考模式，LLM 先推理再回答，忠实度更高

**优化思路**：实验 E3 证明思考模式对忠实度有显著提升（faith 从 0.825 提升到 0.99），但延迟和 token 成本也更高。按档位路由是成本和质量的最佳平衡点。

#### Reflect 节点（答案自检）

仅 complex 档触发。LLM 检查答案的忠实度（是否编造）和相关性（是否切题），两项均 ≥ 0.6 才通过。不通过则修正重生成，最多 1 次。

**优化思路**：实验 E4 证明反思对复杂题至关重要——关掉反思后 cross_page 维度的 faith 从 0.87 下降到 0.757。但反思有成本，所以只在 complex 档开启，且最多重试 1 次。

#### 拒答能力

两次重检索仍不合格 → 明确回答"根据现有文档未找到相关信息"，不编造。这是企业级 RAG 的标志——面试高频追问点。

---

## 9. 第七步：评估体系 — 建立标尺

### 9.1 灌入评估集

```powershell
curl -X POST http://localhost:8090/api/eval/seed
```

这把 `eval/questions.json` 中的 40 道题灌入 `eval_question` 表。每道题包含：

| 字段 | 说明 |
|------|------|
| `question` | 问题文本 |
| `reference_answer` | 参考答案 |
| `dimension` | 维度标签：factual / table / cross_page / refuse |
| `evidence_keywords` | 证据关键词（用于命中判定） |
| `regression` | 是否纳入回归子集 |

**40 题分布**：factual 17 题 / table 8 题 / cross_page 7 题 / refuse 8 题。

### 9.2 跑评估

```powershell
# 基础问答管线评估
curl -X POST http://localhost:8090/api/eval/run \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"baseline-run\", \"engine\": \"baseline\"}"

# Agent 主图评估
curl -X POST http://localhost:8090/api/eval/run \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"agent-run\", \"engine\": \"agent\"}"

# 带 LLM-as-judge 的评估（消耗 API，更精确）
curl -X POST http://localhost:8090/api/eval/run \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"judge-run\", \"with_judge\": true}"

# 只跑回归子集（Prompt 审批用）
curl -X POST http://localhost:8090/api/eval/run \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"regression\", \"regression_only\": true}"
```

### 9.3 查看结果

```powershell
# 查看所有评估运行
curl http://localhost:8090/api/eval/runs

# 查看某次运行的逐题结果
curl http://localhost:8090/api/eval/runs/1/results

# 对比两次运行
curl "http://localhost:8090/api/eval/compare?run_a=1&run_b=2"
```

### 9.4 它背后发生了什么

评估器（`evaluator.py`）对每道题执行：

```
问题 → 混合检索 top-K → 检查证据关键词命中 → 计算指标
```

**核心指标**：

| 指标 | 含义 | 计算方式 |
|------|------|---------|
| Context Recall@K | 答案来源是否进入 top-K | 证据关键词全包含匹配 |
| MRR | 首个命中 chunk 的排名倒数均值 | 1/rank |
| Refuse Accuracy | 该拒答的题是否正确拒答 | 答案中含拒答标记词 |
| Faithfulness | 答案是否忠于检索内容 | LLM-as-judge（可选） |
| Answer Relevancy | 答案是否切题 | LLM-as-judge（可选） |

**证据关键词锚定**：不依赖 chunk_id（会随重入库漂移），而是检查 chunk 内容是否包含全部证据关键词。这保证了"删了重新入库"后评估结果仍然可复现。

**并发执行**：40 题用 ThreadPoolExecutor(4) 并发跑，耗时从 ~80 分钟缩短到 ~20 分钟。评估专用图（`get_eval_graph()`）不带 Checkpointer，线程安全。

---

## 10. 第八步：反馈闭环 — 让系统自我进化

### 10.1 提交反馈

```powershell
# 踩（不满意）
curl -X POST http://localhost:8090/api/feedback \
  -H "Content-Type: application/json" \
  -d "{\"qa_log_id\": 1, \"rating\": -1, \"correction\": \"正确答案应该是 1283 家企业\"}"

# 赞（满意）
curl -X POST http://localhost:8090/api/feedback \
  -H "Content-Type: application/json" \
  -d "{\"qa_log_id\": 2, \"rating\": 1}"
```

### 10.2 查看坏例

```powershell
# 查看所有坏例
curl http://localhost:8090/api/feedback/bad-cases

# 按状态过滤
curl "http://localhost:8090/api/feedback/bad-cases?status=open"
```

### 10.3 手动触发归因

```powershell
curl -X POST http://localhost:8090/api/feedback/bad-cases/1/attribute
```

### 10.4 确认坏例 → 升级回归集

```powershell
curl -X POST http://localhost:8090/api/feedback/bad-cases/1/confirm
```

### 10.5 它背后发生了什么

```
用户点踩 → 自动快照（query + answer + 检索结果）
  → 自动归因（规则预判 + LLM 复核）：
      检索结果中没有能回答问题的内容 → attribution = "retrieval"
      检索结果中有答案但答错了       → attribution = "generation"
  → 人工确认 → 升级为回归集题目
  → 回归集 = Prompt 变更的强制门禁
```

**优化思路**：
- 归因逻辑是"规则 + LLM"双保险：LLM 失败时规则兜底（有检索结果 → generation，无 → retrieval）
- 升级回归集后，每次 Prompt 变更都必须在回归集上跑新旧对比——这就是"反馈驱动进化"的闭环
- 用户纠错内容直接作为回归集的参考答案，省去人工标注成本

---

## 11. 第九步：Prompt 管理与 HITL 审批

### 11.1 查看当前 Prompt

```powershell
# 查看所有 Prompt
curl http://localhost:8090/api/admin/prompts

# 查看单个 Prompt 内容
curl http://localhost:8090/api/admin/prompts/generate
```

系统内置 7 个 Prompt：

| code | 用途 |
|------|------|
| `generate` | 答案生成（注入参考资料 + 引用标注要求） |
| `route` | 复杂度路由分类 |
| `rewrite` | 查询改写（CRAG 纠错用） |
| `decompose` | 复杂问题拆解为子问题 |
| `grade` | 检索质量评估 |
| `reflect` | 答案自检（忠实度 + 相关性） |
| `diagnosis` | 诊断报告生成 |

### 11.2 提交 Prompt 变更

```powershell
curl -X POST http://localhost:8090/api/admin/prompts/generate/change \
  -H "Content-Type: application/json" \
  -d "{\"new_content\": \"你是文档问答专家。请严格依据参考资料回答...\"}"
```

### 11.3 查看审批单

```powershell
curl http://localhost:8090/api/admin/approvals
curl http://localhost:8090/api/admin/approvals/1
```

### 11.4 审批决策

```powershell
# 批准
curl -X POST http://localhost:8090/api/admin/approvals/1/resume \
  -H "Content-Type: application/json" \
  -d "{\"decision\": \"approved\"}"

# 拒绝
curl -X POST http://localhost:8090/api/admin/approvals/1/resume \
  -H "Content-Type: application/json" \
  -d "{\"decision\": \"rejected\"}"
```

### 11.5 它背后发生了什么（HITL 审批图）

```
提交变更 → 自动回归对比（新旧 Prompt 各跑一遍回归集）
  → 生成对比报告（指标 delta + 回归门禁预判）
  → interrupt 暂停 → 等待人工决策
  → approved: Prompt 升版生效 + 刷新内存缓存
  → rejected: 记录拒绝，不生效
```

**关键设计**：审批不是走形式。`interrupt` 携带的是**回归集上新旧 Prompt 的量化对比数据**——审批人看到的是"新 Prompt 让 faithfulness 提升了 3%，但 refuse_accuracy 下降了 2%"，凭数据决策。

**回归门禁**（`gate.py`）：机器预判——如果任何指标环比下降超过阈值（recall -5pp / MRR -10pp / refuse -10pp），门禁标记"不通过"。但这是 **advisory gate**（建议性门禁），人工可以 override——因为回归集样本量小（~15 题），阈值必须从宽。

**LangGraph Checkpointer**：审批图的状态由 PostgreSQL 持久化（`PostgresSaver`）。`interrupt` 暂停后，即使服务重启，状态也不会丢失——`resume` 用 `Command(resume=...)` 从中断点恢复。

---

## 12. 第十步：诊断与可观测性

### 12.1 触发诊断

```powershell
curl -X POST http://localhost:8090/api/diagnosis/trigger
```

### 12.2 查看诊断报告

```powershell
# 最新报告
curl http://localhost:8090/api/diagnosis/latest

# 历史报告
curl http://localhost:8090/api/diagnosis/history

# 指标快照（不调 LLM，不落库）
curl http://localhost:8090/api/diagnosis/metrics
```

### 12.3 它背后发生了什么

诊断 Agent 采集以下指标：

| 类别 | 指标 |
|------|------|
| 问答统计 | 总量、按路由档位分布、拒答率 |
| Token 消耗 | 总输入/输出 token |
| 检索质量 | 低置信比例、空结果率 |
| 阶段耗时 | 各阶段 P50/P95（encode / vector / bm25 / rerank / generate） |
| 反馈分布 | 赞/踩数量、bad case 归因分布 |
| 缓存命中 | 语义缓存命中率 |
| 评估运行 | 最近 5 次评估的指标 |

这些指标交给 LLM 分析，生成诊断报告（summary + anomalies + suggestions）。

**冷启动解法**：独立开发没有真实用户流量，但评估集运行本身就是数据源——每次 `eval_run` 的数据即可供诊断，不依赖真实用户。

### 12.4 可观测性

系统通过 OpenTelemetry 自动记录全链路追踪：
- 每次问答生成一个 `trace_id`，贯穿 解析 → 检索 → 生成 各阶段
- 各阶段耗时记录在 `retrieval_log.stage_ms`（JSON 字段）
- 如果 Zipkin 在运行（`localhost:9411`），trace 会自动导出到 Zipkin UI

---

## 13. 第十一步：Java 网关（鉴权/限流/审计）

### 13.1 构建并启动

```powershell
cd C:\Users\lrs\Desktop\py\rag\rag-java
mvn clean package -DskipTests
java -jar target\rag-gateway-0.1.0.jar
```

网关监听 `8082` 端口，自动连接 PostgreSQL 和 Python 服务（`localhost:8090`）。

### 13.2 注册 / 登录

```powershell
# 注册
curl -X POST http://localhost:8082/api/auth/register \
  -H "Content-Type: application/json" \
  -d "{\"username\": \"user1\", \"password\": \"pass123\"}"

# 登录（获取 JWT Token）
curl -X POST http://localhost:8082/api/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\": \"user1\", \"password\": \"pass123\"}"
# 返回: {"token": "eyJhbG...", "username": "user1", "role": "user"}
```

### 13.3 通过网关问答

```powershell
# 普通问答
curl -X POST http://localhost:8082/api/chat/ask \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"query\": \"白皮书的核心发现是什么？\"}"

# 流式问答（SSE）
curl -X POST http://localhost:8082/api/chat/ask-stream \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"query\": \"白皮书的核心发现是什么？\"}"

# 查看历史
curl http://localhost:8082/api/chat/history/SESSION_ID \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### 13.4 它背后发生了什么

Java 网关是"薄层"——不包含任何业务逻辑，只做四件事：

| 职责 | 实现 |
|------|------|
| **鉴权** | JWT Token 校验（HS256，8 小时过期） |
| **限流** | 滑动窗口（每用户每分钟 20 次），超限返回 429 |
| **审计** | 每次请求记录（用户、操作、查询、状态码、耗时），写入数据库 |
| **SSE 透传** | 流式问答时，Python 侧的 SSE 事件原样透传给客户端 |

**优化思路**：
- 网关不重复实现任何 Python 侧已有的能力（检索、生成、评估），只做"接入层该做的事"
- 限流是进程内滑动窗口（单机演示够用），生产环境应换 Redis 令牌桶
- SSE 透传用 Spring WebFlux 的 `Flux<ServerSentEvent>`，5 分钟超时

---

## 14. 第十二步：MCP Server（外部集成）

### 操作

```powershell
# stdio 模式（IDE Agent 集成用）
cd C:\Users\lrs\Desktop\py\rag\rag-python\src
python -m mcp_server.server

# SSE 模式（网络访问用，端口 8091）
python -m mcp_server.server --sse
```

### 它提供了什么

MCP Server 暴露 4 个工具：

| 工具 | 功能 |
|------|------|
| `search_knowledge` | 在知识库中检索相关内容片段 |
| `ask_knowledge` | 向知识库提问，走完整问答管线 |
| `list_documents` | 列出所有已入库文档 |
| `document_status` | 查询单个文档的状态 |

**使用场景**：任何兼容 MCP 协议的 Client（如 Cursor、Claude Desktop 等 IDE Agent）都可以直接接入本知识库，无需额外适配。

**设计决策**：项目内部的 Agent（LangGraph 主图）直接调用 Python 函数，不走 MCP——MCP 的价值是"对外集成"，内部调用走协议是不必要的开销。

---

## 附录 A：API 速查表

### Python 服务（:8090）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查（含 PG/Redis 探活） |
| POST | `/api/ingest/upload` | 上传文件入库 |
| POST | `/api/ingest/ingest-path` | 按本机路径入库 |
| GET | `/api/ingest/documents` | 文档列表 |
| GET | `/api/ingest/status/{doc_id}` | 文档状态 |
| GET | `/api/ingest/documents/{doc_id}/chunks` | 文档分块抽查 |
| DELETE | `/api/ingest/documents/{doc_id}` | 删除文档（软删） |
| POST | `/api/rag/ask` | 基础问答（同步/SSE） |
| GET | `/api/rag/history/{session_id}` | 会话历史 |
| POST | `/api/agent/run` | Agent 主图问答 |
| POST | `/api/agent/experiment` | 实验开关（E3/E4） |
| POST | `/api/eval/seed` | 灌入评估集 |
| POST | `/api/eval/run` | 跑评估 |
| GET | `/api/eval/runs` | 评估运行列表 |
| GET | `/api/eval/compare` | 对比两次评估 |
| POST | `/api/feedback` | 提交反馈 |
| GET | `/api/feedback/bad-cases` | 坏例列表 |
| POST | `/api/feedback/bad-cases/{id}/attribute` | 触发归因 |
| POST | `/api/feedback/bad-cases/{id}/confirm` | 确认坏例→升级回归集 |
| GET | `/api/admin/prompts` | Prompt 列表 |
| GET | `/api/admin/prompts/{code}` | 查看 Prompt |
| POST | `/api/admin/prompts/{code}/change` | 提交 Prompt 变更 |
| GET | `/api/admin/approvals` | 审批单列表 |
| POST | `/api/admin/approvals/{id}/resume` | 审批决策 |
| POST | `/api/diagnosis/trigger` | 触发诊断 |
| GET | `/api/diagnosis/latest` | 最新诊断报告 |
| GET | `/api/diagnosis/metrics` | 指标快照 |

### Java 网关（:8082）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/register` | 注册 |
| POST | `/api/auth/login` | 登录（返回 JWT） |
| POST | `/api/chat/ask` | 问答（需 JWT） |
| POST | `/api/chat/ask-stream` | 流式问答（SSE，需 JWT） |
| GET | `/api/chat/history/{sessionId}` | 会话历史（需 JWT） |

---

## 附录 B：实验体系

系统内置 5 组对照实验，用于量化每个设计决策的价值：

| 实验 | 变量 | 核心结论 |
|------|------|---------|
| **E1** Embedding 选型 | bge-base(768维) vs ritrieve(1792维) | 持平，默认 bge-base（维度更低 = 存储更省） |
| **E2** Reranker 价值 | 精排开 vs 精排关 | **MRR 从 0.9167 暴跌到 0.3998**——精排是最大单点 |
| **E3** 思考档位 | enable_thinking 开 vs 关 | 思考提升忠实度（faith 0.825→0.99），但延迟更高 |
| **E4** 反思开关 | Reflect 开 vs 关 | 反思对复杂题关键（cross_page faith 0.757→0.87） |
| **E5** VLM 块价值 | 排除 table/image 块 vs 全部 | 当前语料下 VLM 块非必需（语料太小时差异不显著） |

运行实验：

```powershell
cd C:\Users\lrs\Desktop\py\rag
python scripts\run_experiments.py e1
python scripts\run_experiments.py e2
python scripts\run_experiments.py agent-base
python scripts\run_experiments.py e3-off
python scripts\run_experiments.py e4-off
python scripts\run_experiments.py e5
```

---

## 附录 C：常见问题

### Q: 服务启动后 health 返回 503？

检查 PostgreSQL 和 Redis 是否在运行：
```powershell
# 检查 PostgreSQL
pg_isready -h localhost -p 5432

# 检查 Redis
redis-cli ping
```

### Q: 文档入库失败？

检查 `.env` 中的 `MIMO_API_KEY` 是否正确。VLM 调用（扫描件/表格/图片）需要有效的 API Key。

### Q: 问答返回"根据现有文档未找到相关信息"？

1. 确认文档已入库成功（`GET /api/ingest/documents` 查看 status=1）
2. 检查问题是否与文档内容相关（系统会正确拒答知识库外的问题）
3. 如果是知识库内的问题仍被拒答，可能是检索降级（rerank 超时），查看 `retrieval_log`

### Q: 如何添加自己的文档？

```powershell
# 上传单个文件
curl -X POST http://localhost:8090/api/ingest/upload -F "file=@your_doc.pdf"

# 批量入库目录
curl -X POST http://localhost:8090/api/ingest/ingest-path \
  -H "Content-Type: application/json" \
  -d "{\"path\": \"C:/path/to/your/docs\"}"
```

支持格式：PDF、Word (.docx)、Markdown (.md)、图片 (.png/.jpg/.jpeg/.webp)。

### Q: 如何查看某次问答的详细链路？

每次问答返回 `trace_id`，可以通过以下方式追踪：
```powershell
# 查看检索日志
# （直接查数据库，暂无独立 API）
psql -d rag_kb -c "SELECT * FROM retrieval_log WHERE trace_id='YOUR_TRACE_ID'"

# 查看问答日志
psql -d rag_kb -c "SELECT * FROM qa_log WHERE trace_id='YOUR_TRACE_ID'"
```

### Q: 评估跑得太慢？

评估默认 4 并发 worker，40 题约 20 分钟。如果 MiMo API 配额充足，可以增大并发：
```powershell
# 通过 API 无法直接设置并发，但可以通过 run_experiments.py 脚本
# 修改 evaluator.py 中的 concurrency 参数
```

### Q: Java 网关启动失败？

1. 确认 8082 端口未被占用
2. 确认 PostgreSQL 在运行（网关需要连接数据库做用户管理和审计）
3. 确认 Python 服务在 8090 端口运行（网关转发请求到 Python）

---

> **最后提醒**：本系统是一个**持续运营的生产系统**，不是一个跑完 demo 就丢的玩具。反馈闭环、评估体系、HITL 审批——这些机制的价值在于它们**可以持续运转**。即使只有你一个人在用，每次点踩都是在训练系统变得更好。
