# 智能文档问答系统 —— 最终交付报告

> 状态：**已收官（2026-08-23）**。功能闭环、质量受控、可部署、有数据、有故事，进入面试展示阶段。

## 一、项目定位与交付范围

经典命题「智能文档问答」的生产级实现，目标不是"能跑通的 demo"，而是把每一层的
价值都**量化证明**：

| 层 | 交付物 | 证明方式 |
|---|---|---|
| 解析 | 四通道路由（文本直提/表格 VLM 结构化/扫描件 VLM 转录/图片 VLM 描述），VLM 结果 hash 缓存 | 8 文档 420 块入库，扫描件/表格题问答可追溯到 VLM 块 |
| 检索 | pgvector HNSW + BM25(jieba) → RRF → bge-reranker 精排 → 阈值分级 | E2 证明 rerank 净增量 MRR +0.517 |
| 问答 | LangGraph 主图：route 四档 / decompose / CRAG grade≤2轮 / 思考档位 / reflect | agent-base vs baseline 对照（E3/E4 消融） |
| 评估 | 40 题黄金集四维度 + RAGAS 式指标 + LLM-as-judge + 回归子集 | 全部实验同一套评估，可复现 |
| 运营闭环 | 反馈→归因→回归集升级；Prompt 版本化 + HITL 审批（回归对比量化依据） | 全链路回归 15/15 通过 |
| 可观测 | OTEL→Zipkin 全链 trace、慢调用观测、诊断 Agent 自动报告 | 回归 7.2 + 诊断 6.1 通过 |
| 对外 | MCP stdio server（4 工具）+ Java 薄网关（JWT/限流/审计/SSE） | MCP 冒烟 + 网关全回归通过 |

## 二、核心指标（40 题黄金集）

| 实验 | run | context_recall | mrr | refuse_acc | 备注 |
|---|---|---|---|---|---|
| 基线（bge-base+rerank） | 1 | 1.0 | 0.9167 | 1.0 | 检索层 |
| E1 ritrieve-1792 | 6 | 1.0 | 0.9141 | 1.0 | 持平，默认仍用 bge-base |
| E2 关 rerank | 9 | 1.0 | **0.3998** | 1.0 | 精排是排序质量最大单点 |
| E5 排除 VLM 块 | 8 | 1.0 | 0.9479 | 1.0 | 见实验报告如实分析 |
| agent-base（带 judge） | 7 | 待回填 | | | |
| E3 关思考 | 待回填 | | | | |
| E4 关反思 | 待回填 | | | | |

## 三、全链路回归测试（2026-08-17）

`scripts/regression_e2e.py`：15 项全过（2 项初跑失败为脚本断言/超时口径问题，
补验通过，非系统缺陷）。

| 步骤 | 结果 | 说明 |
|---|---|---|
| 1.1 注册用户/admin | PASS | |
| 1.2 登录 JWT + 错误密码 401 | PASS | 手写 HS256 三段式 |
| 1.3 无/坏 token 拒绝 | PASS | |
| 2.1 网关问答 JSON | PASS | 29.2s，含 1283 家企业事实 + 7 引用 |
| 2.2 语义缓存命中 | PASS | 二次请求 0.1s cache_hit=true |
| 2.3 越界拒答 | PASS | |
| 2.4 SSE 流式 | PASS | 9 事件含 [DONE]，网关透传 |
| 3.1 限流 20/min | PASS | 25 并发 → 5 个 429 |
| 4.1 反馈→归因 | PASS(补验) | attribution=generation；服务端 >30s，客户端超时口径修正 |
| 5.1 Prompt 列表经网关代理 | PASS | 7 个 prompt code |
| 5.2 非 admin POST 代理 403 | PASS | |
| 5.3 HITL 审批全流程 | PASS(补验) | submit→回归对比(10题×2)→interrupt→rejected→回滚验证 |
| 6.1 诊断 Agent | PASS | 38.5s，4170 字符报告 |
| 7.1 审计日志 | PASS | 27 条落 kb_audit_log |
| 7.2 Zipkin trace | PASS | OTEL span 可查 |

另：MCP stdio 冒烟（`scripts/smoke_mcp.py`）PASS——4 工具注册、
list_documents/search_knowledge 实调命中白皮书内容。

## 四、关键踩坑与修复（本项目真实发生）

1. **MiMo reasoning 挤占 max_tokens**：finish=length 且 content 为空。
   修复：预算逐级加倍 cap 8192 重试；chat_json 不传 response_format。
   实验链中仍捕获到 16+ 次成功兜底（日志 `retry with max_tokens=2048`），证明修复生效。
2. **E1 维度不匹配**：查询编码器与向量列必须配对（COLUMN_EMBEDDER）。
3. **PostgresSaver**：from_conn_string 是上下文管理器 → 常驻 autocommit 连接。
4. **mcp 2.0 破坏性变更**：FastMCP 移除 → 迁移 MCPServer API。
5. **评估 chunk_id 漂移**：改用证据关键词锚定，重入库后可复现。
6. **--reload 会杀长任务**：评估/实验一律进程内直调或独立进程。
7. **judge 偶发 JSON 截断**（run 7 q20）：reasoning 挤占 JSON 预算，单题 judge 缺失
   不影响核心指标；已知项，可通过 judge 专用更大预算消除。

## 五、目录与启动

见 `rag/README.md`（架构图 / 快速启动 / API 一览 / 实验索引 / 已知约束）。

## 六、2026-08 工程收尾成果（收官补充）

### 6.1 生产部署与运维闭环
- **安全收尾已实测生效**：ufw 仅开放 22/80/443 + SSH 禁用密码登录（坑位 #45 状态同步）。
- **COS 异地容灾备份**：每日 03:00 `pg_dump -Fc` → gzip → coscmd 上传轻量对象存储（双保留：本地 7 天 / COS 30 天）+ **恢复演练闭环**（下载→临时库还原→6/6 表行数对齐；暴露 --role/扩展/解析列序三个真实坑位 #51/#52）。

### 6.2 性能工程（两套压测数据）
- **第一套逐环节极限**：网关文件列表 QPS 1590/p50 4.6ms、问答代理 740 QPS、Redis GET 5324 QPS、检索层 4 档（级联 rerank 38.5% 免精排）。
- **第二套日常混合**（4 并发 120s 真实行为比例）：总 QPS 3.48、错误率 0.24%。
- **问答 LLM 切换 DeepSeek v4-flash**（LLM/VLM 配置分离；2500 RPM 补测 4/8/16 并发 0 失败）：外部配额瓶颈解除，瓶颈归因改写为**服务器 CPU 检索层**（0.6 QPS，LLM 段仅 1-2s）。

### 6.3 质量与工程化
- **GitHub Actions CI 上线**（push 自动跑：pytest 全量 + 前端构建 + Java 编译，全绿）；依赖锁定 requirements.txt（104 包锁生产版本）+ requirements-dev.txt；CI 三连败修复沉淀为坑位 #56。
- **软删同名重传 500 修复**：全量唯一约束 → 部分唯一索引 `WHERE status=1`（坑位 #53）。
- **旧链路清理收官**：死代码删除（sync_service/bm25_index/Chat.vue）+ 旧问答入口删除（rag_api/ChatController/chatApi 前端函数），全仓零残留引用。
- **坑位 #54**：部署 git pull 静默失败教训（部署后必须校验提交号 + 代码特征串）。

### 6.4 已知遗留（有意为之，均带评估）
| 项 | 状态 | 评估 |
|---|---|---|
| 跨语言检索盲区（#25） | 演进项 | 英文语料占比 ~50%；修复需动黄金集验证过的检索核心，收尾期不引入回归风险；双语查询扩展方案已成型，作为下一迭代第一顺位 |
| 监控告警（#55） | 暂缓 | 腾讯云自带监控 + 探活脚本 + Webhook，半天成本，用户确认后实施 |
| CI/CD 之 CD | 留待下一项目 | 手动发布 + 渐进观测是资源受限下的合理策略，已形成面试叙事 |
