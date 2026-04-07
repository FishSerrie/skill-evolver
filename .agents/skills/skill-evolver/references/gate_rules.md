# 多门控规则

## 核心原则

**所有 Keep 条件必须同时满足（AND 逻辑）。任一 Discard 条件触发即 Discard。**

---

## 门控判定伪代码

```python
def gate_decision(current, baseline, policy):
    """
    current: 本轮评测结果
    baseline: 当前 best version 的评测结果
    policy: 门控阈值配置
    """
    # 硬性失败：crash / timeout
    if current.status in ("crash", "timeout"):
        return "revert"

    # L1 快速门卫未通过
    if not current.l1_pass:
        return "discard"

    # 多门控 AND 逻辑
    quality_ok = (
        current.dev_pass_rate
        >= baseline.dev_pass_rate + policy.min_delta
    )
    trigger_ok = (
        current.trigger_f1
        >= baseline.trigger_f1 * (1 - policy.trigger_tolerance)
    )
    cost_ok = (
        current.tokens_mean
        <= baseline.tokens_mean * (1 + policy.max_token_increase)
    )
    latency_ok = (
        current.duration_mean
        <= baseline.duration_mean * (1 + policy.max_latency_increase)
    )
    regression_ok = (
        current.regression_pass_rate
        >= baseline.regression_pass_rate * (1 - policy.regression_tolerance)
    )

    if quality_ok and trigger_ok and cost_ok and latency_ok and regression_ok:
        return "keep"

    # 变化不显著（噪声范围内）→ 不冒险
    if abs(current.dev_pass_rate - baseline.dev_pass_rate) < policy.noise_threshold:
        return "discard"

    return "discard"
```

---

## 默认阈值配置

| 参数 | 默认值 | 说明 |
|---|---|---|
| `min_delta` | 0.02 (2%) | 质量最小提升幅度 |
| `trigger_tolerance` | 0.05 (5%) | trigger 允许的最大退化 |
| `max_token_increase` | 0.20 (20%) | token 允许的最大膨胀 |
| `max_latency_increase` | 0.20 (20%) | 时延允许的最大膨胀 |
| `regression_tolerance` | 0.05 (5%) | regression 允许的最大退化 |
| `noise_threshold` | 0.01 (1%) | 低于此变化量视为噪声 |

这些阈值可由用户在 evolve 配置中覆盖。

---

## 门控结果汇总表

| 条件 | Keep 要求 | Discard 触发 | Revert 触发 |
|---|---|---|---|
| 质量 | dev_pass_rate ≥ baseline + min_delta | 无提升或下降 | 大幅退化 |
| 触发 | trigger_f1 ≥ baseline × 0.95 | 明显恶化 | — |
| 成本 | tokens ≤ baseline × 1.2 | 超阈值 | — |
| 时延 | duration ≤ baseline × 1.2 | 超阈值 | — |
| 回归 | regression_pass ≥ baseline × 0.95 | 明显退化 | — |
| 运行 | — | — | crash / timeout |

---

## L3 门控（补充）

当触发 L3 严格评测时，额外检查：

```python
# holdout 必须和 dev 方向一致
holdout_consistent = (
    current.holdout_pass_rate
    >= baseline.holdout_pass_rate - policy.noise_threshold
)

# 如果 dev 涨了但 holdout 跌了 → 过拟合信号
if not holdout_consistent:
    return "discard"  # 疑似过拟合
```
