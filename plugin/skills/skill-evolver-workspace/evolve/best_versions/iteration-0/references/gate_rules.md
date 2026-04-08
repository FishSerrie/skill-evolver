# Multi-Gate Decision Rules

## Core Principle

**All Keep conditions must be satisfied simultaneously (AND logic). Any single Discard condition triggers Discard.**

---

## Gate Decision Pseudocode

```python
def gate_decision(current, baseline, policy):
    """
    current:  evaluation results for this iteration
    baseline: evaluation results for the current best version
    policy:   gate threshold configuration
    """
    # Hard failure: crash / timeout
    if current.status in ("crash", "timeout"):
        return "revert"

    # L1 quick gate failed
    if not current.l1_pass:
        return "discard"

    # Multi-gate AND logic
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

    # Change is within noise range — do not risk it
    if abs(current.dev_pass_rate - baseline.dev_pass_rate) < policy.noise_threshold:
        return "discard"

    return "discard"
```

---

## Default Threshold Configuration

| Parameter | Default | Description |
|---|---|---|
| `min_delta` | 0.02 (2%) | Minimum quality improvement required |
| `trigger_tolerance` | 0.05 (5%) | Maximum allowed trigger regression |
| `max_token_increase` | 0.20 (20%) | Maximum allowed token inflation |
| `max_latency_increase` | 0.20 (20%) | Maximum allowed latency inflation |
| `regression_tolerance` | 0.05 (5%) | Maximum allowed regression degradation |
| `noise_threshold` | 0.01 (1%) | Changes below this magnitude are treated as noise |

These thresholds can be overridden by the user in the evolve configuration.

---

## Gate Outcome Summary

| Dimension | Keep Requirement | Discard Trigger | Revert Trigger |
|---|---|---|---|
| Quality | dev_pass_rate >= baseline + min_delta | No improvement or decline | Severe regression |
| Trigger | trigger_f1 >= baseline x 0.95 | Significant degradation | -- |
| Cost | tokens <= baseline x 1.2 | Exceeds threshold | -- |
| Latency | duration <= baseline x 1.2 | Exceeds threshold | -- |
| Regression | regression_pass >= baseline x 0.95 | Significant degradation | -- |
| Runtime | -- | -- | crash / timeout |

---

## Strict Eval Gate (Supplementary)

When Strict Eval is triggered, apply additional checks:

```python
# Holdout must be directionally consistent with dev
holdout_consistent = (
    current.holdout_pass_rate
    >= baseline.holdout_pass_rate - policy.noise_threshold
)

# Dev improved but holdout declined → overfitting signal
if not holdout_consistent:
    return "discard"  # suspected overfitting
```

---

## Anti-Goodhart Protocol

Metric optimization can diverge from actual skill quality. The following safeguards prevent Goodhart's Law from corrupting the evolution process.

### Negative Assertions in Ground Truth

GT should include `not_contains` assertions for critical requirements. Examples:
- The output must NOT hallucinate a specific wrong answer
- The output must NOT include raw template variables
- The output must NOT omit required disclaimers

Negative assertions catch cases where the metric improves while the output degrades in ways the positive assertions do not cover.

### Structural Integrity Checks

After every mutation, verify that structural elements remain intact:
- **Section headers**: No required section headers disappeared from the skill body
- **Scripts**: No helper scripts were deleted (only modified or replaced)
- **References**: No reference files were removed without explicit intent

A mutation that passes the quality gate but silently drops structural components is a false positive.

### Holdout Set Protocol

- The holdout set **MUST** be evaluated before declaring convergence
- A skill that improves on dev but regresses on holdout is overfitting to the dev set
- Convergence requires: dev improvement AND holdout consistency (see Strict Eval Gate above)

### Information Barrier

- **Never expose holdout cases to the proposer** (Phase 2 Ideate)
- The proposer may only see dev set results and execution traces from dev cases
- Holdout cases are visible only to the evaluator (Phase 5) and the gate (Phase 6)
- Leaking holdout information into the search process defeats its purpose as a generalization check
