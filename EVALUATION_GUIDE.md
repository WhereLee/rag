# RAG 系统评估执行指南

## 概述

本文档用于指导其他 agent 执行 RAG 系统的评估实验，并将结果返回给主 agent。

## 当前状态

- **Run 16 (E-agent-base)**: ✅ 已完成，指标优秀
  - MRR: 0.9167
  - Context Recall: 1.0 (100%)
  - Refuse Accuracy: 1.0 (100%)
  
- **Run 17 (E3-thinking-off)**: 🔄 正在运行（已启动）

- **Run 18 (E4-reflect-off)**: ⏳ 待运行（chain 模式自动执行）

## 需要执行的实验

### 1. 等待当前 chain 完成

当前有一个 `chain` 模式的实验正在运行（单进程顺序执行）：
- Run 17: E3-thinking-off（关闭思考模式）
- Run 18: E4-reflect-off（关闭反思）

**检查命令：**
```powershell
cd c:\Users\lrs\Desktop\py\rag\scripts
python _status2.py
```

或查看最新 eval_run：
```powershell
cd c:\Users\lrs\Desktop\py\rag
python -c "import sys; sys.path.insert(0, 'rag-python/src'); from db import pg_store; rows = pg_store.query('SELECT id, name, metrics FROM eval_run ORDER BY id DESC LIMIT 3'); [print(r) for r in rows]"
```

### 2. 补充实验（可选）

如果时间允许，可以运行以下补充实验：

**E1: ritrieve 向量模型对照**
```powershell
cd c:\Users\lrs\Desktop\py\rag
python scripts/run_experiments.py e1
```
预期耗时：~20 分钟

**E2: 关闭 rerank 精排对照**
```powershell
cd c:\Users\lrs\Desktop\py\rag
python scripts/run_experiments.py e2
```
预期耗时：~15 分钟

**E5: 排除 VLM 结构化块（table/image）**
```powershell
cd c:\Users\lrs\Desktop\py\rag
python scripts/run_experiments.py e5
```
预期耗时：~20 分钟

## 如何查看结果

### 查看单个实验结果

```python
# scripts/_view_run.py
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rag-python" / "src"))
from db import pg_store

run_id = 17  # 改成要查看的 run_id
run = pg_store.query_one("SELECT * FROM eval_run WHERE id=%s", (run_id,))
print(f"=== Run {run_id}: {run['name']} ===")
print(json.dumps(run["metrics"], indent=2, ensure_ascii=False))
```

### 对比两个实验

```python
# scripts/_compare_runs.py
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rag-python" / "src"))
from eval import evaluator

run_a = 16  # E-agent-base
run_b = 17  # E3-thinking-off
result = evaluator.compare_runs(run_a, run_b)
print(json.dumps(result, indent=2, ensure_ascii=False))
```

### 查看 guard 误拒数

```python
# scripts/_check_guard.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rag-python" / "src"))
from db import pg_store

run_id = 17
n = pg_store.query_one(
    "SELECT count(*) n FROM eval_result WHERE run_id=%s "
    "AND answer LIKE '%%超出了当前知识库%%'", (run_id,))["n"]
total = pg_store.query_one(
    "SELECT count(*) n FROM eval_result WHERE run_id=%s", (run_id,))["n"]
print(f"run{run_id} guard: {n} / {total}")
```

## 结果报告格式

请按以下格式返回结果：

```markdown
## 评估结果报告

### 实验完成情况
- [ ] Run 17 (E3-thinking-off): 完成/未完成
- [ ] Run 18 (E4-reflect-off): 完成/未完成
- [ ] E1 (ritrieve 对照): 完成/未完成/跳过
- [ ] E2 (no-rerank 对照): 完成/未完成/跳过
- [ ] E5 (no-vlm-chunks): 完成/未完成/跳过

### 关键指标对比

| 实验 | MRR | Context Recall | Refuse Accuracy | Guard 误拒 |
|------|-----|----------------|-----------------|-----------|
| E-agent-base (run16) | 0.9167 | 1.0 | 1.0 | 4/40 |
| E3-thinking-off (run17) | ? | ? | ? | ?/? |
| E4-reflect-off (run18) | ? | ? | ? | ?/? |

### 结论
- 思考模式的影响：[提升/降低/无明显变化]
- 反思机制的影响：[提升/降低/无明显变化]
- 建议：[保留/关闭/优化]
```

## 注意事项

1. **不要中断正在运行的实验**：如果 Run 17/18 正在运行，等待完成即可
2. **串行执行**：评估器是串行的，40 题约 80-120 分钟
3. **LLM 成本**：每个实验会调用 MiMo API 约 40-80 次（含 judge）
4. **环境依赖**：需要 PostgreSQL、Redis、MiMo API 可用

## 快速检查清单

执行前确认：
- [ ] PostgreSQL 运行中（`pg_isready`）
- [ ] Redis 运行中（`redis-cli ping`）
- [ ] MiMo API 可达（检查 `.env` 配置）
- [ ] 当前无其他实验进程在跑（`tasklist | findstr python`）

## 联系

如有疑问，返回结果时一并说明。
