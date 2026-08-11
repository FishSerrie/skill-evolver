#!/usr/bin/env python3
"""Evolve loop orchestrator + CLI entrypoint.

This module owns the "glue" that chains the individual Phase functions
(defined in ``evolve_loop.py``) into a complete run:

  * ``_eval_holdout_or_none`` — holdout-split soft fetch used by the
    baseline + gate paths
  * ``run_evolve_loop`` — the canonical 8-Phase orchestrator; calls
    phase_0..phase_8 in order, owns the iteration counter, and carries
    the best-so-far state across iterations
  * ``main`` — the ``python evolve_loop.py`` CLI entry (argparse wiring
    + flag dispatch for --info / --cleanup / --run / --dry-run)

Split rationale (iter 18): ``run_evolve_loop`` was the single biggest
function in the repo (~240 lines) and ``main`` another ~150 lines of
argparse plumbing. Together they were half of evolve_loop.py. Keeping
the Phase definitions in one file (``evolve_loop.py``) and the
"assemble + drive" logic here makes each file's purpose greppable
from its name.

``evolve_loop.py`` still exposes both functions via re-export, so the
``python scripts/evolve_loop.py <args>`` entry point and any existing
``from evolve_loop import run_evolve_loop`` callers keep working.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import find_creator_path, find_workspace
from aggregate_results import parse_results_tsv, calculate_summary
from evaluators import (
    EVALUATOR_NAMES, get_evaluator, parse_evaluator_from_plan, Evaluator,
)
from gate import phase_6_gate_decision
from llm import phase_2_diagnose, phase_3_modify, phase_6_5_review, auto_construct_gt
from cleanup import (
    cleanup_best_versions, cleanup_eval_outputs, _try_launch_eval_viewer,
    _prepare_viewer_data,
)
from evolve_loop import (  # phase definitions live in evolve_loop.py
    phase_0_setup, phase_1_review, phase_4_commit,
    phase_7_log, phase_8_loop_control,
    git_revert_last, save_best_version, _list_untracked,
    persist_holdout_cases,
)


def _measured(result: dict, *keys: str) -> dict:
    """The named keys that ``result`` actually carries, omitting the rest.

    Exists so a metric nobody measured stays absent instead of arriving as
    a plausible constant. Both `trigger_f1` and `regression_pass` were
    previously passed to the gate as a literal ``1.0`` regardless of
    whether anything had computed them — a perfect score, permanently, for
    a check that never ran. That made `trigger_tolerance` and
    `regression_tolerance` dead settings, and it also defeated the gate's
    own safeguard: `check_trigger` looks for the key's presence to decide
    whether to report itself inactive, so a constant made it announce a
    passing check forever.

    An absent key lets the gate say "not measured", which is the truth and
    is actionable. A defaulted key says "measured, and fine", which is
    neither.
    """
    return {key: result[key] for key in keys if key in result}


def _eval_holdout_or_none(evaluator, skill_path: Path, gt_path: Path,
                          workspace: Path | None = None,
                          iteration: int | None = None) -> float | None:
    """Run the evaluator on the holdout split and return the pass rate.

    Returns None when the GT has no holdout cases (so the evaluator either
    raises or reports zero assertions). The gate then degrades to dev-only
    quality logic.

    ``workspace``/``iteration``, when both given, persist the holdout
    ``cases`` to ``iteration-E{N}/holdout_cases/`` via
    :func:`persist_holdout_cases` — fixing the bug traced in the
    architecture plan §0.6, where this function used to read
    ``result.get("pass_rate")`` and silently drop ``result["cases"]``.
    Both are optional (default None) so any existing caller that only
    wants the pass rate keeps working unchanged.
    """
    try:
        result = evaluator.full_eval(skill_path, gt_path, split="holdout")
    except Exception:
        return None
    if not result or result.get("total_assertions", 0) == 0:
        return None
    if workspace is not None and iteration is not None:
        persist_holdout_cases(workspace, iteration, result.get("cases"))
    return result.get("pass_rate")


def _git_diff_for_commit(skill_path: Path, commit_hash: str) -> str:
    """Return the diff a single commit introduced, for Phase 6.5's
    verifier panel — the panel reviews what a candidate actually
    changed, not a description of what it claims to have changed.

    Scoped to the artifact's pathspec. Unlike the staging path this is
    read-only, so an unscoped diff destroys nothing; what it does is feed
    the review panel changes the candidate did not make, and the panel is
    asked to judge whether the change is overfitting or gaming its
    metric. A verifier reasoning about someone else's edits is being
    asked the wrong question.

    Returns an empty string (not an exception) if the diff can't be
    produced (e.g. the commit is the repo's first commit and has no
    parent) — Phase 6.5 still runs with an empty diff rather than
    aborting the whole iteration over a diff-formatting failure.
    """
    from target import resolve_target

    try:
        target = resolve_target(skill_path)
    except (FileNotFoundError, ValueError):
        return ""
    result = subprocess.run(
        ["git", "diff", f"{commit_hash}~1", commit_hash,
         "--", target.vcs_pathspec],
        cwd=str(target.vcs_root), capture_output=True, text=True, timeout=10,
    )
    return result.stdout if result.returncode == 0 else ""


# ─────────────────────────────────────────────
# Full auto loop
# ─────────────────────────────────────────────

def run_evolve_loop(skill_path: Path, gt_path: Path, workspace: Path,
                    max_iterations: int = 20, model: str | None = None,
                    verbose: bool = True,
                    evaluator: Evaluator | None = None,
                    dry_run: bool = False) -> dict:
    """Run the complete 8-phase evolve loop.

    This is the REAL auto loop. Phase 2+3 use claude -p for LLM reasoning.
    Evaluation uses the pluggable Evaluator interface.

    Args:
        evaluator: Pluggable evaluator instance. If None, auto-detects from
                   evolve_plan.md config or defaults to CreatorEvaluator.
        dry_run: Preview mode. Phases 0..3 run normally (setup, baseline,
                 first-iteration review, ideate+modify), but the loop
                 breaks BEFORE phase_4_commit — no git commit happens,
                 no gate decision, no log write beyond the baseline.
                 The mutation proposal from phase_2_3 is returned in the
                 result dict so the user can inspect what would have
                 been changed before allowing a real run.
    """
    # Initialize evaluator
    plan_path = workspace / "evolve" / "evolve_plan.md"
    plan_config = parse_evaluator_from_plan(plan_path)
    if evaluator is None:
        eval_config = {k: v for k, v in plan_config.items() if k != "_unknown"}
        if model:
            eval_config["model"] = model
        evaluator = get_evaluator(eval_config)

    # Gate thresholds the plan may set. Read here so the keys documented in
    # SKILL.md and gate_rules.md reach the gate that implements them —
    # otherwise a plan capping the artifact's size would have no effect and
    # nothing would say so, which is worse than having no cap at all.
    gate_thresholds = {
        key: plan_config[key]
        for key in ("max_structure_growth", "max_structure",
                    "min_metrics", "max_metric_regression",
                    "min_delta", "noise_threshold", "trigger_tolerance",
                    "max_token_increase", "max_latency_increase",
                    "regression_tolerance")
        if key in plan_config
    }

    def log(msg):
        if verbose:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] {msg}", file=sys.stderr, flush=True)

    log("=" * 60)
    log("EVOLVE LOOP START")
    log(f"Skill: {skill_path}")
    log(f"GT: {gt_path}")
    log(f"Max iterations: {max_iterations}")
    log(f"Evaluator: {evaluator.info()}")
    if gate_thresholds:
        log(f"Gate thresholds from plan: {gate_thresholds}")
    for key in plan_config.get("_unknown", []):
        # Reported rather than ignored: a misspelled threshold used to be
        # dropped in silence, so a plan that looked like it capped the
        # artifact's size had no effect and nothing said so.
        log(f"WARNING: unrecognised plan setting {key!r} — it has no effect")
    log("=" * 60)

    # skill-creator is an optional enhancement, not a prerequisite. The
    # evolve loop's evaluation, gating, and memory are all Evolver's own
    # (LocalEvaluator is stdlib-only). Creator adds redundant frontmatter
    # validation, opt-in trigger-F1, and the post-run HTML review — each
    # degrades independently where it is used. Report which mode we are in
    # so the log makes the difference auditable, then continue either way.
    creator_path = find_creator_path()
    if creator_path:
        log(f"skill-creator found (optional enhancements enabled): {creator_path}")
    else:
        log("skill-creator not installed — running standalone. "
            "Frontmatter validation uses the built-in stdlib checker; "
            "trigger-F1 and the HTML review are unavailable.")

    # Phase 0: Setup
    log("Phase 0: Setup")
    setup = phase_0_setup(skill_path, gt_path, workspace)
    evolve_dir = Path(setup["evolve_dir"])

    l1 = evaluator.quick_gate(skill_path, gt_path)
    if not l1["pass"]:
        log(f"ABORT: L1 gate failed — {l1['errors']}")
        return {"success": False, "error": "L1 gate failed"}

    # Baseline eval — runs both dev and holdout so the gate can compare
    # both surfaces from iteration 1 onwards. holdout is soft-fetched and
    # may be None if the GT has no holdout split.
    log("Phase 0: Baseline eval")
    baseline = evaluator.full_eval(skill_path, gt_path)
    baseline_rate = baseline["pass_rate"]
    # Red-team finding #7 (iter 30): reject empty dev split. With zero
    # assertions, pass_rate collapses to 0.0 (the `if total_t else 0`
    # guard in LocalEvaluator), giving the gate no signal at all and
    # confusing the user as to why no iterations ever improve. An
    # empty dev GT is a data-prep error, not a valid loop state.
    if baseline.get("total_assertions", 0) == 0:
        msg = (
            f"Phase 0 baseline: GT at {gt_path} has 0 assertions in the "
            f"dev split. The evolve loop needs at least one scoreable "
            f"case to produce a signal for the Phase 6 gate. Add at "
            f"least one dev case to evals.json (see references/"
            f"eval_strategy.md for templates) or pass a different GT "
            f"via --gt."
        )
        log(f"ABORT: {msg}")
        return {"success": False, "error": msg}
    baseline_holdout = _eval_holdout_or_none(
        evaluator, skill_path, gt_path, workspace=workspace, iteration=0)
    log(f"Baseline: {baseline['total_passed']}/{baseline['total_assertions']} = {baseline_rate:.0%}"
        + (f" | holdout {baseline_holdout:.0%}" if baseline_holdout is not None else " | holdout n/a"))

    phase_7_log(workspace, 0, "baseline", baseline_rate * 100, 0.0,
                1.0, 0, "pass", "baseline", "-", "initial baseline")
    save_best_version(skill_path, workspace, 0)

    best_rate = baseline_rate
    best_holdout = baseline_holdout
    # Real bug found via adversarial review: cost/latency baseline used
    # to stay frozen at whatever Phase 0 measured, forever, even as
    # best_rate/best_holdout tracked the current best version on every
    # keep. gate_rules.md's own contract says "baseline: evaluation
    # results for the CURRENT BEST version" — that was only true for 2
    # of 5 gate dimensions. Track these the same way quality is tracked.
    best_tokens = baseline.get("tokens", 0)
    best_duration = baseline.get("duration", 0.0)
    # Structural size and per-metric scores of the current best, tracked the
    # same way for the same reason: a gate handed a stale baseline compares
    # the candidate against something that is no longer the incumbent.
    # Absent when the evaluator does not report them, in which case the
    # corresponding gate stays inactive rather than guessing.
    best_snapshot = baseline.get("snapshot")
    best_metrics = baseline.get("metrics", {})
    current_layer = "body"

    for iteration in range(1, max_iterations + 1):
        log("")
        log(f"{'=' * 40}")
        log(f"ITERATION {iteration}/{max_iterations}")
        log(f"{'=' * 40}")
        t0 = time.time()

        # Phase 1: Review
        log("Phase 1: Review")
        review = phase_1_review(workspace, skill_path)
        log(f"  {review['iterations']} iters, {review['keeps']} keeps, stuck={review['stuck']}")

        # Snapshot untracked files BEFORE phase_2_3 so we can diff
        # after the mutation runs and pass the resulting new_files list
        # to phase_4_commit. This lets Layer 3 mutations add new files
        # (iter 25 / #1) without re-opening iter 8's `git add -A`
        # footgun — only the files the mutation actually created are
        # staged, any user-dropped debris is ignored.
        untracked_before = _list_untracked(skill_path)

        # Phase 2: Diagnose, Phase 3: Modify (via claude -p) — two
        # separate _call_claude subprocess invocations, no shared
        # context (Module B isolation; phase_2_3_ideate_and_modify is
        # the deprecated single-call predecessor, kept only for
        # external callers that haven't migrated)
        log("Phase 2: Diagnose (calling claude -p)")
        diagnosis = phase_2_diagnose(
            skill_path, workspace, review, gt_path, current_layer, model=model)
        log("Phase 3: Modify (calling claude -p)")
        mutation = phase_3_modify(skill_path, diagnosis, current_layer, model=model)
        result_23 = {
            "changed": mutation["changed"],
            "description": mutation["description"],
            "mutation_type": "unknown",
            "diagnosis": diagnosis.get("recommended_focus", ""),
        }
        log(f"  Result: changed={result_23['changed']}, {result_23['description']}")

        if not result_23["changed"]:
            log("  No changes — stopping")
            phase_7_log(workspace, iteration, "-", best_rate * 100, 0.0,
                        1.0, 0, "pass", "exhausted", current_layer, "no improvement found")
            break

        # Compute mutation-added new files (iter 25). Empty set if the
        # mutation only edited tracked files, which is the common case.
        untracked_after = _list_untracked(skill_path)
        new_files = sorted(untracked_after - untracked_before)
        if new_files:
            log(f"  Phase 2+3 added {len(new_files)} new file(s): {', '.join(new_files[:5])}"
                + (f" (+{len(new_files) - 5} more)" if len(new_files) > 5 else ""))

        # Dry-run: stop here, before Phase 4 commits anything. Revert
        # the mutation first so the working tree matches what Phase 0
        # started with. The loop returns the proposed change so the
        # caller can inspect it.
        if dry_run:
            log("DRY-RUN: phase_2_3 proposed a mutation — reverting working tree and exiting")
            # Scoped to the artifact. `git checkout -- .` was already
            # safe by accident — `.` is a pathspec relative to cwd, so it
            # only reached the subtree — but it depended on cwd being a
            # directory, which is false for a file target. Naming the
            # pathspec makes the scope explicit and works for both shapes.
            from target import resolve_target

            target = resolve_target(skill_path)
            subprocess.run(
                ["git", "checkout", "--", target.vcs_pathspec],
                cwd=str(target.vcs_root),
                capture_output=True, text=True, timeout=10,
            )
            # Also remove the mutation-added untracked files so the
            # tree matches the pre-iteration state exactly. Paths are
            # repository-relative (that is what _list_untracked returns),
            # so they resolve against the repository root.
            for nf in new_files:
                try:
                    (target.vcs_root / nf).unlink(missing_ok=True)
                except OSError:
                    pass
            return {
                "success": True,
                "dry_run": True,
                "baseline_pass_rate": baseline_rate,
                "proposed_mutation": result_23,
                "proposed_new_files": new_files,
                "best_metric": best_rate,
                "iterations_run": 1,
            }

        # Phase 4: Commit — pass mutation-added new files explicitly
        log("Phase 4: Commit")
        commit = phase_4_commit(
            skill_path, current_layer, result_23["description"],
            new_files=new_files,
        )
        if not commit["success"]:
            log(f"  Commit failed: {commit.get('error')}")
            continue
        log(f"  Committed: {commit['commit_hash']}")

        # Phase 5: Verify
        log("Phase 5: Verify")
        l1 = evaluator.quick_gate(skill_path, gt_path)
        log(f"  L1: {'PASS' if l1['pass'] else 'FAIL'}")
        if not l1["pass"]:
            # Red-team finding #8 (iter 30): check revert actually
            # succeeded. If git revert fails (merge conflict, detached
            # HEAD, hook veto, etc.) and we keep iterating, the broken
            # mutation contaminates the next iteration's baseline and
            # the entire run becomes unreliable. Abort instead.
            revert = git_revert_last(skill_path, commit["commit_hash"])
            if not revert.get("success"):
                msg = (
                    f"L1 fail at iter {iteration}; git revert ALSO failed "
                    f"({revert.get('output', 'no output')}). Working tree "
                    f"is in an undefined state. Aborting loop."
                )
                log(f"  ABORT: {msg}")
                phase_7_log(workspace, iteration, commit["commit_hash"], 0, -(best_rate*100),
                            1.0, 0, "fail", "crash", current_layer, msg)
                return {"success": False, "error": msg,
                        "baseline_rate": baseline_rate, "best_rate": best_rate}
            phase_7_log(workspace, iteration, commit["commit_hash"], 0, -(best_rate*100),
                        1.0, 0, "fail", "discard", current_layer,
                        f"L1 fail: {result_23['description']}")
            continue

        # L2 eval (uses pluggable evaluator) — dev + holdout so the gate
        # has both surfaces. holdout is soft-fetched (None if no split).
        log("  L2 eval...")
        new_eval = evaluator.full_eval(skill_path, gt_path)
        new_rate = new_eval["pass_rate"]
        new_holdout = _eval_holdout_or_none(
            evaluator, skill_path, gt_path, workspace=workspace, iteration=iteration)
        delta = new_rate - best_rate
        ho_msg = (f" | holdout {new_holdout:.0%}" if new_holdout is not None else "")
        log(f"  L2: {new_eval.get('total_passed', '?')}/{new_eval.get('total_assertions', '?')} = {new_rate:.0%} (delta: {delta:+.0%}){ho_msg}")

        # Phase 6: Gate (with real metrics from evaluator, incl. holdout)
        log("Phase 6: Gate")
        # Structural and per-metric figures are forwarded when the evaluator
        # supplies them. Omitting them left the structure and metric gates
        # permanently inactive — they pass when handed nothing — so a
        # candidate a hundred times more verbose was kept while the plan
        # appeared to cap its size. Thresholds come from the plan so the
        # documented keys (max_structure, min_metrics, ...) actually reach
        # the gate that reads them.
        #
        # `trigger_f1` and `regression_pass` are forwarded only when the
        # evaluator measured them, and deliberately not defaulted to 1.0.
        # They used to be hard-coded to 1.0 here while CreatorEvaluator was
        # computing the real figure two modules away, so `trigger_tolerance`
        # compared 1.0 against 1.0 forever — and worse, the gate's own
        # `has_trigger` check saw a value present and therefore never warned
        # that the trigger gate was inactive. A missing measurement now
        # looks missing, which is the only state the gate can report
        # honestly.
        gate = phase_6_gate_decision(
            {"pass_rate": new_rate, "holdout_pass_rate": new_holdout,
             "l1_pass": True,
             "tokens_mean": new_eval.get("tokens", 0),
             "duration_mean": new_eval.get("duration", 0.0),
             "snapshot": new_eval.get("snapshot"),
             "metrics": new_eval.get("metrics", {}),
             **_measured(new_eval, "trigger_f1", "regression_pass")},
            {"pass_rate": best_rate, "holdout_pass_rate": best_holdout,
             "tokens_mean": best_tokens,
             "duration_mean": best_duration,
             "snapshot": best_snapshot,
             "metrics": best_metrics,
             **_measured(baseline, "trigger_f1", "regression_pass")},
            {"min_delta": 0.01, "noise_threshold": 0.005, **gate_thresholds}
        )
        decision = gate["decision"]
        log(f"  Decision: {decision}")
        for r in gate.get("reasons", []):
            log(f"    · {r}")

        # Phase 6.5: Adversarial review panel — only spent on candidates
        # that already look like a keep. Three independent verifiers
        # (overfit / assertion_gaming / structural) can still veto a
        # numeric-gate pass; a "skipped" panel result (>=2 verifier
        # calls failed) falls back to the numeric gate's own decision
        # rather than blocking the iteration on a broken review step.
        # The whole block is wrapped in try/except (defense-in-depth,
        # per adversarial review): _call_llm now degrades subprocess-
        # level failures to an "error" verdict internally, but this is
        # still a newer, less battle-tested subsystem than the rest of
        # the loop — an unexpected exception here should degrade the
        # same way a "skipped" panel does, not crash an iteration that
        # otherwise already has a valid numeric-gate decision.
        adversarial_result = None
        if decision == "keep":
            log("Phase 6.5: Adversarial review")
            try:
                diff = _git_diff_for_commit(skill_path, commit["commit_hash"])
                adversarial_result = phase_6_5_review(
                    skill_path, diff,
                    {"dev_pass_rate": new_rate, "holdout_pass_rate": new_holdout,
                     "baseline_dev_pass_rate": best_rate,
                     "baseline_holdout_pass_rate": best_holdout},
                    model=model)
            except Exception as exc:
                adversarial_result = {
                    "decision": "skipped", "verdicts": [],
                    "reasoning": f"Phase 6.5 raised {type(exc).__name__}: {exc}",
                }
            log(f"  Panel: {adversarial_result['decision']} — {adversarial_result['reasoning']}")
            if adversarial_result["decision"] == "reject":
                decision = "discard"

        if decision == "keep":
            best_rate = new_rate
            if new_holdout is not None:
                best_holdout = new_holdout
            best_tokens = new_eval.get("tokens", 0)
            best_duration = new_eval.get("duration", 0.0)
            # Advance the structural and per-metric baselines with the rest.
            # Leaving them frozen at Phase 0 would let a candidate grow 25%
            # every iteration and never trip the cap, since each step is
            # measured against the original rather than the incumbent.
            if new_eval.get("snapshot"):
                best_snapshot = new_eval["snapshot"]
            if new_eval.get("metrics"):
                best_metrics = new_eval["metrics"]
            save_best_version(skill_path, workspace, iteration)
            log(f"  KEEP — new best: dev {best_rate:.0%}"
                + (f", holdout {best_holdout:.0%}" if best_holdout is not None else ""))
        else:
            # Red-team finding #8 (iter 30): same safety check as the
            # L1-fail branch above — a failed revert means the mutation
            # is still in the working tree and subsequent iterations
            # would build on corrupt state. Abort the loop cleanly
            # instead of pretending the revert succeeded.
            revert = git_revert_last(skill_path, commit["commit_hash"])
            if not revert.get("success"):
                msg = (
                    f"Gate decision={decision} at iter {iteration}; "
                    f"git revert ALSO failed ({revert.get('output', 'no output')}). "
                    f"Working tree is in an undefined state. Aborting loop."
                )
                log(f"  ABORT: {msg}")
                phase_7_log(workspace, iteration, commit["commit_hash"],
                            new_rate * 100, delta * 100,
                            1.0, new_eval.get("tokens", 0), "fail", "crash",
                            current_layer, msg)
                return {"success": False, "error": msg,
                        "baseline_rate": baseline_rate, "best_rate": best_rate}
            log(f"  {decision.upper()} — reverted")

        # Phase 7: Log (writes results.tsv + experiments.jsonl +
        # iteration-E{N}/meta.json + iteration-E{N}/cases/case_{id}.json
        # — the full paper §2 filesystem layout for next-iter Phase 1
        # grep/cat access)
        elapsed = time.time() - t0
        phase_7_log(workspace, iteration, commit["commit_hash"],
                    new_rate * 100, delta * 100,
                    1.0, new_eval.get("tokens", 0), "pass", decision,
                    current_layer, result_23["description"],
                    experiment={
                        "iteration": iteration,
                        "mutation_type": result_23["mutation_type"],
                        "mutation_layer": current_layer,
                        "intent": result_23["description"],
                        "status": decision,
                        "elapsed_seconds": round(elapsed, 1),
                        "tokens": new_eval.get("tokens", 0),
                        "duration": new_eval.get("duration", 0.0),
                        "diagnosis": result_23.get("diagnosis", ""),
                        "adversarial_review": adversarial_result,
                    },
                    eval_result=new_eval,
                    split="dev")
        log(f"  Logged ({elapsed:.1f}s)")

        # Phase 8: Loop control
        ctrl = phase_8_loop_control(workspace, max_iterations)
        log(f"Phase 8: {ctrl['reason']}")
        if not ctrl["continue"]:
            break
        if ctrl.get("promote_layer"):
            current_layer = ctrl["next_layer"]
            log(f"  PROMOTE → {current_layer}")

    # Final
    log("")
    log("=" * 60)
    log("EVOLVE COMPLETE")
    log("=" * 60)

    holdout = evaluator.full_eval(skill_path, gt_path, split="holdout")
    final_rows = parse_results_tsv(workspace)
    final_summary = calculate_summary(final_rows)

    log(f"Baseline: {baseline_rate:.0%} → Best: {best_rate:.0%}")
    log(f"Keeps: {final_summary['keep_count']} | Discards: {final_summary['discard_count']}")
    log(f"Holdout: {holdout['pass_rate']:.0%}")

    cleanup_best_versions(workspace, keep_n=3)

    # Build real skill outputs for eval viewer, then generate HTML
    viewer_data_dir = None
    try:
        viewer_data_dir = _prepare_viewer_data(workspace, holdout, skill_path)
    except Exception as exc:
        log(f"Viewer data preparation failed (non-fatal): {exc}")
    viewer_launched = _try_launch_eval_viewer(
        workspace, skill_path, viewer_data_dir=viewer_data_dir)
    if viewer_launched:
        log("Eval viewer launched — open the URL above to review results")

    return {
        "baseline_rate": baseline_rate,
        "best_rate": best_rate,
        "holdout_rate": holdout["pass_rate"],
        "iterations": final_summary["total_iterations"],
        "keeps": final_summary["keep_count"],
        "discards": final_summary["discard_count"],
        "viewer_launched": viewer_launched,
    }


# ─────────────────────────────────────────────
# Main (reference CLI)
# ─────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Evolve loop orchestrator")
    parser.add_argument("skill_path", type=Path, help="Path to target skill")
    parser.add_argument("--gt", type=Path, default=None, help="Path to GT JSON")
    parser.add_argument("--max-iterations", type=int, default=20)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--model", default=None, help="Model for LLM CLI")
    parser.add_argument("--evaluator", default=None,
                        # Read from the registry rather than repeated here.
                        # The hard-coded list had gone stale: `grader` and
                        # `behavioral` were both supported by
                        # `get_evaluator`, and SKILL.md documents
                        # `evaluator: grader`, but argparse rejected the flag
                        # outright with exit 2 — a supported backend
                        # unreachable from the CLI that documents it.
                        choices=list(EVALUATOR_NAMES),
                        help="Evaluator engine (default: auto-detect from evolve_plan.md)")
    parser.add_argument("--evaluator-script", default=None,
                        help="Path to eval script (for --evaluator script)")
    parser.add_argument("--evaluator-test-cmd", default=None,
                        help="Test command (for --evaluator pytest)")
    parser.add_argument("--run", action="store_true",
                        help="Run the full auto evolve loop")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview the first iteration's proposed "
                             "mutation without committing or gating — "
                             "Phase 0..3 run, then the working tree "
                             "is reverted and the proposal is returned")
    parser.add_argument("--info", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--cleanup-versions", action="store_true")
    parser.add_argument("--creator-path", type=Path, default=None,
                        help="Path to skill-creator installation (overrides auto-discovery)")
    parser.add_argument("--verbose", action="store_true", default=True)
    args = parser.parse_args()

    # Set creator path override via env var (picked up by require_creator())
    if args.creator_path:
        os.environ["SKILL_CREATOR_PATH"] = str(args.creator_path.resolve())

    ws = args.workspace or find_workspace(args.skill_path)

    if args.info:
        # Evaluator registry was removed in iter 19 in favor of lazy
        # imports. Enumerate the known backend names here instead of
        # poking into evaluators.py internals.
        from evaluators import EVALUATOR_NAMES
        evaluators_info = {name: name.capitalize() + "Evaluator"
                           for name in EVALUATOR_NAMES}
        print(json.dumps({
            "phases": {
                "phase_0": "Setup (auto)", "phase_1": "Review (auto)",
                "phase_2_3": "Ideate+Modify (LLM)", "phase_4": "Commit (auto)",
                "phase_5": "Verify (pluggable evaluator)", "phase_6": "Gate (auto)",
                "phase_7": "Log (auto)", "phase_8": "Loop control (auto)",
            },
            "evaluators": evaluators_info,
        }, indent=2))
        return

    if args.cleanup:
        print(json.dumps({"cleaned": cleanup_eval_outputs(ws)}, indent=2))
        return

    if args.cleanup_versions:
        print(json.dumps({"cleaned": cleanup_best_versions(ws)}, indent=2))
        return

    if not args.gt:
        # Auto-discover GT data
        candidates = [
            ws / "evals" / "evals.json",
            args.skill_path / "evals.json",
            args.skill_path.parent / "evals.json",
        ]
        for candidate in candidates:
            if candidate.exists():
                args.gt = candidate
                print(f"Auto-discovered GT data: {candidate}", file=sys.stderr)
                break
        if not args.gt:
            # Auto-construct GT using LLM to analyze the skill
            gt_target = ws / "evals" / "evals.json"
            gt_target.parent.mkdir(parents=True, exist_ok=True)
            print("No GT data found. Auto-constructing from SKILL.md...",
                  file=sys.stderr)
            gt_result = auto_construct_gt(args.skill_path, gt_target,
                                          model=args.model)
            if gt_result:
                args.gt = gt_target
                print(f"Generated {gt_result['count']} test cases → {gt_target}",
                      file=sys.stderr)
            else:
                print("Error: GT auto-construction failed. Provide --gt manually.",
                      file=sys.stderr)
                sys.exit(1)

    # Build evaluator from CLI args or evolve_plan.md
    eval_config = {}
    if args.evaluator:
        eval_config["evaluator"] = args.evaluator
    if args.evaluator_script:
        eval_config["evaluator_script"] = args.evaluator_script
    if args.evaluator_test_cmd:
        eval_config["evaluator_test_cmd"] = args.evaluator_test_cmd
    if args.model:
        eval_config["model"] = args.model

    evaluator_instance = None
    if eval_config.get("evaluator"):
        evaluator_instance = get_evaluator(eval_config)

    # Creator is optional — report availability, never gate on it.
    creator = find_creator_path()
    if creator:
        print(f"skill-creator found: {creator}", file=sys.stderr)
    else:
        print("skill-creator not installed — running standalone "
              "(built-in validation; no trigger-F1 or HTML review).",
              file=sys.stderr)

    if args.run or args.dry_run:
        # THE REAL LOOP (or dry-run preview)
        result = run_evolve_loop(
            args.skill_path, args.gt, ws,
            max_iterations=args.max_iterations,
            model=args.model, verbose=args.verbose,
            evaluator=evaluator_instance,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2, default=str))
    else:
        # Setup only
        setup = phase_0_setup(args.skill_path, args.gt, ws)
        print(json.dumps(setup, indent=2))
        print("\nTo run the full loop, add --run:", file=sys.stderr)
        print(f"  python evolve_loop.py {args.skill_path} --gt {args.gt} --run",
              file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        sys.exit(130)
    except FileNotFoundError as e:
        print(f"Error: File not found — {e}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in GT data — {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Run with PYTHONTRACEBACK=1 for full traceback.", file=sys.stderr)
        if os.environ.get("PYTHONTRACEBACK"):
            raise
        sys.exit(1)
