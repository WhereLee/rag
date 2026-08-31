# Agent 设计：概念阶段（企划起草助手）

> 创建：2026-08-29（多轮讨论定稿）
> 定位：**概念阶段（活动发起/起草）Agent 设计的唯一事实源**。本阶段所有 Agent 设计以此文档为准，其他文档中的相关章节已迁移/清除（见文末"演进注记"）。
> 范围：概念诞生环节的发起人侧（想想法 → 对话构思 → 表单产出 → 提交审批）。投票/批复/执行/复盘等环节的 Agent 设计不在本文档范围。

---

## 1. 设计前提（三条边界，讨论定稿）

1. **控制权与能力分离**：Java 状态机掌控流程流转（起草中/投票/复议/批复/超时），Agent 负责智能辅助。Agent 可在多个节点提供辅助（起草建议、投票摘要、批复辅助、复盘素材），**只要不决定状态跳转**。
2. **表单是主体，AI 是辅助**：对话是构思手段，表单是唯一产物出口。AI 产出必须经过人的眼睛（人确认/编辑后才落库），提交永远是人的动作。
3. **受控行动能力**：Agent 有受限的写权限（回填草稿、沉淀经验），但必须：身份透传（代表发起人）、范围限制（仅起草中状态 + 本概念）、全量留痕（trace 记录 ai_draft）、人确认前置。

## 2. 用户场景（定稿用例）

副社长只有一个想法（"下月 5 号文昌有发射计划，想组织骑行活动，学校出发 75 公里"）→ 与起草助手对话：

1. 对话 Agent 分析想法：查系统内经验（RAG）→ 无经验给通用信息（发射壮观/75km 强度/一天来回 vs 两天/露营 vs 旅租/征集修车帮扶组等）
2. 用户说"你生成一版" → 方案 Agent 填表单（update_draft 受控写）
3. 用户手动改表单 → 告诉 Agent"我改了这些" → Agent 再给建议
4. 有价值的点 → save_experience 沉淀（供未来同类活动复用）；思考角度 → 归纳为 thinking_pattern（按发起人复用）
5. 多轮打磨出初稿 → 用户点提交 → 进入投票
6. 提交后：其他管理层看到「表单 + AI 整理的发起人想法简析」（活动意义/考虑过的问题/参考过的经验）

**Agent 是可选项**：不打开对话窗口直接填表提交是合法路径，主流程零依赖 AI。

## 3. 总体架构

```
┌─ Java 状态机（事实与控制层，不可动摇）──────────────┐
│  概念状态机 / 权限 / 事务 / 审计(trace)             │
│  提交 → 投票 → 复议 → 老师批复 → 通过/作废          │
└────────────────────────────────────────────────────┘
        ▲ 受控写(update_draft)        ▲ 会话消息/快照落表
        │ 仅起草中+本概念              │
┌─ Python Agent 层（智能层，LangGraph）───────────────┐
│  对话 Agent（ReAct 主循环，唯一的一张图）             │
│    ├─ 工具：检索/搜索/上下文/表单读                  │
│    ├─ 工具：generate_draft（方案生成，单步）          │
│    └─ 工具：受控写（update_draft/经验/思考角度/skill）│
└────────────────────────────────────────────────────┘
        ▲
   Java 代理接口（身份透传 + 限流 + 超时降级）
```

## 4. 对话 Agent（唯一的一张图）

- 形态：LangGraph `create_react_agent` 主循环（ReAct：推理 → 行动 → 观察 → 回复），用户唯一面对的面；**所有多步决策（要不要补检索、信息够不够、何时生成）都在主循环内完成**
- 意图路由（每轮消息判断）：
  - 闲聊/分析想法 → 直接回答，需要信息时走检索双通道
  - "生成一版/帮我填表/改成两天" → 调用 generate_draft 工具（带新约束增量修订）
  - "这个点值得记下来" → 提议 save_experience（用户确认）
  - "把思考过程做成 skill" → 提议 generate_skill（用户确认）
- 检索决策：系统内有历史可依 → `search_experience`（RAG）；系统内没有（发射时间/路况/天气）→ `web_search`（模型侧能力，暂缓）
- 上下文管理：长对话压缩历史摘要 + 注入该发起人的思考角度（thinking_pattern）
- 会话持久化：消息 + 工具调用 + 表单快照落 `concept_draft_session` 表（事实源，checkpoint 只是运行态缓存）

## 5. 方案生成：工具，不是子 Agent（形态修正定稿）

**结论：概念阶段没有子 Agent。** 方案生成是 `generate_draft` 工具（单步：输入需求+上下文 → 输出草案 JSON）。

- **为什么不是独立子 Agent**：方案生成看似多步（理解→补检索→生成→校验→修订），但"要不要补检索/信息够不够"的判断**全部可由对话 Agent 主循环吸收**——主循环先检索、先问用户，把信息集齐后调一次生成；不满意则继续对话、再调一次带新约束的修订。每次调用都是确定性单步。
- **判据**（全项目统一）：子任务内部需要 LLM 自主多步循环 → 子 Agent；确定性单步 → 工具。概念阶段全部是工具；**复盘 Agent（活动后）是第一个真子 Agent**（自己决定补查记录、按维度分析、不满意重写）。
- **代价对比**（若硬上子 Agent）：双层图 = 两套循环/超时/状态/checkpoint；每次生成多一层 LLM 循环，token 翻倍；排障链路翻倍。职责分离（对话管交互、方案管产出）用独立函数即可实现，不需要独立图。
- 流程：输入（对话上下文 + 已确认约束）→ 生成草案（planned_time/location/content + 决策说明）→ 校验（时间冲突 check_schedule + 字段完整性）→ 输出；增量修订 = 带新约束再调一次
- 写表单：通过 `update_draft` 落字段（受控写，trace 留 ai_draft）
- 决策说明累积 → 提交时生成"发起人想法简析"（ai_brief）

## 6. 工具集（9 个）

| # | 工具 | 读/写 | 触发 | 边界/确认 |
|---|---|---|---|---|
| T1 | `search_experience` | 读 | 需要系统内历史经验 | RAG 检索（经验库+历史留痕+复盘），身份透传 |
| T2 | `web_search` | 读 | 系统内无经验需外部信息 | **模型侧自带能力**（mimo），可配置，暂缓实现 |
| T3 | `get_club_context` | 读 | 需要社团上下文 | 简介/管理层/往届/活跃概念 |
| T4 | `get_draft` | 读 | 同步用户手动改后的表单 | 当前表单快照（事实源是表单） |
| T5 | `generate_draft` | 读（不落库） | 生成/修订草案（"生成一版"/"改成两天"） | 输入需求+上下文 → 输出草案 JSON + 决策说明；产物经人确认后由 update_draft 落表 |
| T6 | `update_draft` | **写** | 草案经人确认/采纳后落表 | 发起人本人+起草中+本概念，trace 留 ai_draft |
| T7 | `save_experience` | **写** | 对话中出现可复用知识 | AI 提议 → 用户确认 → 落经验库（可追溯） |
| T8 | `save_thinking_pattern` | **写** | 归纳发起人思考角度 | 多轮对话积累后归纳 → 用户确认 → 存 thinking_pattern（按发起人复用） |
| T9 | `generate_skill` | **写** | 把思考框架固化为 skill | 用户确认 + 落盘为 SKILL.md（带元数据头） |

**不提供**：一切状态流转工具（提交/投票/批复）——AI 的写被圈死在"起草中"的字段里。

## 7. 经验体系（统一经验库）

- **不设独立记忆表**：一切可复用知识统一为经验（`experience_entry`），含业务知识（筹备/强度/住宿/风险）与**思考角度**（thinking_pattern）
- 表结构：`experience_entry(id, club_id 可空, category[thinking_pattern/筹备知识/风险教训/context], title, content, owner_id, source_concept_id, source_user_id, status, created_at)`
- **思考角度归纳机制**（T7）：多轮对话积累 → AI 归纳"这位发起人的思考角度清单"（先考虑强度分级 → 再考虑住宿 → 关注成员体验）→ 用户确认 → 存 thinking_pattern → 未来对话时注入，按发起人习惯组织建议
- 复用：对话 Agent 每轮开始注入相关经验（RAG 检索）+ 该发起人的 thinking_pattern

## 8. Skill 机制

- **skill 是什么**：教 AI 怎么思考/做事的 markdown 文件（如"骑行活动筹备思考框架：强度分级 → 补给 → 修车帮扶 → 住宿决策"）
- **必须带元数据头**（frontmatter：`name` / `description` / `when_to_use`）——AI 按当前任务与 description 匹配决定何时加载，避免误用（纯内容文件无触发机制，是无效设计）
- 生成流程：对话中 AI 提议（内容 + 元数据头）→ 用户在对话里审阅 → 确认后落盘到项目 skills 目录
- **review 门槛**：AI 生成的 skill 质量不可控，必须人确认；放 D4（最后）实现，先让经验条目（T6/T7）验证提炼质量

## 9. 数据模型（草案）

```sql
-- 起草会话（多轮消息 + 工具调用 + 表单快照，审计与续聊的事实源）
CREATE TABLE concept_draft_session (
    id            BIGINT PRIMARY KEY,
    concept_id    BIGINT NOT NULL,
    user_id       BIGINT NOT NULL,           -- 发起人
    role          VARCHAR(10) NOT NULL,      -- user/assistant/tool
    content       TEXT,                      -- 消息内容
    tool_name     VARCHAR(50),               -- 工具调用记录
    tool_args     JSONB,
    form_snapshot JSONB,                     -- AI 填表后的表单快照
    tokens_in     INT, tokens_out INT,       -- 成本监控
    created_at    TIMESTAMP NOT NULL DEFAULT now()
);

-- 经验条目（统一经验库：业务知识 + 思考角度）
CREATE TABLE experience_entry (
    id BIGINT PRIMARY KEY,
    club_id BIGINT,                          -- 可空=跨社团通用
    category VARCHAR(30),                    -- thinking_pattern/筹备知识/风险教训/context
    title VARCHAR(100),
    content TEXT,
    owner_id BIGINT,                         -- 思考角度归属的发起人（可空=通用）
    source_concept_id BIGINT,                -- 来源概念（可追溯）
    source_user_id BIGINT,                   -- 沉淀人
    status SMALLINT DEFAULT 1,               -- 1=有效 0=废弃
    created_at TIMESTAMP NOT NULL DEFAULT now()
);
```

concept_session 加一列 `ai_brief TEXT`：**提交时**由 AI 基于完整会话生成一次"发起人想法简析"（活动意义/考虑过的问题/参考过的经验/权衡），冻结在提交时刻——投票人与老师审阅时详情页展示「表单 + 想法简析」。

## 10. 交互细节

1. **表单同步语义**：AI 填表后用户手动改，前端自动把表单变化作为隐式上下文带给 AI（无需用户描述改了什么），也支持口头补充原因；表单是事实源，对话是上下文
2. **会话恢复**：页面刷新/换设备，从 concept_draft_session 重放消息
3. **想法简析展示**：详情页独立"发起人思路"区块（审阅第一眼可见），不入时间线
4. **AI 可选项**：直接填表不对话是合法路径；AI 挂掉（超时/服务不可用）降级为纯手动表单

## 11. 与基座衔接

- 状态机/权限/事务全在 Java；AI 的写圈死在"起草中字段"（T5）
- 提交仍人点；投票/批复流程不动
- 工具调用身份透传：前端 token → Java 代理 → Python Agent → 工具调回 Java 带同一身份 → Java 校验（activity:manage + 社团归属）——**权限判断永远在 Java 业务层**
- 审计：concept_trace 加 `ai_draft` 动作（操作人=发起人，detail=采纳的草案摘要），区分"AI 生成"与"人采纳"

## 12. 技术形态

- Python：FastAPI + LangGraph（对话图 + 方案子图，agent-as-tool）+ LLM API 编排（mimo/DeepSeek，API 替代本地推理）
- Java：代理接口（`/concepts/{id}/ai/chat` 等）+ 会话表管理 + 工具执行端点（update_draft/save_experience 等，鉴权+留痕）
- 超时 30s，超时返回"AI 暂不可用"；限流复用 @RateLimiter（防刷 token 成本）；配置化开关 ai.draft.enabled

## 13. 落地分块

- **D1**：concept_draft_session 表 + Java 代理接口（chat 入口）+ 前端聊天窗（消息流 + 表单联动 + 想法简析展示位）——先用**无工具纯对话**跑通闭环
- **D2**：工具接入（search_experience/get_draft/update_draft 受控写）——AI 真正能填表
- **D3**：经验沉淀（save_experience + thinking_pattern 归纳）+ 提交时想法简析生成
- **D4**：skill 自动生成（generate_skill，带元数据头 + 人工 review）

## 14. 演进注记（取代的旧设计）

| 旧内容 | 位置 | 处理 |
|---|---|---|
| 五、企划建议 Agent（历史经验检索+冷启动两阶段） | 设计讨论.md | 已迁移（本文档 2/6/7 节承接并演进） |
| 十、活动全流程的 LangGraph 编排（2026-08-24 旧方案） | 设计讨论.md | 已清除（技术复审推翻：审批流回归状态机；概念阶段会话起草按本文档） |
| S2 面试素材（活动全流程 LangGraph 编排） | 设计讨论.md | 已清除（基于旧方案，与新设计冲突） |
| 企划助手旧描述（对话窗口+冷启动/非冷启动） | 交接文档-社团管理Agent.md | 已更新为指向本文档 |

**形态修正（2026-08-29 定稿）**：

- **曾设计**：方案 Agent 作为独立子图（agent-as-tool），对话 Agent 委托方案 Agent。
- **修正为**：方案生成是 `generate_draft` 工具（单步），概念阶段无子 Agent；所有多步决策由对话 Agent 主循环承担。
- **理由**：①"要不要补检索/信息够不够"的判断可由主循环吸收，方案生成每次调用都是确定性单步；②子 Agent 代价（双层循环/双套超时状态/排障翻倍/token 翻倍）在概念阶段无对应收益；③判据全项目统一：内部多步自主 → 子 Agent（复盘阶段），确定性单步 → 工具（概念阶段全部）。
- **面试叙事**："评估过方案 Agent 独立子图，判定不需要——多步自主可被主循环吸收；复盘 Agent 才是真子 Agent（内部有真实的自主循环）。同一判据，两种选择。"

> 交互形态演进：2026-08-24 定稿"表单填报优先、AI 建议在后"（简单场景）；2026-08-29 用户明确构思环节是开放性的（强度/住宿/帮扶等维度填表填不出），演进为"对话式构思 + 表单落地"——对话是构思手段，表单仍是唯一产物出口，AI 产出必经人确认。
