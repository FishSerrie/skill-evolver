# Multi-Gate Decision Rules

## Core Principle

**All Keep conditions must be satisfied simultaneously (AND logic). Any single Discard condition triggers Discard.**

---

## Gate Decision Pseudocode

This pseudocode mirrors the live implementation in
`scripts/gate.py::phase_6_gate_decision` (also re-exported from
`scripts/evolve_loop.py` for back-compat). Both holdout consistency
and the dev-saturation branch are real code paths, not aspirational.

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

    # ---- Quality gate, with dev-saturation branch ----
    # When baseline dev is already at the ceiling (within noise of 1.0),
    # demanding cur_dev >= base_dev + min_delta is mathematically impossible
    # (pass_rate cannot exceed 1.0). Switch to "dev does not regress AND
    # holdout improved by min_delta". This unblocks iterations whose value
    # lives in holdout improvement (e.g. evaluator/test-framework fixes).
    has_holdout = (
        current.holdout_pass_rate is not None
        and baseline.holdout_pass_rate is not None
    )
    dev_saturated = baseline.dev_pass_rate >= 1.0 - policy.noise_threshold

    if dev_saturated:
        dev_no_regress = (
            current.dev_pass_rate
            >= baseline.dev_pass_rate - policy.noise_threshold
        )
        if has_holdout:
            holdout_improved = (
                current.holdout_pass_rate
                >= baseline.holdout_pass_rate + policy.min_delta
            )
            quality_ok = dev_no_regress and holdout_improved
        else:
            # No holdout signal and dev is saturated — no honest way to
            # judge improvement, so don't risk it.
            quality_ok = False
    else:
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

    # ---- Holdout consistency hard guard (anti-Goodhart) ----
    # A meaningful holdout regression always vetoes a keep, even if dev
    # improved. This is the Strict Eval Gate from the section below,
    # actually wired into the per-iteration decision (not deferred to a
    # separate convergence check).
    holdout_consistent = True
    if has_holdout and (
        current.holdout_pass_rate
        < baseline.holdout_pass_rate - policy.noise_threshold
    ):
        holdout_consistent = False

    if (quality_ok and trigger_ok and cost_ok and latency_ok
            and regression_ok and holdout_consistent):
        return "keep"

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

> **Note:** holdout consistency is now enforced **on every iteration** by the
> main gate above (the `holdout_consistent` block). This section originally
> described a separate convergence-time check; that role is now subsumed by
> the per-iteration guard. Strict Eval still runs additional surfaces
> (regression set, blind A/B) but does not need its own quality logic.

```python
# Per-iteration holdout consistency (already enforced in gate_decision):
holdout_consistent = (
    current.holdout_pass_rate
    >= baseline.holdout_pass_rate - policy.noise_threshold
)
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
