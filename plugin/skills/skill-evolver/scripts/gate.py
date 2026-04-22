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

    cur_trigger = current_metrics.get("trigger_f1", 1.0)
    base_trigger = baseline_metrics.get("trigger_f1", 1.0)
    trigger_ok = cur_trigger >= base_trigger * (1 - trigger_tolerance)
    if not trigger_ok:
        reasons.append(f"trigger FAIL: {cur_trigger:.3f} < {base_trigger:.3f} * {1 - trigger_tolerance}")

    cur_tokens = current_metrics.get("tokens_mean", 0)
    base_tokens = baseline_metrics.get("tokens_mean", 1)
    cost_ok = base_tokens == 0 or cur_tokens <= base_tokens * (1 + max_token_increase)
    if not cost_ok:
        reasons.append(f"cost FAIL: {cur_tokens} > {base_tokens} * {1 + max_token_increase}")

    cur_dur = current_metrics.get("duration_mean", 0)
    base_dur = baseline_metrics.get("duration_mean", 1)
    latency_ok = base_dur == 0 or cur_dur <= base_dur * (1 + max_latency_increase)
    if not latency_ok:
        reasons.append(f"latency FAIL: {cur_dur:.1f} > {base_dur:.1f} * {1 + max_latency_increase}")

    cur_reg = current_metrics.get("regression_pass", 1.0)
    base_reg = baseline_metrics.get("regression_pass", 1.0)
    regression_ok = cur_reg >= base_reg * (1 - regression_tolerance)
    if not regression_ok:
        reasons.append(f"regression FAIL: {cur_reg:.3f} < {base_reg:.3f} * {1 - regression_tolerance}")

    if (quality_ok and trigger_ok and cost_ok and latency_ok
            and regression_ok and holdout_consistent):
        return {"decision": "keep", "reasons": reasons}

    # Noise check
    if abs(cur_pr - base_pr) < noise_threshold:
        reasons.append(f"change within noise ({noise_threshold})")

    return {"decision": "discard", "reasons": reasons}
