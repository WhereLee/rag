# 概念阶段 Agent 开发方案（D1-D4）

> 创建：2026-08-29（在线调研 2026-08 最新生态后定稿）
> 依据：`docs/agent设计-概念阶段.md`（设计唯一事实源，本方案只写"怎么实现"）
> 形态：单对话 Agent（LangGraph ReAct 主循环）+ 9 工具集；**概念阶段无子 Agent**

---

## 1. 技术选型（2026-08 调研结论）

| 项 | 选型 | 说明 |
|---|---|---|
| 语言/框架 | Python 3.12 + FastAPI | AI 服务侧 |
| Agent 构建 | **`langchain.agents.create_agent`**（LangChain 1.x） | ⚠️ 2026 年 `langgraph.prebuilt.create_react_agent` 已废弃，官方推荐 `create_agent`——底层仍是 ReAct 图（model 节点 + tools 节点 + 条件边），返回 CompiledStateGraph，可 stream/加 checkpointer |
| 图运行时 | LangGraph 1.x（锁版本） | API 换代快（0.x→1.x 不到一年），锁死版本 + 升级评审 |
| 会话持久化 | `langgraph-checkpoint-postgres` 的 **PostgresSaver** | thread_id = concept_id，服务重启后可续聊（checkpoint 是运行态缓存，业务事实源仍是 Java 侧 concept_draft_session 表） |
| 长期记忆 | **不用 LangGraph Store** | 经验/思考角度是业务数据（权限+审计在 Java），存 Java 侧 experience_entry 表，Python 通过工具读写——与"图管生成、业务表管事实"原则一致 |
| 模型 | DeepSeek（主，OpenAI 兼容协议） | `langchain-openai` ChatOpenAI + base_url 指向 DeepSeek/mimo |
| web_search | 模型侧能力（mimo），**暂缓** | 可配置项，模型支持时启用 T2 |
| 工具定义 | `@tool` 装饰器 | docstring 即 LLM 看到的工具说明，必须写清触发条件 |

## 2. 目录结构（club-agent/python，新建）

```
python/
├── pyproject.toml
├── src/agent_draft/
│   ├── main.py              # FastAPI 入口（/health /chat /brief）
│   ├── config.py            # 模型/超时/开关（ai.draft.enabled 等）
│   ├── graph.py             # create_agent 组装（9 工具注册 + PostgresSaver）
│   ├── persistence.py       # PostgresSaver 初始化（连接池）
│   ├── prompts.py           # 起草助手系统提示词（固定模板，用户输入只进 data 区）
│   ├── state.py             # 图状态定义（messages + draft 快照）
│   └── tools/
│       ├── __init__.py      # 工具注册表（9 个）
│       ├── java_client.py   # 工具回 Java 的 HTTP 客户端（身份透传）
│       ├── context.py       # T3 get_club_context
│       ├── experience.py    # T1 search_experience / T7 save_experience / T8 save_thinking_pattern
│       ├── draft.py         # T4 get_draft / T5 generate_draft / T6 update_draft
│       └── skill.py         # T9 generate_skill
└── tests/
    ├── test_tools.py        # 工具函数单测（mock java_client）
    └── test_graph.py        # 图结构冒烟（内存 checkpointer）
```

## 3. 数据模型（Java 侧，DDL 入 init_db.sql）

```sql
-- 起草会话（多轮消息 + 工具调用 + 表单快照，审计与续聊的事实源）
CREATE TABLE IF NOT EXISTS concept_draft_session (
    id            BIGINT PRIMARY KEY,
    concept_id    BIGINT NOT NULL,
    user_id       BIGINT NOT NULL,           -- 发起人
    role          VARCHAR(10) NOT NULL,      -- user/assistant/tool
    content       TEXT,
    tool_name     VARCHAR(50),
    tool_args     JSONB,
    form_snapshot JSONB,
    tokens_in     INT, tokens_out INT,
    created_at    TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX ix_draft_session_concept ON concept_draft_session (concept_id, created_at);

-- 经验条目（统一经验库：业务知识 + 思考角度）
CREATE TABLE IF NOT EXISTS experience_entry (
    id BIGINT PRIMARY KEY,
    club_id BIGINT,
    category VARCHAR(30),                    -- thinking_pattern/筹备知识/风险教训/context
    title VARCHAR(100),
    content TEXT,
    owner_id BIGINT,                         -- 思考角度归属的发起人（可空=通用）
    source_concept_id BIGINT,
    source_user_id BIGINT,
    status SMALLINT DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX ix_experience_club ON experience_entry (club_id, status);

-- concept_session 加一列（提交时冻结）
ALTER TABLE concept_session ADD COLUMN IF NOT EXISTS ai_brief TEXT;
```

## 4. 接口设计

### 4.1 前端 → Java（代理，鉴权走既有 JWT）

| 接口 | 说明 |
|---|---|
| `POST /clubs/{clubId}/concepts/{id}/ai/chat` | body: `{message}` → 落用户消息 → 调 Python → 落 assistant 消息（含 tool 记录）→ 返回本轮完整消息列表 |
| `GET /clubs/{clubId}/concepts/{id}/ai/session` | 会话重放（页面刷新/换设备） |
| `POST /clubs/{clubId}/concepts/{id}/ai/brief` | 提交时生成想法简析（Java 提交接口内调用） |

约束：发起人本人 + 起草中状态（chat 时概念必须 status=1）；超时 30s 返回 1035"AI 暂不可用"；`@RateLimiter` 防刷；`ai.draft.enabled=false` 时 404。

### 4.2 Python → Java（工具执行，身份透传：chat 请求头带用户 token，Python 原样转发）

| 工具 | Java 端点 | 校验 | 写? |
|---|---|---|---|
| T1 search_experience | `GET /clubs/{clubId}/ai/experience?q=` | 管理层 | 读 |
| T3 get_club_context | `GET /clubs/{clubId}/ai/context` | 管理层 | 读 |
| T4 get_draft | `GET /clubs/{clubId}/concepts/{id}/draft` | 发起人 | 读 |
| T5 generate_draft | （Python 内纯 LLM，不调 Java） | — | 读 |
| T6 update_draft | `PUT /clubs/{clubId}/concepts/{id}/ai-draft` | 发起人+起草中 | **写**（trace ai_draft） |
| T7 save_experience | `POST /clubs/{clubId}/ai/experience` | 管理层 | **写**（人确认前置：前端确认弹窗后才调） |
| T8 save_thinking_pattern | `POST /clubs/{clubId}/ai/thinking-pattern` | 管理层 | **写**（人确认前置） |
| T9 generate_skill | `POST /clubs/{clubId}/ai/skill` | 管理层 | **写**（人确认前置，落盘 SKILL.md） |

**权限判断永远在 Java**；Python 是带身份的转发器。T6 的 Java 端点内部逻辑 = 校验发起人+起草中 → 更新字段 → trace(ai_draft, detail=草案摘要) → 返回最新草稿。

## 5. 对话 Agent 图（Python）

```python
# graph.py（示意）
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres import PostgresSaver

llm = ChatOpenAI(model="deepseek-v4-flash", base_url=..., api_key=...)
tools = [search_experience, get_club_context, get_draft,
         generate_draft, update_draft, save_experience,
         save_thinking_pattern, generate_skill]  # T2 web_search 暂缓
agent = create_agent(
    model=llm,
    tools=tools,
    system_prompt=PROMPT,          # 起草助手：角色/边界/表单字段语义/确认前置规则
    checkpointer=PostgresSaver.from_conn_string(DATABASE_URL),  # thread_id=concept_id
)
```

- 会话 key：`{"configurable": {"thread_id": str(concept_id)}}`——续聊恢复走 checkpointer
- 工具调用记录：Python 侧每次工具调用回传 Java 落 concept_draft_session（role=tool），保证**业务事实源完整**（checkpoint 挂了也能从表恢复）
- 上下文注入（thinking_pattern/相关经验）：不设 preprocess 节点——由 T1 search_experience 的 Java 端点拼好（检索经验 + 该发起人思考角度一起返回），主循环按需取

## 6. 前端（D1-D4 增量）

- **D1**：ConceptBirth.vue 表单右侧加聊天窗——消息列表 + 输入框 + 发送（POST /ai/chat，整包返回）；进入页面 GET /ai/session 重放；发送中 loading + 失败提示（可手动填表）
- **D2**：聊天消息里渲染"草案卡片"（AI 生成后展示字段预览 + [采纳] 按钮 → PUT ai-draft 落表 → 表单联动刷新）
- **D3**：对话中 AI 提议沉淀时展示确认卡片（[记住这个经验]/[归纳我的思考角度]）→ 确认后调 Java；详情页加"发起人思路"区块（ai_brief）
- **D4**：skill 确认卡片（展示 SKILL.md 内容预览 + [落盘]）

## 7. 分块任务清单

### D1：会话闭环（无工具纯对话）✅ 2026-08-29 完成
- [x] python 骨架：pyproject / config / main.py（/health /chat）+ PostgresSaver 初始化
- [x] create_agent 纯对话（无工具），DeepSeek
- [x] concept_draft_session 建表（alter_draft_session.sql 幂等 + init_db.sql 同步）
- [x] Java：entity/mapper/controller（chat 代理 + session 重放 + 消息落表 + 1035 超时）
- [x] 前端：聊天窗 UI + 发送 + 重放 + 降级提示（ConceptBirth.vue 双栏：表单左 + AI 聊天窗右）
- [x] 冒烟 8/8（登录/401/概念/chat 成对/回复非空/重放一致/403/落表）+ 浏览器实测（真实对话、布局、会话恢复）

### D2：工具接入（AI 能填表）✅ 2026-08-29 完成
- [x] Java 工具端点（context/experience/draft 读 + ai-draft 受控写 + trace）
- [x] Python 工具函数（java_client 身份透传）+ create_agent 挂工具
- [x] generate_draft 提示词与 JSON schema（planned_time/location/content + 决策说明 100 字内）
- [x] 前端：草案卡片 + 采纳按钮 + 表单联动（adoptedDraftId 防重复采纳）
- [x] 冒烟 12/12（上下文/经验冷启动/生成→采纳→trace→表单同步/非发起人 403）+ 浏览器实测（MiMo 真实生成 60s、草案卡片、采纳联动、审计留痕）
- [x] LLM 切换 MiMo（mimo-v2.5，OpenAI 兼容）；工具永不抛异常（防 checkpoint 污染）；超时链放宽 120s（详见设计讨论 K19-K23/S15）

### D3：经验沉淀 + 想法简析 ✅ 2026-08-29 完成
- [x] experience_entry 建表 + Java CRUD + 检索端点（含 thinking_pattern 注入）
- [x] extract_experience / extract_thinking_pattern 工具（只草拟）+ 前端确认卡片 + Java 落库端点
- [x] 提交后异步生成 ai_brief（@Async，不阻塞提交；失败零影响）
- [x] 详情页"发起人思路"区块（投票人/老师可见）+ trace ai_brief 留痕
- [x] 冒烟 10/10（沉淀/检索命中/注入/403/提交/异步 brief/详情可见）+ 浏览器实测（详情页区块 + 时间线 ai_brief 条目）

### D4：skill 生成
- [x] generate_skill 工具（SKILL.md 模板 + 元数据头 name/description/when_to_use）
- [x] 前端确认卡片（内容预览）+ 落盘到 skills 目录
- [x] 冒烟：生成 → 确认 → 落盘 → 校验 frontmatter 格式（8/8 + 浏览器实测确认卡片）

## 8. 测试方案

1. **Python 单测**：工具函数（mock java_client 断言参数透传/异常透传）；图结构（内存 checkpointer 跑一轮，断言工具被调用）
2. **冒烟脚本**（PS + UTF-8 BOM）：每块验收项全过（见各块冒烟清单）
3. **浏览器实测**：真实链路（登录 → 发起概念 → 对话 → 生成 → 采纳 → 提交 → 详情看想法简析）——雪花 ID 精度/中文编码这类坑只有浏览器链路能暴露
4. **并发/安全抽查**：非发起人调 chat 403；已提交后调 chat 1031；update_draft 越权写被拦

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| LangGraph 1.x API 仍在迭代 | 锁版本（pyproject 精确版本）；升级走评审 + 全量回归；核心逻辑与框架解耦（工具函数不依赖图 API） |
| DeepSeek tool calling 不稳定 | 工具 schema 简洁；失败重试 1 次；30s 超时降级"AI 暂不可用" |
| token 成本（多轮对话累积） | 会话超 20 轮后上下文摘要压缩；tools 只注入必要上下文（thinking_pattern 有 owner_id 过滤） |
| 中文编码（PS 冒烟脚本） | 脚本 UTF-8 BOM；断言查 DB 不依赖响应中文 |
| 会话表膨胀 | concept 终态（作废/通过）后会话只读；保留（审计），不清理 |

## 10. 里程碑验收

| 里程碑 | 验收 |
|---|---|
| M1（D1） | 对话闭环可跑：发起人能在概念页与 AI 对话，刷新后会话还在，AI 不可用时手动填表不受影响 |
| M2（D2） | AI 能检索经验 → 生成草案 → 人采纳落表 → trace 可见；表单是最终事实 |
| M3（D3） | 经验/思考角度沉淀并可复用；提交后详情页展示发起人想法简析 |
| M4（D4） | skill 生成落盘（带元数据头），对话 Agent 可加载复用 |
