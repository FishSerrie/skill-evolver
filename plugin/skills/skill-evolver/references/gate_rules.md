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
            # If holdout is ALSO already saturated, demanding
            # holdout_improved below is equally impossible — degrade one
            # more step to "no regression on either split" instead of
            # "improve the unimprovable" (real bug found via a live
            # evolve run: this used to make "keep" permanently
            # unreachable once both splits hit the ceiling, including
            # for safe, no-regression doc/slimming fixes this skill's
            # own evolve_plan.md recommends once saturated).
            holdout_saturated = (
                baseline.holdout_pass_rate >= 1.0 - policy.noise_threshold
            )
            if holdout_saturated:
                holdout_no_regress = (
                    current.holdout_pass_rate
                    >= baseline.holdout_pass_rate - policy.noise_threshold
                )
                quality_ok = dev_no_regress and holdout_no_regress
            else:
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
    # base == 0 is not a "skip the check" signal — it means there is no
    # baseline cost/latency signal to compare against (e.g. an evaluator
    # that honestly reports zero tokens, not an estimate). Unconditionally
    # passing here used to silently disable cost/latency gating whenever
    # that happened (real bug found via adversarial review) — require the
    # candidate to also report zero rather than accepting any value.
    cost_ok = (
        current.tokens_mean == 0
        if baseline.tokens_mean == 0 else
        current.tokens_mean <= baseline.tokens_mean * (1 + policy.max_token_increase)
    )
    latency_ok = (
        current.duration_mean == 0
        if baseline.duration_mean == 0 else
        current.duration_mean <= baseline.duration_mean * (1 + policy.max_latency_increase)
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

    # ---- Structural budget and per-dimension metric floors ----
    # Each delegates to its own function so the rule can be read and tested
    # on its own; the decision below only combines verdicts.
    structure_ok, _ = check_structure(
        current.snapshot, baseline.snapshot, policy
    )
    metrics_ok, _ = check_metric_thresholds(current, baseline, policy)

    if (quality_ok and trigger_ok and cost_ok and latency_ok
            and regression_ok and holdout_consistent
            and structure_ok and metrics_ok):
        return "keep"

    return "discard"
```

**Structural budget** (`check_structure`): reads only the keys every Target
shape reports — `chars` / `lines` / `non_empty_lines` / `child_units` /
`child_lines` — so the gate never has to ask what kind of artifact it is
looking at. A key-presence test such as `if "share_of_file" in snapshot`
would be a type check wearing a dictionary lookup as a disguise;
shape-specific detail lives under `extra` and is deliberately not read.

An optimizer left unchecked reliably grows its subject — every iteration can
justify one more clause — and the result scores well on the metric while
becoming unusable. Evaluation of hand-written instruction sets found focused
ones outperforming exhaustive ones. Growth must be paid for by measured
improvement (the quality gate); this gate caps how much may be spent.
Because `child_lines` is counted, moving bulk out of the entry point into
reference files does not evade it.

**Per-dimension metric floors** (`check_metric_thresholds`): precision and
recall diagnose different failures — one means content was invented, the
other that expectations were missed — so each gets its own bar. Ranking on a
single blended number cannot express "never let precision fall below 0.9
however good recall gets". A floor named on a metric the evaluator does not
report is itself a failure, because a floor believed active but never
evaluated is worse than no floor at all.

**Note on the trigger gate:** with no trigger data on either side, both
values default to 1.0 and `trigger_ok` is unconditionally true. That is now
recorded explicitly in the reasons as `trigger not evaluated`, because a
gate silently doing nothing is indistinguishable in the log from a gate that
examined the data and approved.

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
| `max_structure_growth` | 0.25 (25%) | Maximum growth per structural dimension |
| `max_structure` | `{}` | Absolute caps per dimension, e.g. `{"lines": 200, "child_units": 3}` |
| `min_metrics` | `{}` | Absolute floors per metric, e.g. `{"precision": 0.9}` |
| `max_metric_regression` | `{}` | Per-metric tolerance against baseline, e.g. `{"precision": 0.05}` |

These thresholds can be overridden by the user in the evolve configuration.
The four structural and metric entries default to empty or permissive, so
adding these gates changes no existing verdict until they are configured.

---

## Gate Outcome Summary

| Dimension | Keep Requirement | Discard Trigger | Revert Trigger |
|---|---|---|---|
| Quality | dev_pass_rate >= baseline + min_delta | No improvement or decline | Severe regression |
| Trigger | trigger_f1 >= baseline x 0.95 | Significant degradation | -- |
| Cost | tokens <= baseline x 1.2 | Exceeds threshold | -- |
| Latency | duration <= baseline x 1.2 | Exceeds threshold | -- |
| Regression | regression_pass >= baseline x 0.95 | Significant degradation | -- |
| Structure | every dimension <= baseline x 1.25 and under its cap | Candidate grew | -- |
| Metrics | every named dimension >= its floor and within its regression tolerance | A dimension traded away | -- |
| Runtime | -- | -- | crash / timeout |

---

## Strict Eval Gate (Supplementary)

Holdout consistency is now enforced **on every iteration** by the main
gate above (the `holdout_consistent` block in the pseudocode) — this
section used to describe a separate convergence-time check with its
own copy of that same logic; that copy has been removed since it only
ever restated the per-iteration guard verbatim. Strict Eval's actual
remaining job is running the additional surfaces the per-iteration
gate doesn't touch — regression set, optional blind A/B (see
`references/evolve_protocol.md`'s Phase 5 "Strict Eval" section) — not
quality logic of its own.

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

A mutation that passes the quality gate but silently drops structural
components (a section header, a helper script, a reference file) is a
false positive. This is one of the three suspicion angles the
Adversarial Review Panel's `structural` checker enforces with real
code, not just documented convention — see that section below for the
concrete checklist; it's the authoritative version, not restated here.

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

## Adversarial Review Panel (Phase 6.5)

> **Note:** the Structural Integrity Checks above and part of the overfitting concern this section opens with are now additionally checked by real code, not just documented convention — via the panel below. `gate.py::phase_6_gate_decision` itself is unchanged (still a pure 5-dimension numeric AND gate); the panel runs as a separate Phase 6.5, after a candidate already passed that numeric gate and before it is actually treated as a keep.

Implemented in `scripts/verifier_panel.py` (isolation mechanism + aggregation) + `scripts/llm.py::phase_6_5_review` (CLI-mode wiring) + `scripts/orchestrator.py` (insertion point — between the Phase 6 decision and the keep/discard branch; see `references/evolve_protocol.md` Phase 6.5 for the conversation-mode path).

### Three independent checkers

Each checker is one independent Agent tool call (in-conversation mode) or one independent `_call_claude` subprocess (CLI mode) — the same physical isolation mechanism Module B's diagnoser/mutator split uses (`isolation.py`), applied to a new role. None of the three ever sees the proposer's own description of its change — `verifier_panel.build_verifier_task_spec` has no `diagnosis`/`description` parameter, only the diff and the numeric metrics the gate already computed.

| Checker | Suspicion angle | Given |
|---|---|---|
| `overfit` | dev improved but holdout flat/regressed; unreasonable case-specific hardcoding | diff + dev/holdout pass_rate (current vs. baseline) |
| `assertion_gaming` | literal strings stuffed into unrelated text to satisfy `contains`; synonym swaps to dodge `not_contains` | diff + metrics (deliberately NOT the proposer's description) |
| `structural` | missing section headers, deleted helper scripts, removed reference files without indication of intent | diff + skill file tree |

### Aggregation rule

Implemented in `verifier_panel.py::aggregate_verdicts` — deliberately conservative, because a false reject costs one more iteration while a false pass risks a gamed/overfit/structurally-broken candidate reaching main:

- **`structural` gets an independent veto**: a real `reject` from the `structural` checker rejects the candidate immediately, regardless of what the other two checkers say — checked before anything else, including the error-count branches below. Found via a live red-team round: a genuine structural violation (a required section silently deleted, no indication of intent) got correctly rejected by `structural`, but `overfit`/`assertion_gaming` correctly said `pass` from their own narrow angles — a "pure" single-category violation can only ever be seen by the one checker whose lens covers it, so the old ">=2/3" majority rule could never reach 2 votes no matter how real the problem was. `structural` is different in kind from the other two anyway (its own suspicion text: "the one checker that does not require guessing at the proposer's motive... not a check of intent") — a near-objective consistency check, not a probabilistic judgment call, so a majority-vote discount doesn't apply the same way to it.
- Otherwise, among `overfit`/`assertion_gaming` (and a non-rejecting `structural`): all 3 return a real verdict (no failed call) → **>= 2 `reject` vetoes** the candidate.
- Exactly 1 verifier call fails: degrade to the other 2's verdicts — agreement wins; disagreement (1 pass, 1 reject) defaults to `reject`.
- **>= 2 verifier calls fail**: the panel itself is unusable this round. Result is `"skipped"`, which falls back to the numeric gate's own decision — but the skip is recorded explicitly as `adversarial_review: {"decision": "skipped", ...}` in experiments.jsonl, never silently treated as a pass.

### Known residual risk (not solved by majority vote)

Majority vote assumes the 3 verifiers' judgments are independent. If they share enough model/prompt similarity to be correlated rather than genuinely independent, a proposer that learns to satisfy 2/3 could still slip through — the three distinct suspicion angles reduce this correlation but do not guarantee independence. This is a known limitation, not a solved problem.

Darwin-Gödel Machine (arXiv 2505.22954) is the concrete cautionary case for why this panel cannot be oversold as unfoolable: it found a branch that satisfied its evaluation function by deleting the log markers that function checked for, rather than fixing the underlying bug ("objective hacking"). Per that lesson, the `assertion_gaming` checker's specific detection heuristics live only inside `verifier_panel.py`, never in any file the mutator's Agent call has read access to.

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
