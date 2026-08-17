# AI Agent 智能知识库平台 — 讨论总结报告

> ⚠️ **修订标注（2026-08-17）**：本文档为与早期 agent 的讨论记录，仅作历史留存。
> 其中「小说知识库」方向已被否决（独立开发无法获得高质量用户反馈数据），
> 项目重定位为「智能文档问答系统」，LLM 供应商改为小米 MiMo。
> **现行方案以 `architecture.md` v2.0 和 `development-plan.md` v2.0 为准。**

> 讨论时间：2026-08-17
> 参与方：用户（应届毕业生，软件工程 Java 方向） + Reasonix
> 背景：为简历项目做规划，目标是打造一个有企业级工程深度的 AI Agent 项目

---

## 一、用户背景与核心诉求

### 1.1 个人背景

- 应届毕业生（已毕业），准备找工作，需要简历项目
- 软件工程 Java 方向，但也可以用 Python，支持混合开发
- 已有 RAG 开发经验（`pytxt/rag` 项目：小说问答系统，含混合检索、Rerank、记忆系统）
- 本地环境：Java 17 / Python 3.13 / Go 1.26 / PostgreSQL / MySQL / Redis

### 1.2 核心诉求

1. **不要学生级别的简化**——要求按照企业级工程深度来做，关注效率、日志追查、并发控制、容错降级等
2. **不要应付面试的心态**——项目需要经得起面试官多轮深入追问
3. **希望了解行业现状**——2026 年 8 月 AI Agent 领域的热门技术、企业标准

---

## 二、核心问题讨论

### 2.1 问题一：面试官更想看"完整业务闭环"还是"技术功能点"？

#### 结论

> **面试官更想看"技术功能点"，但需要有业务场景包装。**

#### 分析

| 类型 | 优点 | 缺点 |
|------|------|------|
| 纯业务闭环（社团管理、OA） | 完整感、有场景 | 追问天花板低，80% 简历都是管理系统，无法区分 |
| 纯技术功能（短链、RAG） | 技术深度可展开 | 缺乏业务场景支撑，像在做练习题 |
| **业务场景 + 技术深度** | 有存在的理由 + 有追问空间 | **最优解** |

#### 原因

- 面试的本质是"你能比其他候选人多回答几层追问"
- CRUD 项目的追问答案千篇一律，无法拉开差距
- 技术功能点（如 RAG、Agent）可以从业务聊到架构、从检索策略聊到工程落地，一个项目够聊 3 轮面试

#### 最优公式

> **一个具体业务场景 + 2-3 个可深入追问的技术亮点**

业务场景只需要一句话说清楚"解决什么问题"就够了，剩下全部留给技术深度。

### 2.2 问题二：2026 年 8 月 AI Agent 领域合格项目应该包含什么？

#### 调研结果（GitHub 实时数据）

**一级热度（简历必须体现）：**

| 技术 | 说明 | GitHub 生态 |
|------|------|------------|
| **MCP 协议** (Model Context Protocol) | Agent 与外部工具的标准化通信协议，官方有 Python/TS/Go SDK | 13000+ 相关仓库 |
| **RAG 混合检索 + 精排** | 向量 + BM25 + RRF 融合 + CrossEncoder Rerank | 生产级标配 |
| **Agentic RAG** | Agent 自主决定"要不要检索、检索什么、结果够不够" | 2025-2026 年主旋律 |
| **Spring AI Alibaba** | Java 生态的 AI Agent 框架（阿里官方） | `alibaba/spring-ai-alibaba` |

**二级热度（加分项）：**

| 技术 | 说明 |
|------|------|
| **Multi-Agent 协作** | 多个专精 Agent 协作（Supervisor 模式） |
| **LangGraph** | 有状态工作流编排（节点+边+条件分支），Human-in-the-Loop 一等公民支持 |
| **LLM 可观测性** | 调用链追踪、token 监控、延迟分析（Opik, LangSmith） |
| **评估体系** | Recall@K、MRR、RAGAS、Faithfulness |
| **Self-Reflection** | 生成后自检：忠实度 + 相关性 + 完整性 |

---

## 三、用户开发经验洞察（核心设计输入）

### 3.1 洞察一：Agent 不应只面向 C 端，管理侧同样需要

**用户原话**：Agent 在管理员层面也应该发挥作用，比如周期性阅读性能检测报告、得出优化建议、快速定位问题。

**分析**：这是一个企业级思维——AI 系统不只服务终端用户，还需要服务"运营系统的人"。管理侧 Agent 可以做的：

| 场景 | Agent 角色 |
|------|-----------|
| Prompt 优化建议 | 分析问答日志，发现"检索到了但答不好"的 case，建议 prompt 调整方向 |
| 性能诊断 | 读取链路日志，定位瓶颈（如 Rerank 阶段延迟飙升） |
| 检索质量分析 | 统计命中率趋势，低置信度 case 聚类 |
| 异常告警 | SQL 慢查询、API 超时、错误率上升 |

### 3.2 洞察二：确定人机边界（Human-in-the-Loop）

**用户原话**：Agent 开发中最明显的教训是确定人机边界。某些业务交由人来决策，Agent 应该只是意见。

**分析**：这是从实际踩坑中总结的架构原则。具体体现：

- **Prompt 变更**：Agent 建议修改 → 管理员审批 → 生效
- **索引重建**：Agent 发现质量问题建议重建 → 管理员确认 → 执行
- **敏感操作**：Agent 生成的回答涉及敏感信息 → 人工审核 → 发布

**技术实现**：使用 LangGraph 的 `interrupt()` 机制，在关键决策节点暂停等待人工输入。

### 3.3 洞察三：合理引入 Agent 技术栈，不简化

**用户原话**：Function Calling、MCP/Skill 都没做过，想合理加入。希望加入有挑战的 Agent 技术内容，做加分项。

**分析**：用户不只是想堆技术，而是想在有真实业务场景的项目中自然地融入这些技术点。

---

## 四、技术选型决策过程

### 4.1 LangGraph vs Spring 状态机

| 方案 | 优点 | 缺点 | 结论 |
|------|------|------|------|
| **LangGraph4j (Java)** | 和 Spring 生态集成 | 生态不够成熟，存在已知问题 | ❌ 不采用 |
| **Spring 状态机 + Spring AI** | Java 一等公民 | Human-in-the-Loop 需要自建，Agent 编排能力弱 | ❌ 过于简化 |
| **LangGraph (Python)** | 生态最成熟，interrupt/Checkpointer/SubGraph 一等支持 | Agent 逻辑在 Python 端 | ✅ **最终选择** |

**决策理由**：用户明确倾向 LangGraph，且 Python 端已有成熟的 RAG 基础。LangGraph 的 `StateGraph`、`interrupt()`、`Checkpointer`、`SubGraph` 都是一等公民支持，是 Agent 编排的最佳选择。

### 4.2 Java + Python 双层分工

| 职责 | 层 | 技术 | 理由 |
|------|-----|------|------|
| Agent 编排（ReAct/多 Agent/路由） | Python | **LangGraph** | 生态成熟，interrupt/SubGraph 原生支持 |
| RAG 管线（检索+精排） | Python | FastAPI + ONNX | 已有成熟实现，BM25/pgvector/RRF/Rerank |
| MCP Server（工具标准化） | Python | MCP Python SDK | 官方 SDK 最成熟 |
| Self-Reflection / Query Decomposition | Python | LangGraph 节点 | Agent 推理逻辑和检索紧耦合 |
| 诊断 Agent / 评估 | Python | LangGraph + LLM | 数据分析 + 生成报告 |
| API 网关（鉴权/限流/日志） | Java | Spring Boot | 企业级框架天然适合 |
| 用户管理 / 权限 | Java | Spring Boot + JPA | 业务 CRUD |
| Prompt 版本管理 | Java | Spring Boot + JPA | 业务 CRUD + 审批流程 |
| LLM 简单调用（非 Agent） | Java | Spring AI | ChatClient 统一抽象 |

**通信方式**：Java 通过 HTTP 调用 Python 服务（内部 API）。

### 4.3 数据层

| 存储 | 用途 |
|------|------|
| PostgreSQL + pgvector | 向量存储 + 业务数据（prompt/审批/用户） |
| Redis | 会话缓存、分布式锁、任务队列 |
| 文件系统 | 原始文档、模型文件、评估报告 |

---

## 五、最终项目定义

### 项目名称

**AI Agent 智能知识库平台**

### 一句话定位

> 一个面向终端用户和系统管理员双侧的 AI 知识库问答平台。用户侧提供基于私有文档的 Agentic RAG 问答，管理侧提供 Agent 驱动的 Prompt 优化建议、性能诊断和检索质量分析，并通过 LangGraph 的 Human-in-the-Loop 机制确保关键决策的人工可控。

### 面试叙事线

> "这个项目最核心的设计决策是**双侧 Agent 架构**——不只是用户在用 Agent，管理员也在用 Agent。用户侧 Agent 负责 Agentic RAG 问答（含 Query Decomposition 和 Self-Reflection），管理侧 Agent 负责 prompt 优化建议、性能异常诊断、检索质量分析。
>
> 同时我从开发中总结出一个关键教训：**确定人机边界**。Agent 不应该是全权决策者，在 prompt 变更、索引重建这些关键操作上，我用 LangGraph 的 interrupt 机制设置了人工审批节点，Agent 只出建议，人来做最终决策。
>
> 技术上我实现了 Multi-Agent Supervisor 路由、ReAct 自主决策式检索、MCP 协议工具标准化、Self-Reflection 自检修正，以及完整的可观测性体系和离线评估框架。"

**这段话里每一个点都可以展开 10 分钟的深度追问。一个项目够面 3 轮。**

---

## 六、参考项目与技术资源

### GitHub 调研热门项目

| 项目 | Stars | 说明 | 参考价值 |
|------|-------|------|---------|
| `langchain-ai/langchain` | 90k+ | Agent 工程平台 | LangChain/LangGraph 核心 |
| `microsoft/autogen` | 40k+ | 多 Agent 对话框架 | Multi-Agent 参考 |
| `openai/openai-agents-python` | 20k+ | OpenAI 官方 Agent 框架 | Function Calling 范式 |
| `alibaba/spring-ai-alibaba` | 高 | Java AI Agent 框架 | Spring AI 集成参考 |
| `SciPhi-AI/R2R` | 高 | 生产级 RAG 系统 | RAG 架构参考 |
| `comet-ml/opik` | 7k+ | LLM 可观测性 | 监控 + 评估参考 |
| `langgraph4j/langgraph4j` | - | LangGraph Java 移植 | 评估后决定不用 |
| `modelcontextprotocol/python-sdk` | 高 | MCP 官方 Python SDK | MCP 实现参考 |

### 核心技术栈

- **LangGraph**：Agent 有状态工作流编排
- **Spring AI Alibaba**：Java 侧 AI 能力
- **FastAPI**：Python AI 服务
- **PostgreSQL + pgvector**：向量存储
- **BGE-base-zh-v1.5**：中文 Embedding 模型
- **bge-reranker-v2-m3**：Rerank 精排模型
- **MCP**：Model Context Protocol 工具标准化
- **ONNX Runtime**：模型推理加速（INT8 量化）
