# 智能文档问答系统 — 开发计划

> 版本：v2.0（全面修订版）
> 日期：2026-08-17
> 原则变化：不设工期压力，质量优先；评估标尺前置（Phase 2 即建成）；
> 每阶段都有可端口级验证的产出。

---

## 开发原则

1. **标尺先行**——评估集在第一个检索版本跑通时同步建成，此后所有优化必须有对照数据；
2. **自底向上，每阶段可运行**——拒绝"憋大招"，每阶段结束用 curl/脚本验证；
3. **本机资产最大化**——模型/设施能复用就复用（pytxt ONNX 流水线、Zipkin、PG/Redis）；
4. **成本敏感**——一切 VLM/LLM 调用有缓存、有档位、有上限；开发期防呆（批量任务限速）。

---

## 阶段总览

```
Phase 0 ──── 环境与骨架
Phase 1 ──── 入库管线（解析路由 + 切块 + 向量化）
Phase 2 ──── 混合检索 + 基础问答 + 黄金评估集 v1   ← 标尺建成
Phase 3 ──── LangGraph Agentic RAG（路由/拆解/CRAG/反思/拒答）
Phase 4 ──── 记忆系统 + 反馈闭环
Phase 5 ──── HITL 审批 + Prompt 管理 + 回归门禁
Phase 6 ──── 可观测性 + 诊断 Agent + MCP 对外
Phase 7 ──── Java 薄网关（可裁剪）+ 模型实验收尾 + 全链路打磨
```

---

## Phase 0：环境与骨架

- [ ] 创建 `rag_kb` 数据库 + pgvector 扩展；`scripts/init_db.sql`（架构文档 §十全部表）
- [ ] `rag-python` 骨架：pyproject、config.py（读 .env）、结构化日志、FastAPI health
- [ ] psycopg3 连接池封装（min5/max20，从 pytxt 经验移植并升级到 psycopg3）
- [ ] MiMo 客户端封装：OpenAI 兼容调用、`enable_thinking` 档位参数、重试/超时、
      流式 SSE、token 统计
- [ ] 模型就位：复制 ritrieve_zh_v1(int8)、bge-base(int8)、reranker-v2-m3(int8)
      到 `rag-python/models/`，各写一次推理冒烟测试

**验收**：health 端点 + MiMo 一次带思考/不带思考调用 + 三个本地模型各推理一条。

## Phase 1：入库管线

- [ ] 语料集准备：按架构文档 §十一 采集 6 类文档入 `data/corpus/`
- [ ] 页级检测器：文本密度 + 表格特征（块对齐度 + drawings 线条）
- [ ] 通道A：pymupdf 文本直提 → 页级中间表示
- [ ] 通道B：表格区域裁剪 → MiMo-V2.5 结构化表格（JSON schema 约束 + 重试降级）
- [ ] 通道C：扫描页整页 VLM 解析（用自渲染扫描 PDF 验证）
- [ ] 图片通道：语义描述 + 文字抽取双路
- [ ] 解析缓存：按 file_hash 落盘 `data/parsed/`
- [ ] 切块器：段落策略 + overlap + 上下文注入行；表格/图片 chunk 单独路径
- [ ] 向量化入库：ONNX 批处理流水线 + `kb_chunk` 写入 + HNSW 索引
- [ ] API：`POST /api/ingest/upload`、`GET /api/ingest/status/{doc_id}`

**验收**：6 类语料全部入库成功；每页 parse_channel 留痕可查；
建库耗时与 VLM 调用次数统计输出（成本基线）。

## Phase 2：混合检索 + 基础问答 + 黄金评估集 v1

- [ ] 向量检索 + BM25 索引（jieba 自定义词典：语料中的术语/人名）
- [ ] RRF 融合 + Rerank（信号量 1 并发 + 15s 超时降级）+ 阈值判定
- [ ] 检索日志写 `retrieval_log`（分阶段耗时）
- [ ] **黄金评估集 v1**：60-80 题四维标注（事实/表格图表/跨页/拒答）入 `eval_question`
- [ ] 评估器 v1：Context Recall@8、MRR、Refuse Accuracy（检索层指标先行）
- [ ] 基础问答 API：`POST /api/rag/ask`（检索 + MiMo 生成 + 引用标注 + SSE 流式）
- [ ] **实验 E1**：embedding 三选一对照，定默认模型
- [ ] **实验 E5**：表格页通道A vs 通道B 的表格维度 Recall 对照

**验收**：评估报告 v1（分维度指标）；ask 端点端口级验证 10 问；
E1/E5 报告入 `docs/experiments/`。

## Phase 3：LangGraph Agentic RAG

- [ ] AgentState + 主图骨架（StateGraph + PostgresSaver checkpointer）
- [ ] Route 节点：simple/standard/complex/out_of_scope 四分类（关思考）
- [ ] Decompose 节点：complex 拆 2-3 子查询并行检索合并
- [ ] GradeCtx 节点（CRAG 式）：检索质量评分 → Rewrite+ReRetrieve（≤2 次）→ NoAnswer
- [ ] Generate 节点：思考档位按路由档位配置；提示词从 prompt_registry 加载
- [ ] Reflect 节点（仅 complex）：忠实度/相关性自检，≤1 次修正
- [ ] Guard 节点：拒答话术
- [ ] 生成层指标接入评估器：Faithfulness、Answer Relevancy（LLM-as-judge）
- [ ] **实验 E3**：思考档位开/关对照；**实验 E4**：反思开/关对照
- [ ] API 切换到走主图（保留 simple 直连快路径）

**验收**：评估报告 v2 对比 v1 全维度提升数据；E3/E4 报告；
拒答维度正确率 >90%；端口级验证覆盖四种路由各至少 2 问。

## Phase 4：记忆系统 + 反馈闭环

- [ ] 会话记忆：滑动窗口 10 轮 + 早期摘要（qa_session.summary）
- [ ] 长期记忆：LLM 定期提炼关注点/未决问题 → memory_entry（带向量）→ 问答时召回注入
- [ ] 语义缓存：查询 embedding Redis 近邻匹配 → 命中直返
- [ ] 反馈 API：`POST /api/feedback`（赞/踩/纠错）→ feedback + bad_case 落库（含链路快照）
- [ ] 归因任务：bad case 自动归因 retrieval/generation（规则 + LLM 复核）
- [ ] 回归集升级流：confirmed bad case → eval_question.in_regression=true

**验收**：跨轮指代问答验证（"它/上面那个"）；负反馈 → 归因 → 回归集
全流程脚本走通。

## Phase 5：HITL 审批 + Prompt 管理

- [ ] prompt_registry 全部提示词入库（route/rewrite/grade/reflect/generate/diagnosis）
- [ ] 审批图：Suggest → interrupt → Execute/Rollback（Checkpointer 跨请求恢复）
- [ ] 回归门禁：prompt 变更自动跑回归集新旧对比，数据写入 eval_compare
- [ ] API：`POST /api/agent/run`、`POST /api/agent/resume`、`GET /api/agent/status`
- [ ] Prompt 管理 API：列表/详情/提交变更/审批决策

**验收**：完整闭环演练——提交 prompt 变更 → 自动回归对比 → interrupt 暂停 →
resume 批准 → 生效；拒绝路径回滚验证；进程重启后中断任务可恢复。

## Phase 6：可观测性 + 诊断 Agent + MCP

- [ ] OTEL 全链路追踪接入（解析/检索/生成 span）→ 本地 Zipkin
- [ ] 指标体系：token 消耗（按任务类型）、阶段延迟 P50/P95、缓存命中率、拒答率
- [ ] 诊断图：CollectMetrics → Analyze → GenerateReport（数据源含 eval_run，无冷启动问题）
- [ ] 诊断 API：latest / history / trigger；诊断建议可转 HITL 审批单
- [ ] MCP Server（独立进程）：search_knowledge / list_documents / ask_knowledge /
      document_status；stdio + SSE；用外部 MCP Client 实测接入
- [ ] （可选）A2A Agent Card 暴露诊断 Agent

**验收**：Zipkin 中可见一条完整问答 trace；诊断报告自动生成一份；
外部 Client 通过 MCP 成功调用知识库工具。

## Phase 7：Java 薄网关 + 收尾打磨（可裁剪）

- [ ] Spring Boot 3.x：鉴权（JWT）/限流/审计日志/SSE 转发（WebFlux Flux）
- [ ] 管理 API 代理：prompt/审批/诊断/评估报告查询
- [ ] Python 服务熔断降级（调用失败返回缓存或明确错误）
- [ ] **实验 E2**：reranker v2-m3 vs v2-gte 对照（需下载导出 ONNX INT8）
- [ ] 全链路压测与成本报告：单次问答平均 token/费用、缓存收益
- [ ] README：架构图 + 快速启动 + 实验报告索引 + 面试问答预演清单
- [ ] （可选）最小前端对话页

**验收**：经 Java 网关的完整问答链路；全部 5 个实验报告归档；
README 可让陌生人 30 分钟内跑起系统。

---

## 关键里程碑

| 里程碑 | 验收标准 |
|--------|---------|
| **M1 入库可用** | 6 类语料入库，解析通道留痕，建库成本统计输出 |
| **M2 标尺建成** | 60-80 题评估集 + 检索层指标报告 + E1/E5 实验结论 |
| **M3 Agentic 完整** | 四路由 + CRAG 纠错 + 拒答 + 反思，v2 报告全维度优于 v1 |
| **M4 闭环运转** | 反馈→归因→回归集→审批门禁 全链路演练通过 |
| **M5 运维能力** | 追踪/指标/诊断报告/外部 MCP 全部可用 |
| **M6 交付** | 网关联调 + 5 实验报告 + README + 成本报告 |

---

## 风险与应对

| 风险 | 应对 |
|------|------|
| MiMo API 限流/欠费 | 解析缓存 + 语义缓存 + 批量任务限速；开发期 token 预算监控 |
| VLM 解析表格不稳定 | JSON schema 约束 + 2 次重试 + 降级纯文本；解析失败率纳入监控 |
| CPU 建库慢 | VLM 页控制在 60 页内；向量化批处理 + P 核绑定 |
| LangGraph 版本变动 | 锁定版本；核心节点逻辑与框架解耦（节点是纯函数） |
| 评估集质量 | 构造过程三人格交叉（生成→校验→抽查）；分维度报告暴露不平衡 |
| psycopg3 与旧代码差异 | Phase 0 先做连接池冒烟，不带着假设往下写 |

---

## 与 v1.0 计划的差异说明

| v1.0 | v2.0 | 原因 |
|------|------|------|
| 小说知识库 + 知识图谱方向 | 通用文档问答 | 独立开发无用户反馈数据，小说域评估无法闭环 |
| DeepSeek + 预留多供应商 | MiMo 单一供应商 + 思考档位路由 | DeepSeek 涨价；mimo-v2.5 全模态一个模型覆盖全部需求 |
| MinerU 解析全家桶 | 按页解析路由（直提/VLM） | 成本 1/10，代码全自写，叙事更强 |
| 评估在第 9 周 | 评估集 Phase 2 建成 | 标尺先行，否则优化全是盲改 |
| MCP 内部调用 | MCP 对外集成 | 回答"为什么不直接调函数"的必然追问 |
| jf_ 表前缀 / japy_moments 库 | rag_kb 新库全新表名 | 与旧项目切割 |
| Multi-Agent 四 Agent 路由 | 单图四档路由 | 避免为架构而架构，复杂度花在检索纠错上 |
