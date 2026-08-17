"""
实验执行器（进程内直调，不经 HTTP）。

用法：
  python scripts/run_experiments.py e1       # E1: ritrieve(embedding2) 对照
  python scripts/run_experiments.py e2       # E2: 关闭 rerank 精排对照
  python scripts/run_experiments.py e3-off   # E3: 强制关思考
  python scripts/run_experiments.py e4-off   # E4: 关闭反思
  python scripts/run_experiments.py e5       # E5: 排除 table/image 块（VLM 结构化块价值）
  python scripts/run_experiments.py agent-base  # agent 默认配置基线（带 judge）
  python scripts/run_experiments.py chain    # 单进程顺序跑 agent-base → e3-off → e4-off
"""
import os
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag-python" / "src"))

step = sys.argv[1] if len(sys.argv) > 1 else ""

if step == "e1":
    os.environ["VECTOR_COLUMN"] = "embedding2"   # 必须在 import config 前
    from eval import evaluator
    r = evaluator.run_eval(name="E1-ritrieve-1792")
elif step == "agent-base":
    from eval import evaluator
    r = evaluator.run_eval(name="E-agent-base", engine="agent", with_judge=True)
elif step == "e3-off":
    from eval import evaluator
    from agent.main_graph import experiment_flags
    experiment_flags["force_thinking"] = False
    r = evaluator.run_eval(name="E3-thinking-off", engine="agent", with_judge=True)
elif step == "e4-off":
    from eval import evaluator
    from agent.main_graph import experiment_flags
    experiment_flags["disable_reflect"] = True
    r = evaluator.run_eval(name="E4-reflect-off", engine="agent", with_judge=True)
elif step == "e2":
    from eval import evaluator
    r = evaluator.run_eval(name="E2-no-rerank", use_rerank=False)
elif step == "e5":
    from eval import evaluator
    r = evaluator.run_eval(name="E5-no-vlm-chunks",
                           exclude_types=("table", "image"))
elif step == "chain":
    # 单进程顺序跑全部 agent 系实验（避免 shell 链派生问题）
    import json as _j
    from eval import evaluator
    from agent.main_graph import experiment_flags
    out_dir = ROOT / "rag-python"
    results = {}

    experiment_flags.update({"force_thinking": None, "disable_reflect": False})
    results["agent-base"] = evaluator.run_eval(
        name="E-agent-base", engine="agent", with_judge=True)
    (out_dir / "_experiment_agent-base.json").write_text(
        _j.dumps(results["agent-base"], ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    print(_j.dumps(results["agent-base"], ensure_ascii=False, default=str))

    experiment_flags["force_thinking"] = False
    results["e3-off"] = evaluator.run_eval(
        name="E3-thinking-off", engine="agent", with_judge=True)
    (out_dir / "_experiment_e3-off.json").write_text(
        _j.dumps(results["e3-off"], ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    print(_j.dumps(results["e3-off"], ensure_ascii=False, default=str))

    experiment_flags.update({"force_thinking": None, "disable_reflect": True})
    results["e4-off"] = evaluator.run_eval(
        name="E4-reflect-off", engine="agent", with_judge=True)
    (out_dir / "_experiment_e4-off.json").write_text(
        _j.dumps(results["e4-off"], ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    print(_j.dumps(results["e4-off"], ensure_ascii=False, default=str))
    r = results
else:
    print(__doc__)
    sys.exit(1)

out = ROOT / "rag-python" / f"_experiment_{step}.json"
out.write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str),
               encoding="utf-8")
print(json.dumps(r, ensure_ascii=False, default=str))
