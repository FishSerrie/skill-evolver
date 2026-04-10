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
        current.regression_pass
        >= baseline.regression_pass * (1 - policy.regression_tolerance)
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

---

## L1 Quality Rules (P0 Hard Rules)

Added 2026-04-10, inspired by the skill-qa-workflow project (83 universal
skill quality rules across 7 scan dimensions). These rules run in the L1
Quick Gate (`scripts/run_l1_gate.py`) as regex/grep checks — no LLM needed,
<100ms runtime overhead. They're checked **before the iteration loop starts**
and don't iterate — they're pass/fail prerequisites.

Using the "training model" analogy: **L1 quality rules are the data cleaning
pipeline** (format validation + security scan before training starts). **L2 GT
assertions are the training itself** (iterative optimization toward a target).

| Rule | Severity | What |
|------|----------|------|
| SEC001 | **critical** | No `rm -rf /`, `DROP TABLE/DATABASE` in prompt content |
| SEC003 | **critical** | No hardcoded secrets (`sk-*`, `ghp_*`, `AKIA*`, `password='literal'`) |
| SEC002 | warning | No default `sudo` escalation |
| SEC004 | warning | No `eval()` / `exec()` in prompt content |
| SEC006 | warning | No `curl\|bash` pipe-to-execution |
| S003+ | warning | description >= 50 chars (informative trigger surface) |
| S004+ | warning | SKILL.md body >= 200 chars (substantive instructions) |
| S007 | warning | SKILL.md 20-500 lines (appropriate granularity) |
| TD011 | warning | No hardcoded `localhost:port` / `127.0.0.1` API URLs |
| C001 | warning | No hardcoded `/Users/` / `/home/` absolute paths |
| C005 | warning | UTF-8 encoding, no BOM |

**Severity model**: `critical` = blocks L1 gate (iteration cannot start).
`warning` = logged in `quality_findings` for Phase 2 diagnosis, does not
block. This matches the skill-qa-workflow P0 (hard block) vs P1/P2
(advisory) distinction.

**Scanning scope**: `.md` files get full rule set with code-markup stripping
— both triple-backtick fences and inline backtick spans are removed before
scanning (prevents false positives on documented anti-patterns in rule
tables — e.g. the SEC001/SEC003 examples above would otherwise trigger
the scanner on this very file). `.py`/`.sh` files only scanned for
secrets (SEC003) — other rules would false-positive on the evaluation
framework's own regex patterns and subprocess calls.

---

## Why No 6th Gate Dimension

The current 5-way AND gate measures **relative behavioral change** after
a mutation (did the skill get better or worse?). The skill-qa-workflow's
6-dimension scoring model measures **absolute quality** (does the skill
meet a standard?). These are complementary, not competing:

| Layer | What it measures | How |
|-------|-----------------|-----|
| **L1 Quick Gate** | Absolute quality threshold | Binary pass/fail (quality rules above) |
| **Phase 6 AND Gate** | Relative quality change | 5 dimensions, AND logic (quality + trigger + cost + latency + regression) |
| **L2 GT Probes** | Quality-aware pass_rate | New probes (cases 41-48) add Q001/Q004/UX001/WD001 etc. to the dev-split metric |

Adding a 6th "structural quality" dimension to Phase 6 would mean: even if
pass_rate improved, if structural quality dropped, discard. But:

1. L1 already handles the hard structural floor (P0 rules).
2. L2 GT probes fold quality awareness INTO pass_rate (so quality regressions
   show up as pass_rate regressions on cases 41-48).
3. A separate structural_quality gate metric would need its own threshold
   tuning and could cause false discards on legitimate refactors.

**Decision**: don't add a 6th dimension. If future evidence shows mutations
improving pass_rate while degrading structural quality (despite L1 and L2),
revisit this decision then.

**Future considerations** from skill-qa-workflow worth watching:
- **Challenger concept** (competitive analysis, "justify your divergence") — not in scope now but could become a Phase 2 ideation strategy
- **Fix-type tagging** (`[FIX:SKILL]` vs `[FIX:CLI]`) — useful for distinguishing what the loop can fix autonomously vs what requires engineering
