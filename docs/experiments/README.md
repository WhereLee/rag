# 实验报告索引

所有实验由 `scripts/run_experiments.py` 进程内直调执行，40 题黄金集（factual 17 / table 8 / cross_page 7 / refuse 8），
指标落 `eval_run` 表。基线 run 1 之后各实验 run id 见表内。

## 结果汇总

| 实验 | run | 变量 | context_recall | mrr | refuse_acc | judge(faith/relev) |
|---|---|---|---|---|---|---|
| 基线 | 1 | bge-base 768 + rerank + 基础管线 | 1.0 | 0.9167 | 1.0 | — |
| E1 | 6 | 换 ritrieve-zh 1792 维（embedding2 列） | 1.0 | 0.9141 | 1.0 | — |
| E2 | 9 | 关闭 rerank 精排（仅 RRF 融合） | 1.0 | **0.3998** | 1.0 | — |
| E5 | 8 | 检索排除 table/image 块 | 1.0 | 0.9479 | 1.0 | — |
| agent-base | 7 | LangGraph 默认配置（带 judge） | 待实验链完成 | | | |
| E3 | — | force_thinking=False | 待实验链完成 | | | |
| E4 | — | disable_reflect=True | 待实验链完成 | | | |

## E1：embedding 模型对照（run 6）

- 变量：查询/存储向量从 `embedding`（bge-base-zh-v1.5, 768 维）切到 `embedding2`（ritrieve-zh-v1, 1792 维），
  查询编码器经 `COLUMN_EMBEDDER` 自动配对，避免维度不匹配。
- 结果：recall 均 1.0；总 MRR 0.9141 vs 基线 0.9167（-0.0026）；table 维度 0.8438 vs 0.8542。
- 结论：该语料规模（420 chunks）下两者检索质量几乎持平，bge-base 略优且推理成本约低一半
  （768 vs 1792 维、模型更小）。生产默认保留 bge-base；ritrieve 列保留作为大语料/长文本场景的
  AB 切换能力（环境变量 `VECTOR_COLUMN=embedding2` 即可）。

## E2：rerank 精排价值量化（run 9）

- 变量：`use_rerank=False`，即只用向量+BM25 的 RRF 融合排序，跳过 bge-reranker-v2-m3 精排。
- 结果：recall 保持 1.0（候选池未变），但总 MRR 从 0.9167 崩到 **0.3998**（-0.517）；
  分维度：factual 0.9706→0.4147，table 0.8542→0.3387，cross_page 0.8571→0.4333。
- 结论：rerank 是本系统排序质量的最大单点贡献者——证据块几乎总在 top-40 候选里（recall 不变），
  但没有交叉编码器精排时平均要排到第 3-4 位才命中。这也解释了为什么 CPU 环境下仍值得保留
  reranker（信号量 1 并发 + 超时降级兼顾延迟与质量）。
- 备注：原计划的 v2-gte reranker 模型对照不可行——ModelScope 可达但仅有 LLM 型 reranker
  （minicpm-layerwise/gemma，2B+ 参数），纯 CPU 16GB 环境无法承载；本机仅有
  bge-reranker-v2-m3。故 E2 改为更有价值的“精排开/关”对照。

## E5：VLM 结构化块价值量化（run 8）

- 变量：检索层 `exclude_types=("table","image")`，即排除 B/C/IMG 通道产出的 VLM 块，只留 text 块。
- 结果：recall 仍 1.0，总 MRR 0.9479（较基线 +0.031），table 维度 MRR 0.9167 vs 基线 0.8542（+0.0625）。
- 命中块类型分析（`scripts/_analyze_e5.py`）：table/cross_page 维度全部命中 text 块。
- 结论（如实记录）：在当前语料下，VLM 结构化块与 text 块内容同源（白皮书 B 通道页同时产出
  直提 text 块与 VLM 表格块），排除 VLM 块不损失召回，甚至因候选池变小提升了排名。
  VLM 通道的价值在**扫描件（C 通道，无直提文本）**场景才体现——语料中扫描通知题
  （factual 维度）的答案只能来自 VLM 转录块，该题在 E5 中仍命中是因为其证据关键词同时
  出现在 C 通道转录生成的 text 类型块里。
- 改进方向：若语料含大量"纯表格扫描件"（表格信息仅在 VLM 结构化块、无正文复述），
  E5 的 recall 才会显著下降，届时才能量化出 VLM 块的净增量。当前结论不夸大。

## E3 / E4 / agent-base

由实验链（terminal 后台）执行，均 engine=agent + LLM-as-judge。完成后回填本表并附结论。

（原 E2 留档说明已并入上方 E2 节备注。）
