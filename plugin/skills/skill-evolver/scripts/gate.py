#!/usr/bin/env python3
"""Phase 6: Multi-gate decision logic.

Extracted from evolve_loop.py to keep the gate rules reviewable in
isolation. The implementation mirrors the pseudocode documented in
``references/gate_rules.md``; any change here must be reflected there
(and vice versa). The gate is pure — no I/O, no subprocess, just a
deterministic function from (current_metrics, baseline_metrics,
thresholds) to {"decision", "reasons"}.
"""

from __future__ import annotations

# Structural keys every target shape reports, in the order a gate reads
# them. Imported rather than restated so there is one definition of the
# snapshot contract; a local copy would drift the moment a shape changed
# what it measures.
try:  # pragma: no cover - exercised by whichever import path is live
    from target import SNAPSHOT_KEYS
except ImportError:  # pragma: no cover - gate.py used standalone
    SNAPSHOT_KEYS = (
        "chars", "lines", "non_empty_lines", "child_units", "child_lines",
    )


def check_structure(current_snapshot: dict | None,
                    baseline_snapshot: dict | None,
                    thresholds: dict | None = None) -> tuple[bool, list[str]]:
    """Whether a candidate stayed within its size budget.

    Returns ``(ok, reasons)``. Both snapshots absent means no structural
    signal exists, which is a pass — a gate that failed for lack of data
    would block every run that does not collect it.

    Reads only the keys in :data:`SNAPSHOT_KEYS`, which every target shape
    reports. That is what keeps this free of type branching: testing
    whether a key is present (``if "share_of_file" in snap``) would be a
    check on which kind of artifact this is, wearing a dictionary lookup as
    a disguise, and the point of the uniform contract is that the gate
    never has to ask.

    Why a size budget at all: evaluation of hand-written instruction sets
    found focused ones outperforming exhaustive ones, and an optimizer left
    unchecked reliably grows its subject — every iteration can justify one
    more clause, and the result scores well on the metric while becoming
    unusable. Growth has to be paid for by measured improvement, which the
    quality gate handles; this gate caps how much can be spent.
    """
    th = thresholds or {}
    max_growth = th.get("max_structure_growth", 0.25)
    absolute_caps = th.get("max_structure", {}) or {}

    reasons: list[str] = []
    if not current_snapshot:
        return True, reasons

    for key in SNAPSHOT_KEYS:
        value = current_snapshot.get(key)
        if not isinstance(value, (int, float)):
            continue

        cap = absolute_caps.get(key)
        if cap is not None and value > cap:
            reasons.append(f"structure FAIL: {key} {value} > cap {cap}")

        if not baseline_snapshot:
            continue
        base = baseline_snapshot.get(key)
        if not isinstance(base, (int, float)) or base <= 0:
            # No baseline to grow relative to. A zero baseline means the
            # dimension did not exist before, and calling any first
            # appearance an unbounded increase would reject a candidate for
            # adding its first reference file.
            continue
        if value > base * (1 + max_growth):
            reasons.append(
                f"structure FAIL: {key} {value} > {base} * {1 + max_growth:.2f}"
            )

    return not reasons, reasons


def check_metric_thresholds(current_metrics: dict, baseline_metrics: dict,
                            thresholds: dict | None = None
                            ) -> tuple[bool, list[str]]:
    """Whether each named metric dimension cleared its own floor.

    Returns ``(ok, reasons)``. Precision and recall diagnose different
    failures — one means content was invented, the other that expectations
    were missed — so they get independent floors. Ranking on a single
    blended number cannot express "never let precision fall below 0.9
    however good recall gets", which is exactly the constraint a
    rule-dense task needs.

    Two kinds of bound, because they answer different questions:

    - ``min_metrics``: absolute floors. "Never ship below this."
    - ``max_metric_regression``: per-dimension tolerance against the
      baseline. Catches a candidate that trades one dimension away for
      another while the primary number improves — the trade the primary
      metric is designed not to notice.

    Dimensions the caller did not name are not checked. Inventing a
    default floor would silently reject candidates against a bar nobody
    set.
    """
    th = thresholds or {}
    floors = th.get("min_metrics", {}) or {}
    max_regression = th.get("max_metric_regression", {}) or {}

    current = current_metrics.get("metrics") or {}
    baseline = baseline_metrics.get("metrics") or {}
    reasons: list[str] = []

    for name, floor in sorted(floors.items()):
        value = current.get(name)
        if value is None:
            # A floor set on a metric the evaluator does not produce is a
            # configuration mistake, and reporting it beats passing
            # silently: a floor believed to be active but never evaluated
            # is worse than no floor at all.
            reasons.append(f"metric FAIL: {name} not reported, floor {floor}")
        elif value < floor:
            reasons.append(f"metric FAIL: {name} {value:.3f} < floor {floor}")

    for name, tolerance in sorted(max_regression.items()):
        value = current.get(name)
        base = baseline.get(name)
        if value is None or base is None:
            continue
        if value < base - tolerance:
            reasons.append(
                f"metric FAIL: {name} regressed {value:.3f} < "
                f"{base:.3f} - {tolerance}"
            )

    return not reasons, reasons


def phase_6_gate_decision(current_metrics: dict, baseline_metrics: dict,
                          thresholds: dict | None = None) -> dict:
    """Multi-gate decision. Returns {"decision", "reasons"}.

    decision: "keep" | "discard" | "revert"
    """
    th = thresholds or {}
    min_delta = th.get("min_delta", 0.02)
    trigger_tolerance = th.get("trigger_tolerance", 0.05)
    max_token_increase = th.get("max_token_increase", 0.20)
    max_latency_increase = th.get("max_latency_increase", 0.20)
    regression_tolerance = th.get("regression_tolerance", 0.05)
    noise_threshold = th.get("noise_threshold", 0.01)

    reasons = []

    # Hard failures
    if current_metrics.get("status") in ("crash", "timeout"):
        return {"decision": "revert", "reasons": ["crash or timeout"]}

    if not current_metrics.get("l1_pass", True):
        return {"decision": "discard", "reasons": ["L1 gate failed"]}

    # Multi-gate AND logic
    cur_pr = current_metrics.get("pass_rate", 0)
    base_pr = baseline_metrics.get("pass_rate", 0)

    # Holdout pass rate (soft-fetch — None means evaluator did not run holdout
    # split, e.g. GT has no holdout cases). When None, we silently degrade to
    # the legacy dev-only quality check.
    cur_ho = current_metrics.get("holdout_pass_rate")
    base_ho = baseline_metrics.get("holdout_pass_rate")
    has_holdout = cur_ho is not None and base_ho is not None

    # Dev saturation: when baseline dev is already at the ceiling (within
    # noise of 1.0), demanding cur_pr >= base_pr + min_delta is mathematically
    # impossible (pass_rate cannot exceed 1.0). Switch the quality criterion
    # to "dev does not regress AND holdout improved by min_delta".
    dev_saturated = base_pr >= 1.0 - noise_threshold

    if dev_saturated:
        dev_no_regress = cur_pr >= base_pr - noise_threshold
        if has_holdout:
            # Both dev AND holdout already at the ceiling: demanding
            # holdout_improved (below) is mathematically impossible once
            # base_ho >= 1.0 - noise_threshold, since pass_rate cannot
            # exceed 1.0 — that made "keep" permanently unreachable for
            # any future change, including safe, no-regression fixes
            # (docs, slimming) this skill's own evolve_plan.md explicitly
            # recommends once saturated. Degrade one step further: the
            # only honest bar left is "did not regress on either split".
            holdout_saturated = base_ho >= 1.0 - noise_threshold
            if holdout_saturated:
                holdout_no_regress = cur_ho >= base_ho - noise_threshold
                quality_ok = dev_no_regress and holdout_no_regress
                if quality_ok:
                    reasons.append(
                        f"quality (dev+holdout saturated): both held at "
                        f"ceiling (dev {cur_pr:.3f}, holdout {cur_ho:.3f})"
                    )
                else:
                    if not dev_no_regress:
                        reasons.append(
                            f"quality FAIL (dev+holdout saturated): dev "
                            f"regressed {cur_pr:.3f} < {base_pr:.3f} - {noise_threshold}"
                        )
                    else:
                        reasons.append(
                            f"quality FAIL (dev+holdout saturated): holdout "
                            f"regressed {cur_ho:.3f} < {base_ho:.3f} - {noise_threshold}"
                        )
            else:
                holdout_improved = cur_ho >= base_ho + min_delta
                quality_ok = dev_no_regress and holdout_improved
                if quality_ok:
                    reasons.append(
                        f"quality (dev saturated): dev held at {cur_pr:.3f}, "
                        f"holdout {cur_ho:.3f} >= {base_ho:.3f} + {min_delta}"
                    )
                else:
                    if not dev_no_regress:
                        reasons.append(
                            f"quality FAIL (dev saturated): dev regressed "
                            f"{cur_pr:.3f} < {base_pr:.3f} - {noise_threshold}"
                        )
                    else:
                        reasons.append(
                            f"quality FAIL (dev saturated): holdout "
                            f"{cur_ho:.3f} < {base_ho:.3f} + {min_delta}"
                        )
        else:
            # No holdout data — dev is saturated and we have no other signal
            # to improve. The only honest call is "no signal, do not risk".
            quality_ok = False
            reasons.append(
                f"quality FAIL (dev saturated, no holdout): no signal to improve"
            )
    else:
        quality_ok = cur_pr >= base_pr + min_delta
        if quality_ok:
            reasons.append(f"quality: {cur_pr:.3f} >= {base_pr:.3f} + {min_delta}")
        else:
            reasons.append(f"quality FAIL: {cur_pr:.3f} < {base_pr:.3f} + {min_delta}")

    # Holdout consistency hard guard (anti-Goodhart): regardless of dev
    # behavior, a meaningful holdout regression always vetoes a keep. This
    # implements the Strict Eval Gate from gate_rules.md, which previously
    # existed only as documented pseudocode with no code path.
    holdout_consistent = True
    if has_holdout and cur_ho < base_ho - noise_threshold:
        holdout_consistent = False
        reasons.append(
            f"holdout REGRESS (overfit signal): {cur_ho:.3f} < "
            f"{base_ho:.3f} - {noise_threshold}"
        )

    # Absent on both sides means no trigger data was collected, so this
    # gate has nothing to compare and cannot veto anything. Recorded
    # explicitly rather than left implicit: with the defaults below the
    # check passes unconditionally, and a gate silently doing nothing looks
    # identical in the log to a gate that examined the data and approved —
    # which is how a change that harmed trigger accuracy could be kept
    # while the log appeared to show the guard working.
    has_trigger = (
        "trigger_f1" in current_metrics or "trigger_f1" in baseline_metrics
    )
    cur_trigger = current_metrics.get("trigger_f1", 1.0)
    base_trigger = baseline_metrics.get("trigger_f1", 1.0)
    trigger_ok = cur_trigger >= base_trigger * (1 - trigger_tolerance)
    if not trigger_ok:
        reasons.append(f"trigger FAIL: {cur_trigger:.3f} < {base_trigger:.3f} * {1 - trigger_tolerance}")
    elif not has_trigger:
        reasons.append("trigger not evaluated (no trigger data; gate inactive)")

    cur_tokens = current_metrics.get("tokens_mean", 0)
    base_tokens = baseline_metrics.get("tokens_mean", 1)
    # `base_tokens == 0` is not just a divide-by-zero guard — treating it
    # as "skip the check" silently disables cost gating whenever the
    # active evaluator reports an honest zero (behavioral_runner.py's
    # CLI path always does, by design, not as an estimate). With the same
    # evaluator on both sides, cur_tokens is also 0 and the two branches
    # agree anyway; this only matters — and was silently wrong — when a
    # nonzero cost appears against a zero baseline (e.g. an evaluator
    # switch mid-run). No baseline signal to compare against, so require
    # the candidate to also report zero rather than passing unconditionally.
    cost_ok = cur_tokens == 0 if base_tokens == 0 else cur_tokens <= base_tokens * (1 + max_token_increase)
    if not cost_ok:
        reasons.append(f"cost FAIL: {cur_tokens} > {base_tokens} * {1 + max_token_increase}")

    cur_dur = current_metrics.get("duration_mean", 0)
    base_dur = baseline_metrics.get("duration_mean", 1)
    latency_ok = cur_dur == 0 if base_dur == 0 else cur_dur <= base_dur * (1 + max_latency_increase)
    if not latency_ok:
        reasons.append(f"latency FAIL: {cur_dur:.1f} > {base_dur:.1f} * {1 + max_latency_increase}")

    cur_reg = current_metrics.get("regression_pass", 1.0)
    base_reg = baseline_metrics.get("regression_pass", 1.0)
    regression_ok = cur_reg >= base_reg * (1 - regression_tolerance)
    if not regression_ok:
        reasons.append(f"regression FAIL: {cur_reg:.3f} < {base_reg:.3f} * {1 - regression_tolerance}")

    # Structure and per-dimension metric floors. Both delegate to their own
    # function so each rule can be read and tested on its own; the gate's
    # job here is only to combine verdicts.
    structure_ok, structure_reasons = check_structure(
        current_metrics.get("snapshot"),
        baseline_metrics.get("snapshot"),
        th,
    )
    reasons.extend(structure_reasons)

    metrics_ok, metric_reasons = check_metric_thresholds(
        current_metrics, baseline_metrics, th
    )
    reasons.extend(metric_reasons)

    if (quality_ok and trigger_ok and cost_ok and latency_ok
            and regression_ok and holdout_consistent
            and structure_ok and metrics_ok):
        return {"decision": "keep", "reasons": reasons}

    # Noise check
    if abs(cur_pr - base_pr) < noise_threshold:
        reasons.append(f"change within noise ({noise_threshold})")

    return {"decision": "discard", "reasons": reasons}
