"""
回归门禁（CI gate）：prompt 变更审批的量化阈值判定。

职责边界（单一职责）：只做"读 delta + 阈值 → 出结论"的纯判定；
不跑评估（evaluator 的职责）、不改任何状态（approval_graph 的职责）。

注意：回归子集样本量小（当前约 15 题），阈值从宽设定；
结论仅供参考，最终决策权仍在审批人（HITL）。
"""
import logging

import config

logger = logging.getLogger("rag.gate")

# 默认阈值：指标环比下降超过该幅度 → gate 不通过（fail-closed 不适用，
# 这里是 advisory gate，机器预判 + 人工最终裁决）
DEFAULT_THRESHOLDS = {
    "context_recall": -0.05,    # 检索召回环比下降 >5pp → 不通过
    "mrr": -0.10,               # 排序质量环比下降 >10pp → 不通过
    "refuse_accuracy": -0.10,   # 拒答准确率环比下降 >10pp → 不通过
}


def _thresholds() -> dict:
    """阈值配置（DEFAULT_THRESHOLDS + 环境变量覆盖 GATE_<KEY>）。"""
    t = dict(DEFAULT_THRESHOLDS)
    for key in t:
        env = config.os.getenv(f"GATE_{key.upper()}", "")
        try:
            if env:
                t[key] = float(env)
        except ValueError:
            logger.warning("非法 GATE_%s=%r，用默认值 %s", key.upper(), env, t[key])
    return t


def check_gate(delta: dict, thresholds: dict | None = None) -> dict:
    """
    判定回归对比 delta 是否通过门禁。

    参数：
      delta: evaluator.compare_runs 的 delta_b_minus_a（{metric: diff}）
      thresholds: 覆盖默认阈值（测试用）
    返回：
      {"passed": bool, "failures": [{"metric", "delta", "threshold"}], "note": str}
    """
    thr = thresholds or _thresholds()
    failures = []
    for metric, limit in thr.items():
        if metric not in delta:
            continue  # 该指标不在对比结果里（如 judge 未开启）→ 不判
        try:
            d = float(delta[metric])
        except (TypeError, ValueError):
            continue
        if d < limit:
            failures.append({"metric": metric, "delta": round(d, 4),
                             "threshold": limit})
    passed = not failures
    note = ""
    if passed:
        note = "门禁通过：无指标环比下降超过阈值"
    else:
        note = f"门禁不通过：{len(failures)} 个指标环比下降超过阈值"
    return {"passed": passed, "failures": failures, "note": note}
