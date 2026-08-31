# 交接文档 — 社团管理 Agent

> 面向后续会话 / 开发者的快速上手文档。读完本文件即可开始工作。
> 最近更新：2026-08-27

---

## 1. 项目定位

**老旧管理系统的 Agent 功能化改造**（独立新项目，不依赖 RAG 项目资产背书）：

- **业务主体**：社团内部管理——活动全生命周期（企划 → 审批 → 公示 → 执行 → 复盘 → 经验沉淀）
- **核心价值**：职责留痕（谁在何时做了什么）+ 经验复用（活动留痕/复盘沉淀为经验库）+ 跨届传承（管理层换届后新管理层通过 RAG 问答继承经验）
- **Agent 定位**：人是主控，Agent 是可选参谋（建议 + 回填，无决策权）

## 2. 仓库结构

**两个仓库**（注意别搞混）：

| 仓库 | 位置 | 内容 |
|---|---|---|
| club-agent（独立 git 仓库） | `c:\Users\lrs\Desktop\py\rag\club-agent\` | 全部代码（java 后端 + frontend 前端） |
| rag 父仓库 | `c:\Users\lrs\Desktop\py\rag\` | 设计文档（docs/），club-agent 目录未纳入版本管理 |

club-agent 提交历史：
```
c860aba fix: 生产级补强——雪花ID序列化/审批CAS/防重key粒度化/限流防重/分页上限/回归34项
6071568 feat: 社团与成员块——@ClubPermission 权限/创建/申请/审批/任命/离职 + 前端三页
8474700 feat: 基础配置块——数据模型/RBAC/认证(验证码+JWT+防爆破)/个人信息(COS存储抽象)/双日志
e9116fc chore: 工程初始化——Maven/多环境/fail-fast/健康检查/启动脚本
```

## 3. 技术栈

**后端** `club-agent/java`：Spring Boot 3.5.16 + Java 17
- Spring Security + JWT（jjwt 0.12.6）、MyBatis-Plus 3.5.7、PostgreSQL、Redis
- hutool-captcha 验证码、knife4j 4.5.0 API 文档、actuator 健康检查
- 腾讯云 COS SDK（存储抽象：local / cos 可切换）
- 端口 **8093**，多环境（dev/prod），密钥走环境变量 + ConfigValidator fail-fast

**前端** `club-agent/frontend`：Vue 3.4 + Vite 5 + Element Plus 2.7 + Pinia + Vue Router 4 + axios，端口 **5174**

**AI 部分（规划中，未实现）**：Python FastAPI + LangGraph（独立进程，后续接入）；LLM 走 API（DeepSeek/MiMo），复用 RAG 项目检索基建（历史留痕/经验条目为检索源）

## 4. 已完成功能（3 块 + 补强轮）

**基础配置块**（8474700）：登录注册（验证码 + 防爆破锁定 + 登录日志）、JWT 认证、RBAC 权限模型、个人信息（头像存储抽象）、操作日志、预设老师账号（DataInitializer 幂等写入）

**社团与成员块**（6071568）：社团创建/列表/详情、成员申请/审批/拒绝、管理层任命/离职（换届模型）、@ClubPermission 社团上下文权限、跨社团管理层唯一约束、我的社团

**生产级补强轮**（c860aba）：
- 雪花 ID 前端精度：VO 的 Long id 加 `@JsonSerialize(using = ToStringSerializer.class)`
- 审批 CAS 条件更新（`WHERE status = 待处理` 更新，rows==0 抛"已被处理"）
- 防重 key 粒度化：`方法 + 用户 + IP + 参数`
- 写接口 @RateLimiter + @RepeatSubmit；分页 @Min(1)/@Max(100)
- 回归 34 项通过（越权/并发/数据污染清理）

**前端页面**：Login / Register / Profile / ClubList / ClubDetail / MyClubs

## 5. 数据库模型（库名 club_agent，init_db.sql 建表）

| 表 | 说明 |
|---|---|
| sys_user | 全局用户（is_teacher 标记老师；status 禁用位） |
| club | 社团（teacher_id 指导老师，可管多社团） |
| membership | 成员关系（user_id + club_id 唯一；status 申请/通过/拒绝；role_id 引用角色） |
| rbac_role | 动态角色（code/is_management 标记，非枚举） |
| rbac_permission | 权限点（MENU/BUTTON/ACTION） |
| rbac_role_permission | 角色-权限多对多 |
| oper_log / login_log | 操作日志 / 登录日志（审计） |

**关键约束**（触发器 `fn_check_management_unique` 数据库层兜底）：
1. 跨社团唯一：一人不能同时任多个社团管理层
2. 一社团一社长（president 槽位唯一）
3. 副社长最多 2 人

**规范**：雪花 ID（普通 BIGINT，不用自增）、逻辑删除（deleted 0/1）、命名（sys_/rbac_ 前缀，业务表避开 PG 保留字）、时间字段 created_at/updated_at（TIMESTAMP，时区 GMT+8）

## 6. 关键工程模式（面试可讲 / 新功能照抄）

- **@ClubPermission 切面**：方法注解 + 社团上下文解析（URL 路径 / DTO 字段取 clubId），权限码校验（club:member:approve 风格）
- **CAS 条件更新**：状态流转类更新一律带条件 WHERE + 影响行数判定，防并发覆盖
- **防重 / 限流注解**：@RepeatSubmit（防重 key=方法+用户+IP+参数）、@RateLimiter（Redis 滑动窗口/计数器）
- **存储抽象**：StorageService 接口 + Local/Cos 双实现，`storage.mode` 配置切换，COS 模式 fail-fast 校验密钥
- **fail-fast 配置校验**：ConfigValidator 启动时校验密钥/必配项，缺失拒绝启动
- **DataInitializer**：角色/权限/预设老师账号幂等初始化（BCrypt 运行时加密，不落明文）
- **VO 雪花 ID 序列化**：所有返回前端的 Long id 必须 ToStringSerializer（JS Number 精度陷阱）

## 7. 设计文档索引（父仓库 docs/）

- `docs/社团管理Agent-活动流程设计.md`：**业务流程事实源**。第一章活动前（企划→双人投票→复议→老师批复→三条作废）、第二章 2.1 公示（企划公布→问卷→讨论群→正式文件）、2.2 分工（职责项模型）、2.3 执行阶段（报名/签到/留痕/奖励/两层结束）；第三章活动后待讨论
- `docs/agent设计-概念阶段.md`：**概念阶段（活动发起/起草）Agent 设计唯一事实源**（2026-08-29 起）——单对话 Agent（LangGraph ReAct）+ 9 工具集（含 generate_draft 方案生成工具）+ 统一经验库（含思考角度归纳）+ skill 机制 + 落地分块 D1-D4；**形态修正定稿：概念阶段无子 Agent**（子 Agent 留给复盘阶段）
- `docs/社团管理Agent-设计讨论.md`：通用设计决策 + 素材归档（S1-S9）+ 待定项（含双系统逻辑 JOIN 设想）；其中概念阶段 Agent 相关旧章节（五/十/S2）已清除并标注迁移

**术语**：企划（发起人填写的 时间/地点/大致内容，进审批链）→ 正式文件（公示讨论后含分工）。"初稿"已废弃（文档改 4 处待用户裁决）。

## 8. 协作模式与铁律（用户明确要求，违反会严重影响体验）

1. **动手前必须确认**：任何实质性操作（建文件/改代码/改文档/跑命令）前，先列清单经用户确认；业务讨论 ≠ 实施授权（已踩过"擅自改流程文档"的坑）
2. **小块推进 + 每块讨论**：禁止一次写完整个框架；每块实现后解释关键点，用户消化后再下一块（近期用户对 Agent 设计改为"直接动手看成果"，但动手范围必须先确认）
3. **严禁简化**：不得以"规模小/用不上/成本高"为由砍功能或简化设计；流程类简化必须向用户说明差别由用户拍板
4. **不用 AskUserQuestion**：用自然对话确认；用户反感选择题式追问
5. **不要主动教面试话术**：用户主动问面试相关时再谈
6. **PowerShell 环境**：无 `&&` 分隔符（用 `;`）；中文脚本注意编码（BOM/UTF-8）

## 9. 下一步计划（迭代3 活动与留痕）

**本次编码（已确认范围）**：企划（初稿）部分
- 发起页面：企划表单（预计时间 / 预计地点 / 活动大致内容，三项必填，无名称字段）
- 企划助手（起草 Agent）：**设计见 `docs/agent设计-概念阶段.md`**（对话式构思 + 表单落地，D1 起实现）——当前已落地的基座为表单 + 提交审批链，Agent 对话层按 D1-D4 分块推进
- 提交后进审批链（审批功能不在本次）

**后续**：审批链（已落地：投票/复议/作废/通知/老师批复）→ 概念 Agent 起草层（D1 会话表+聊天接口 → D2 工具接入 → D3 经验沉淀+想法简析 → D4 skill 生成，见 `docs/agent设计-概念阶段.md`）→ 公示 + 问卷 + 讨论群（WebSocket）→ 正式文件 + 分工 → 报名/签到/执行留痕/打分/等级 → 复盘/经验提炼/打分建议/RAG 问答

**技术提示**：企划表设计沿用现有规范（雪花 ID/逻辑删除/时间字段）；企划助手 LLM 走 API（Python 侧），Java 侧只做会话代理；历史检索源（活动留痕 + 复盘 + 经验条目）依赖后续环节落库，冷启动阶段无检索数据

## 10. 环境与启动

- 数据库：PostgreSQL（库 club_agent），Redis 127.0.0.1:6379
- 环境变量：JWT_SECRET、SPRING_DATASOURCE_URL/USERNAME/PASSWORD、STORAGE_MODE 等（见 application*.yml，全部 `${ENV}` 引用）；`.env` 在 club-agent/ 根（Spring 不读 .env，必须经脚本注入）
- 后端启动：`powershell -File club-agent\java\scripts\start-dev.ps1`（注入 .env 后 spring-boot:run）
- 前端启动：`club-agent\frontend` 下 `npm run dev`（5174）
- API 文档：knife4j（后端 8093 起后访问）

## 11. 待裁决事项（当前挂起）

1. 活动流程文档 4 处"初稿→企划"修改：保留 or 还原（用户裁决中）
2. ~~是否新建 `docs/社团管理Agent-Agent设计.md`~~（2026-08-29 已解决：新建 `docs/agent设计-概念阶段.md`，概念阶段 Agent 设计唯一事实源）
3. 企划助手的 Python 侧实现位置（club-agent 内新建 python 模块 vs 独立目录）
