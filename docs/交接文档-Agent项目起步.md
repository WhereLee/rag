# 交接文档：Agent 项目起步（用户因素 + 本机可复用资产）

> 创建：2026-08-24，RAG 项目收官后，用户计划新开 Agent 项目（新目录讨论）
> 读者：接手 Agent 项目的 agent
> 用途：让新会话**不用重新摸索**——用户怎么协作、本机有什么可复用的模型/服务器/代码资产

---

## 一、用户因素（协作规范，违反会严重影响体验）

### 身份与定位
- **已毕业，社招**（非在校学生）——一切要求生产级、企业级标准的根本原因
- 目标方向：后端开发 + LLM Agent；本 RAG 项目已完成（功能闭环/CI 全绿/部署/压测/文档收官），Agent 是下一个项目

### 沟通规则（硬性）
- **全程中文**；大白话讲解，别堆术语；每条结论/问题**标注影响**
- 要求**挑战性讨论**：不要顺着他说话，像同事一样质疑、给反面观点（他自认理解可能不对）
- **2026-08-24 明确：不要主动教"面试话术/面试叙事"**——他不需要话术指导，讨论就事论事讲技术；他主动问面试相关再谈
- 讨论方案时**给完整技术内容**（蓝图/模块/取舍），他喜欢深度技术讨论

### 动手前（最重要）
- **任何实质性操作前必须确认**：创建文件/写代码/改文件/跑命令——先说明要做什么、怎么做，等确认
- **方案先对话沟通**再出正式计划，不直接甩计划
- **方案偏离时暂停汇报**：原方案/备选方案/优劣对比，等决策，不自行切换

### 红线
- **严禁以"规模小/用不上/不值得/成本高"为由简化功能**——功能默认要做，不做必须有技术原因且由用户决策
- **密钥绝不进 git、绝不写入长期记忆**（.env 管理）
- **推断必须有证据**：无法验证的事实明说"无法判断"，禁止凭经验断言

### 工程习惯
- 测试双验收（正常 + 边界）；部署后校验提交号/特征串；临时脚本用后即删
- 文档沉淀：坑位（现象/原因/影响/处理 + 状态标签）+ 素材（结论/背景/话术/追问）——沿用 RAG 项目的格式
- 已固化 skill（本机全局生效）：`powershell-safe-command`（复杂命令一律脚本化）、`powershell-encoding`（中文编码）、`archive-interview-and-pitfalls`（文档归档）

## 二、本机可复用资产（下一个会话直接用的清单）

### 2.1 云服务器（124.223.36.154）
```
ssh ubuntu@124.223.36.154        # 密钥已配置，免密 sudo，密码登录已禁用
```
| 资产 | 状态 | 说明 |
|---|---|---|
| 腾讯云轻量 4G/4核（Xeon 8255C） | 运行中 | 内存：rag 服务已停，**当前仅 ~600MB 占用，可用 ~3.1G** |
| PostgreSQL 16 | **运行中** | 127.0.0.1:5432，库 rag_kb（用户 rag_app）；**Agent 项目可建新库**（建库时考虑备份扩展） |
| Redis 7 | 运行中 | 127.0.0.1:6379，占用 ~30MB |
| nginx | 运行中 | 80 端口，站点配置在 /etc/nginx/sites-enabled/ |
| rag 4 服务 | 已停+禁自启 | systemd 模板在 /etc/systemd/system/rag-*.service（User=rag 模式可照抄）；恢复：enable+start |
| COS 备份 | cron 每日 03:00 在跑 | 只备份 rag_kb 库；**Agent 项目新库需扩展备份脚本或新建** |
| COS 桶 | 轻量桶 yfwq-9-1325750042（在用）+ 标准桶 rag-1325750042（ap-shanghai，已验证） | coscmd：/opt/rag/coscmd-venv/bin/coscmd，密钥 ~/.cos.conf（600） |

### 2.2 LLM API（Agent 项目的关键资源，密钥不写值只写位置）
| 项 | 配置 | 位置 |
|---|---|---|
| **DeepSeek**（主问答 LLM） | `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL=deepseek-v4-flash`（便宜，2500 RPM）/ `LLM_ENABLE_THINKING=0` | 本地 `.env` + 服务器 `/opt/rag/.env`（rag:ubuntu 640） |
| **MiMo**（视觉 VLM，图片转录） | `MIMO_API_KEY` / `MIMO_MODEL=mimo-v2.5` | 同上 |
| **DeepSeek 要点**（RAG 项目实测） | 思考模式参数 `{"thinking":{"type":"enabled/disabled"}}`；带 tools 多轮必须回传 `reasoning_content` 否则 400；流式 delta 含 reasoning_content | 实现参考 rag-python/src/llm/mimo_client.py |

### 2.3 本地模型（Agent 的记忆检索/工具可直接复用）
- **本地**：`rag-python/models/`（bge-base-zh-v1.5-onnx-int8 99MB + bge-reranker-base-onnx-int8 266MB + ritrieve + v2-m3）
- **服务器**：`/opt/rag/rag-python/models/`（同款，部署时 scp 上去的）
- 加载模式参考：`rag-python/src/retrieval/embedder.py`（惰性单例、int8、线程池配置）——Agent 的长期记忆向量检索可整套迁移

### 2.4 代码资产（从 RAG 仓库直接抄/改）
| 资产 | 位置 | 用途 |
|---|---|---|
| **CI 模板** | `.github/workflows/ci.yml` + requirements.txt freeze 流程 + requirements-dev.txt | 新项目直接复制改路径 |
| **conftest 环境适配** | `rag-python/tests/conftest.py` | CI 无 .env/无模型的三差异适配模式 |
| **LangGraph 图模式** | `rag-python/src/agent/main_graph.py`（11 节点图：条件路由/CRAG 循环/HITL interrupt + PostgresSaver checkpointer） | Agent 项目是 LangGraph 主场，整套模式迁移（节点/边/状态/checkpointer/interrupt） |
| **状态机/幂等/崩溃回收** | `rag-python/src/ingest/worker.py` + parse_tasks 表 | 工具副作用治理、任务计划状态机 |
| **检索技能** | `rag-python/src/retrieval/`（混合检索/RRF/级联 rerank/信号量） | 长期记忆检索、工具库知识检索 |
| **安全** | Java 网关（JWT/限流/审计/上传魔数）+ Python prompt_guard | 工具权限/注入防护/审计 |
| **压测方法论** | `scripts/bench_*.py` | 分层压测 + 判定公式（标准先行） |
| **文档体系** | `docs/坑位记录.md` + `docs/面试素材积累.md` + `docs/交接文档-服务器资产与协作规范.md` | 格式沿用，坑位/素材可读（有大量可迁移教训） |

### 2.5 环境
- 本地 Windows + PowerShell 5.1（两个编码/转义 skill 已固化，**复杂命令一律"写脚本→scp→执行"**）
- 本地 Python 3.13（rag-python 测试用）+ 服务器 Python 3.12 venv（/opt/rag/rag-python/.venv）
- GitHub：WhereLee/rag 仓库（CI 已跑通，Actions secrets 模式已验证）

### 2.6 资源模型认知（Agent 项目与 RAG 的本质差异，避免重蹈 0.6 QPS 灰心）
- **Agent 项目 LLM 全走 API（DeepSeek 2500 RPM），本地只做编排**——CPU 消耗极小，4G/4 核不再是并发瓶颈
- 并发天花板 = API 配额（2500 RPM 远大于需求），本地只处理编排/存储
- 这意味着：Agent 项目的性能工程重点是**成本控制（token）、延迟（LLM 调用次数）、可靠性（重试/熔断）**，不是本地推理吞吐

## 三、Agent 项目内容蓝图（上轮讨论结论，供新会话延续）

1. **工具调用循环**：ReAct（LLM 决策→工具→观察→再决策）、工具层抽象（注册/schema/校验/结果规范化）、并行工具调用、上下文 token 管理
2. **规划与执行**：任务拆解→子任务 DAG→计划状态机→失败重规划
3. **记忆系统**：短期（窗口/摘要）+ 长期（向量库，复用检索技能）+ 工作记忆（中间产物持久化）
4. **可靠性**：循环失控防护（最大步数/成本熔断）、错误恢复（重试/换路径）、副作用幂等
5. **可观测**：每步 trace（LLM/工具/状态）、检查点中断恢复（LangGraph checkpointer）、成本监控
6. **评估**：任务成功率、工具调用正确率、步数效率、失败模式分析、回归集
7. **安全**：工具权限白名单、提示注入防护、决策审计
8. **HITL**：关键决策人工确认（LangGraph interrupt 主场）

---

> 使用建议：新项目目录建好后，把本文件复制过去作为会话起点；服务器资产详情见 `docs/交接文档-服务器资产与协作规范.md`（含连接方式/操作铁律）。
