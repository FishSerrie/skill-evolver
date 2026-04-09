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
from common import require_creator, CreatorNotFoundError, find_workspace
from aggregate_results import parse_results_tsv, calculate_summary
from evaluators import get_evaluator, parse_evaluator_from_plan, Evaluator
from gate import phase_6_gate_decision
from llm import phase_2_3_ideate_and_modify, auto_construct_gt
from cleanup import (
    cleanup_best_versions, cleanup_eval_outputs, _try_launch_eval_viewer,
)
from evolve_loop import (  # phase definitions live in evolve_loop.py
    phase_0_setup, phase_1_review, phase_4_commit,
    phase_7_log, phase_8_loop_control,
    git_revert_last, save_best_version,
)


def _eval_holdout_or_none(evaluator, skill_path: Path,
                          gt_path: Path) -> float | None:
    """Run the evaluator on the holdout split and return the pass rate.

    Returns None when the GT has no holdout cases (so the evaluator either
    raises or reports zero assertions). The gate then degrades to dev-only
    quality logic.
    """
    try:
        result = evaluator.full_eval(skill_path, gt_path, split="holdout")
    except Exception:
        return None
    if not result or result.get("total_assertions", 0) == 0:
        return None
    return result.get("pass_rate")


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
    if evaluator is None:
        plan_path = workspace / "evolve" / "evolve_plan.md"
        eval_config = parse_evaluator_from_plan(plan_path)
        if model:
            eval_config["model"] = model
        evaluator = get_evaluator(eval_config)

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
    log("=" * 60)

    # Creator dependency check (fail fast)
    log("Checking skill-creator dependency...")
    creator_path = require_creator()
    log(f"Creator found: {creator_path}")

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
    baseline_holdout = _eval_holdout_or_none(evaluator, skill_path, gt_path)
    log(f"Baseline: {baseline['total_passed']}/{baseline['total_assertions']} = {baseline_rate:.0%}"
        + (f" | holdout {baseline_holdout:.0%}" if baseline_holdout is not None else " | holdout n/a"))

    phase_7_log(workspace, 0, "baseline", baseline_rate * 100, 0.0,
                1.0, 0, "pass", "baseline", "-", "initial baseline")
    save_best_version(skill_path, workspace, 0)

    best_rate = baseline_rate
    best_holdout = baseline_holdout
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

        # Phase 2+3: Ideate and Modify (via claude -p)
        log("Phase 2+3: Ideate and Modify (calling claude -p)")
        result_23 = phase_2_3_ideate_and_modify(
            skill_path, workspace, review, gt_path, current_layer, model)
        log(f"  Result: changed={result_23['changed']}, {result_23['description']}")

        if not result_23["changed"]:
            log("  No changes — stopping")
            phase_7_log(workspace, iteration, "-", best_rate * 100, 0.0,
                        1.0, 0, "pass", "exhausted", current_layer, "no improvement found")
            break

        # Dry-run: stop here, before Phase 4 commits anything. Revert
        # the mutation first so the working tree matches what Phase 0
        # started with. The loop returns the proposed change so the
        # caller can inspect it.
        if dry_run:
            log("DRY-RUN: phase_2_3 proposed a mutation — reverting working tree and exiting")
            subprocess.run(
                ["git", "checkout", "--", "."], cwd=str(skill_path),
                capture_output=True, text=True, timeout=10,
            )
            return {
                "success": True,
                "dry_run": True,
                "baseline_pass_rate": baseline_rate,
                "proposed_mutation": result_23,
                "best_metric": best_rate,
                "iterations_run": 1,
            }

        # Phase 4: Commit
        log("Phase 4: Commit")
        commit = phase_4_commit(skill_path, current_layer, result_23["description"])
        if not commit["success"]:
            log(f"  Commit failed: {commit.get('error')}")
            continue
        log(f"  Committed: {commit['commit_hash']}")

        # Phase 5: Verify
        log("Phase 5: Verify")
        l1 = evaluator.quick_gate(skill_path, gt_path)
        log(f"  L1: {'PASS' if l1['pass'] else 'FAIL'}")
        if not l1["pass"]:
            git_revert_last(skill_path)
            phase_7_log(workspace, iteration, commit["commit_hash"], 0, -(best_rate*100),
                        1.0, 0, "fail", "discard", current_layer,
                        f"L1 fail: {result_23['description']}")
            continue

        # L2 eval (uses pluggable evaluator) — dev + holdout so the gate
        # has both surfaces. holdout is soft-fetched (None if no split).
        log("  L2 eval...")
        new_eval = evaluator.full_eval(skill_path, gt_path)
        new_rate = new_eval["pass_rate"]
        new_holdout = _eval_holdout_or_none(evaluator, skill_path, gt_path)
        delta = new_rate - best_rate
        ho_msg = (f" | holdout {new_holdout:.0%}" if new_holdout is not None else "")
        log(f"  L2: {new_eval.get('total_passed', '?')}/{new_eval.get('total_assertions', '?')} = {new_rate:.0%} (delta: {delta:+.0%}){ho_msg}")

        # Phase 6: Gate (with real metrics from evaluator, incl. holdout)
        log("Phase 6: Gate")
        gate = phase_6_gate_decision(
            {"pass_rate": new_rate, "holdout_pass_rate": new_holdout,
             "l1_pass": True, "trigger_f1": 1.0,
             "tokens_mean": new_eval.get("tokens", 0),
             "duration_mean": new_eval.get("duration", 0.0),
             "regression_pass": 1.0},
            {"pass_rate": best_rate, "holdout_pass_rate": best_holdout,
             "trigger_f1": 1.0,
             "tokens_mean": baseline.get("tokens", 0),
             "duration_mean": baseline.get("duration", 0.0),
             "regression_pass": 1.0},
            {"min_delta": 0.01, "noise_threshold": 0.005}
        )
        decision = gate["decision"]
        log(f"  Decision: {decision}")
        for r in gate.get("reasons", []):
            log(f"    · {r}")

        if decision == "keep":
            best_rate = new_rate
            if new_holdout is not None:
                best_holdout = new_holdout
            save_best_version(skill_path, workspace, iteration)
            log(f"  KEEP — new best: dev {best_rate:.0%}"
                + (f", holdout {best_holdout:.0%}" if best_holdout is not None else ""))
        else:
            git_revert_last(skill_path)
            log(f"  {decision.upper()} — reverted")

        # Phase 7: Log (with traces for Meta-Harness active diagnosis)
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
                    },
                    traces=new_eval.get("traces"))
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

    # Try to launch Creator's eval viewer if available
    viewer_launched = _try_launch_eval_viewer(workspace, skill_path)
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
                        choices=["local", "creator", "script", "pytest"],
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

    # Verify creator is available before doing any real work
    try:
        creator = require_creator()
        print(f"skill-creator found: {creator}", file=sys.stderr)
    except CreatorNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

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
