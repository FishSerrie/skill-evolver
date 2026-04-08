#!/usr/bin/env python3
"""Evolve Loop — 8-phase orchestrator for skill evolution.

Usage:
    # FULL AUTO LOOP (the real thing)
    python evolve_loop.py <skill-path> --gt <gt-json> --run [--max-iterations 20]

    # Setup only
    python evolve_loop.py <skill-path> --gt <gt-json>

    # Cleanup
    python evolve_loop.py <skill-path> --cleanup

This script runs the complete 8-phase evolve cycle. Phase 2 (Ideate) and
Phase 3 (Modify) use `claude -p` subprocess to invoke LLM reasoning.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import require_creator, CreatorNotFoundError, find_workspace, validate_frontmatter, parse_skill_md
from aggregate_results import parse_results_tsv, calculate_summary
from evaluators import get_evaluator, parse_evaluator_from_plan, Evaluator
from gate import phase_6_gate_decision  # extracted in iter 15
from llm import (  # extracted in iter 16
    _call_llm, _call_claude, _detect_llm_backend,
    phase_2_3_ideate_and_modify, run_l2_eval_via_claude, _local_eval,
    auto_construct_gt,
)


# ─────────────────────────────────────────────
# Phase 0: Setup (fully automated)
# ─────────────────────────────────────────────

def phase_0_setup(skill_path: Path, gt_path: Path,
                  workspace: Path | None = None) -> dict:
    """Create workspace, initialize memory, generate evolve_plan template.

    On first use, auto-detects creator tools (skill-creator, claw-creator, etc.)
    and configures the evaluation pipeline accordingly.

    Enforces the "clean git state" precondition from
    ``references/evolve_protocol.md`` Phase 0 — without it,
    ``phase_4_commit``'s ``git add -A`` would sweep the user's unrelated
    uncommitted edits into an experiment commit, and a subsequent
    ``git revert`` (after a discard) would silently delete that work.

    Returns: {"workspace", "evolve_dir", "plan_path", "baseline_needed", "creator_config"}
    """
    from setup_workspace import setup_workspace  # noqa: sibling import
    from common import setup_creator_config

    # Precondition: skill dir must be under git AND have a clean working
    # tree. Four-step decision tree mirrors evolve_protocol.md Phase 4:
    #   1. Already under git, clean → proceed
    #   2. Already under git, dirty → refuse (would co-opt user's work)
    #   3. Not under git, git installed → auto-init + initial commit
    #   4. Git not installed → refuse with install instructions
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(skill_path), capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"Phase 0: git is not installed. Install git and retry:\n"
            f"  macOS:  brew install git  or  xcode-select --install\n"
            f"  Ubuntu: sudo apt-get install git\n"
            f"  CentOS: sudo yum install git\n"
            f"  Windows: https://git-scm.com/download/win"
        ) from e
    except (subprocess.TimeoutExpired, OSError) as e:
        raise RuntimeError(f"Phase 0: cannot run `git status` in {skill_path}: {e}") from e

    if status.returncode != 0:
        # Not a git repo. Auto-init per protocol (step 3): git is
        # installed (we just ran it successfully enough to get a
        # non-zero exit), the user has authorized operating on this
        # skill dir, and no prior commit means no user work to lose.
        try:
            subprocess.run(
                ["git", "init"], cwd=str(skill_path),
                capture_output=True, text=True, timeout=10, check=True,
            )
            subprocess.run(
                ["git", "add", "."], cwd=str(skill_path),
                capture_output=True, text=True, timeout=10, check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "chore: init git for evolve tracking"],
                cwd=str(skill_path), capture_output=True, text=True,
                timeout=10, check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            raise RuntimeError(
                f"Phase 0: auto-init failed in {skill_path}: {e}\n"
                f"Run manually: git init && git add . && git commit -m 'init'"
            ) from e
    elif status.stdout.strip():
        # Already a git repo AND dirty → refuse. phase_4_commit's
        # `git add -u` would pull tracked-file dirt into the first
        # experiment commit, and a discarded iteration's `git revert`
        # would silently delete the user's work.
        raise RuntimeError(
            f"Phase 0: {skill_path} has uncommitted changes. Commit or stash "
            f"them before running evolve — otherwise `git add -u` in "
            f"phase_4_commit would sweep tracked-file changes into the "
            f"first experiment commit, and a discarded iteration would "
            f"silently revert your work.\n\n"
            f"Dirty files:\n{status.stdout}"
        )

    ws = workspace or find_workspace(skill_path)
    result = setup_workspace(skill_path, ws)

    evolve_dir = Path(result["evolve_dir"])
    plan_path = evolve_dir / "evolve_plan.md"
    results_tsv = evolve_dir / "results.tsv"

    # First-use creator detection and configuration
    creator_config = setup_creator_config(ws, skill_path)

    # Check if baseline already exists
    baseline_needed = True
    if results_tsv.exists():
        content = results_tsv.read_text()
        if "baseline" in content:
            baseline_needed = False

    return {
        "workspace": str(ws),
        "evolve_dir": str(evolve_dir),
        "plan_path": str(plan_path),
        "baseline_needed": baseline_needed,
        "gt_path": str(gt_path),
        "skill_path": str(skill_path),
        "creator_config": creator_config,
    }


# ─────────────────────────────────────────────
# Phase 1: Review (fully automated)
# ─────────────────────────────────────────────

def phase_1_review(workspace: Path, skill_path: Path) -> dict:
    """Read memory and analyze current state.

    Args:
        workspace: the evolve workspace containing results.tsv and
            experiments.jsonl.
        skill_path: the skill directory under git. Required so the git
            log read runs inside the actual repo; previous versions
            passed ``workspace.parent`` here, which is the GRANDPARENT
            of the skill and typically not a git repo at all, so the
            git log silently returned empty and Phase 2 had no history.

    Returns: {"iterations", "keeps", "discards", "stuck", "recent_failures",
              "successful_patterns", "current_best_metric", "git_log"}
    """

    evolve_dir = workspace / "evolve"
    rows = parse_results_tsv(workspace)
    summary = calculate_summary(rows)

    # Read experiments.jsonl for detailed patterns
    experiments_path = evolve_dir / "experiments.jsonl"
    recent_experiments = []
    if experiments_path.exists():
        lines = experiments_path.read_text().strip().split("\n")
        for line in lines[-10:]:  # last 10
            if line.strip():
                try:
                    recent_experiments.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    # Extract patterns
    successful_patterns = [
        e.get("mutation_type") for e in recent_experiments
        if e.get("status") == "keep"
    ]
    recent_failures = [
        {"intent": e.get("intent"), "reason": e.get("failure_reason")}
        for e in recent_experiments
        if e.get("status") in ("discard", "crash")
    ][-5:]  # last 5 failures

    # Try to get git log — must run inside the skill dir (the git repo),
    # NOT in workspace.parent (the skill's grandparent, typically not a repo).
    git_log = ""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-15"],
            capture_output=True, text=True, timeout=5,
            cwd=str(skill_path),
        )
        if result.returncode == 0:
            git_log = result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Meta-Harness: read execution traces from recent failed iterations
    # Enables active diagnosis in Phase 2 (grep traces, not guess).
    # Sort case files by numeric case id so case_10+ don't get truncated
    # ahead of case_2..case_9 under lex sort (`case_10` precedes `case_2`).
    recent_traces = {}
    if rows:
        # Find the most recent iteration with traces
        for row in reversed(rows):
            iter_num = row.get("iteration", 0)
            trace_dir = evolve_dir / f"iteration-E{iter_num}" / "traces"
            if trace_dir.exists():
                case_files = sorted(
                    trace_dir.glob("case_*.md"),
                    key=lambda p: _iter_num(p.stem),
                )
                for trace_file in case_files[:10]:
                    recent_traces[trace_file.stem] = trace_file.read_text()[:2000]
                break  # only the most recent iteration's traces

    # Collect past diagnoses (counterfactual insights from prior iterations)
    past_diagnoses = [
        e.get("diagnosis") for e in recent_experiments
        if e.get("diagnosis")
    ][-5:]

    return {
        "iterations": summary["total_iterations"],
        "keeps": summary["keep_count"],
        "discards": summary["discard_count"],
        "crashes": summary["crash_count"],
        "stuck": summary.get("is_stuck", False),
        "current_best_metric": summary.get("best_metric"),
        "best_iteration": summary.get("best_iteration"),
        "latest_metric": summary.get("latest_metric"),
        "trajectory": summary.get("trajectory", []),
        "recent_failures": recent_failures,
        "successful_patterns": successful_patterns,
        "git_log": git_log,
        "recent_traces": recent_traces,
        "past_diagnoses": past_diagnoses,
    }


# ─────────────────────────────────────────────
# Phase 2+3 (Ideate+Modify) lives in phase_2_3_ideate_and_modify below.
# The earlier phase_2_prepare_ideation helper was removed once the LLM
# prompt was inlined there — nothing called it.
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# Phase 4: Commit (fully automated)
# ─────────────────────────────────────────────

def phase_4_commit(skill_path: Path, layer: str, description: str) -> dict:
    """Git add + commit the changes.

    Uses ``git add -u`` (tracked-only) so mid-loop user-added debris
    (scratch scripts, *.orig backups, editor swap files, etc.) does not
    get swept into an experiment commit. Paired with the Phase 0
    clean-tree check, this keeps the invariant "commits contain only
    the mutation's intended changes" from start to end of the run.

    Layer 3 mutations that legitimately add NEW files need to commit
    them separately — the loop does not auto-stage untracked content.

    Returns: {"success", "commit_hash", "files_changed"}
    """
    try:
        # Stage changes — tracked files only. Anything untracked is
        # presumed to be the user's own work and left alone. See the
        # docstring for the Layer 3 caveat.
        subprocess.run(["git", "add", "-u"], cwd=str(skill_path),
                       capture_output=True, timeout=10)

        # Check if there are changes
        status = subprocess.run(["git", "status", "--porcelain"],
                                cwd=str(skill_path), capture_output=True,
                                text=True, timeout=10)
        if not status.stdout.strip():
            return {"success": False, "commit_hash": None,
                    "files_changed": [], "error": "No changes to commit"}

        # Commit
        msg = f"experiment({layer}): {description}"
        result = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(skill_path), capture_output=True, text=True, timeout=10,
        )

        if result.returncode != 0:
            return {"success": False, "commit_hash": None,
                    "files_changed": [], "error": result.stderr.strip()}

        # Get commit hash
        hash_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(skill_path), capture_output=True, text=True, timeout=5,
        )
        commit_hash = hash_result.stdout.strip()

        # Get changed files
        diff_result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1"],
            cwd=str(skill_path), capture_output=True, text=True, timeout=5,
        )
        files = [f.strip() for f in diff_result.stdout.strip().split("\n") if f.strip()]

        return {"success": True, "commit_hash": commit_hash,
                "files_changed": files, "error": None}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "commit_hash": None,
                "files_changed": [], "error": str(e)}


# ─────────────────────────────────────────────
# Phase 5: Verify — L1 gate (automated)
# L2 eval requires Claude orchestration (see run_l2_eval.py)
# ─────────────────────────────────────────────

def phase_5_l1_gate(skill_path: Path, gt_path: Path | None = None) -> dict:
    """Run L1 quick gate. Returns {"pass", "checks", "errors"}."""
    from run_l1_gate import run_l1_gate
    return run_l1_gate(skill_path, gt_path)


# ─────────────────────────────────────────────
# Holdout helper — soft fetch
# ─────────────────────────────────────────────

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
# Phase 6: Gate Decision (fully automated)
# ─────────────────────────────────────────────

# phase_6_gate_decision lives in gate.py (imported at top of module).
# Re-exported via the top-level import for `from evolve_loop import ...`
# callers that still reference it as a sibling of other phase_* functions.


# ─────────────────────────────────────────────
# Phase 7: Log (fully automated)
# ─────────────────────────────────────────────

def persist_traces(workspace: Path, iteration: int,
                   traces: dict | None) -> Path | None:
    """Write per-case execution traces to ``iteration-E{N}/traces/``.

    This is the canonical Meta-Harness trace-writing path used by both
    ``phase_7_log`` (CLI ``--run`` mode) and by in-conversation Claude
    executors (who should call this directly after ``LocalEvaluator.
    full_eval`` to persist ``result['traces']`` for the next iteration's
    Phase 1/2 diagnosis).

    Args:
        workspace: the skill's workspace directory.
        iteration: the E-iteration number the traces belong to.
        traces: dict of ``{case_id: trace_content_str}``, or None/empty
            to skip.

    Returns:
        Path to the created ``traces/`` directory, or None if nothing
        was written.
    """
    if not traces:
        return None
    trace_dir = workspace / "evolve" / f"iteration-E{iteration}" / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    for case_id, trace_content in traces.items():
        trace_file = trace_dir / f"case_{case_id}.md"
        trace_file.write_text(str(trace_content))
    return trace_dir


def phase_7_log(workspace: Path, iteration: int, commit: str,
                metric: float, delta: float, trigger_f1: float,
                tokens: int, guard: str, status: str,
                layer: str, description: str,
                experiment: dict | None = None,
                traces: dict | None = None) -> None:
    """Append to results.tsv, experiments.jsonl, and write execution traces.

    Traces are delegated to :func:`persist_traces`, the shared helper
    in-conversation executors can also call directly without going
    through the full phase_7_log pipeline.
    """
    evolve_dir = workspace / "evolve"

    # results.tsv
    tsv_path = evolve_dir / "results.tsv"
    line = (f"{iteration}\t{commit}\t{metric:.1f}\t{delta:+.1f}\t"
            f"{trigger_f1:.2f}\t{tokens}\t{guard}\t{status}\t"
            f"{layer}\t{description}\n")
    with open(tsv_path, "a") as f:
        f.write(line)

    # experiments.jsonl
    if experiment:
        jsonl_path = evolve_dir / "experiments.jsonl"
        with open(jsonl_path, "a") as f:
            f.write(json.dumps(experiment, ensure_ascii=False) + "\n")

    # Execution traces (shared helper — see docstring for in-conv usage)
    persist_traces(workspace, iteration, traces)


# ─────────────────────────────────────────────
# Phase 8: Loop Control (fully automated)
# ─────────────────────────────────────────────

def phase_8_loop_control(workspace: Path, max_iterations: int,
                         consecutive_discard_limit: int = 5,
                         layer_promotion_k: int = 5) -> dict:
    """Determine whether to continue, promote layer, or stop.

    Returns: {"continue", "reason", "promote_layer", "next_layer"}
    """

    rows = parse_results_tsv(workspace)
    n = len(rows)

    if n >= max_iterations:
        return {"continue": False, "reason": f"max_iterations ({max_iterations}) reached",
                "promote_layer": False, "next_layer": None}

    if not rows:
        return {"continue": True, "reason": "no iterations yet",
                "promote_layer": False, "next_layer": None}

    # Check consecutive discards in current layer
    current_layer = rows[-1].get("layer", "body")
    layer_rows = [r for r in rows if r.get("layer") == current_layer]
    recent_statuses = [r.get("status", "") for r in layer_rows[-layer_promotion_k:]]

    if (len(recent_statuses) >= layer_promotion_k and
            all(s in ("discard", "crash", "revert") for s in recent_statuses)):
        # Layer promotion
        layer_order = ["description", "body", "script"]
        try:
            idx = layer_order.index(current_layer)
            if idx < len(layer_order) - 1:
                next_layer = layer_order[idx + 1]
                return {"continue": True, "reason": f"promoting from {current_layer} to {next_layer}",
                        "promote_layer": True, "next_layer": next_layer}
            else:
                return {"continue": False, "reason": "all layers exhausted",
                        "promote_layer": False, "next_layer": None}
        except ValueError:
            pass

    # Check overall consecutive discards
    all_statuses = [r.get("status", "") for r in rows[-consecutive_discard_limit:]]
    if (len(all_statuses) >= consecutive_discard_limit and
            all(s in ("discard", "crash", "revert") for s in all_statuses)):
        return {"continue": True, "reason": "STUCK — switch to radical strategy",
                "promote_layer": False, "next_layer": None}

    return {"continue": True, "reason": "normal",
            "promote_layer": False, "next_layer": None}


# ─────────────────────────────────────────────
# Git helpers
# ─────────────────────────────────────────────

def git_revert_last(skill_path: Path) -> dict:
    """Revert the last commit (for discard/revert actions)."""
    try:
        result = subprocess.run(
            ["git", "revert", "HEAD", "--no-edit"],
            cwd=str(skill_path), capture_output=True, text=True, timeout=10,
        )
        return {"success": result.returncode == 0, "output": result.stdout + result.stderr}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "output": str(e)}


def save_best_version(skill_path: Path, workspace: Path, iteration: int) -> str:
    """Copy current skill to best_versions/."""
    import shutil
    dest = workspace / "evolve" / "best_versions" / f"iteration-{iteration}"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(skill_path, dest, ignore=shutil.ignore_patterns('.git'))
    return str(dest)


# ─────────────────────────────────────────────
# LLM backends, phase 2+3, L2 eval, and auto_construct_gt all live in
# llm.py — imported at the top of this module and re-exported for
# back-compat with any `from evolve_loop import _call_llm` callers
# (notably evaluators.py's lazy import path for BinaryLLMJudge).
# ─────────────────────────────────────────────


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
# Eval Viewer Integration
# ─────────────────────────────────────────────

def _try_launch_eval_viewer(workspace: Path, skill_path: Path) -> bool:
    """Try to launch Creator's eval viewer (generate_review.py) if available.

    Generates a static HTML review of the evolution results.
    Returns True if viewer was launched successfully.
    """
    creator_path = require_creator()

    viewer_script = creator_path / "eval-viewer" / "generate_review.py"
    if not viewer_script.exists():
        return False

    # Parse skill name for the viewer
    try:
        name, _, _ = parse_skill_md(skill_path)
    except (ValueError, FileNotFoundError):
        name = skill_path.name

    # Find the latest benchmark file. Sort numerically by iteration so
    # iteration-E10 ranks after iteration-E9 (lex sort would render the
    # stale E9 benchmark as "latest" once the run hits 10+ iterations).
    evolve_dir = workspace / "evolve"
    benchmark_path = None
    iter_dirs = [
        d for d in evolve_dir.iterdir()
        if d.is_dir() and d.name.startswith("iteration-E")
    ]
    for d in sorted(iter_dirs, key=lambda p: _iter_num(p.name), reverse=True):
        bp = d / "benchmark.json"
        if bp.exists():
            benchmark_path = bp
            break

    try:
        cmd = [
            sys.executable, str(viewer_script),
            str(workspace),
            "--skill-name", name,
            "--static", str(workspace / "evolve" / "review.html"),
        ]
        if benchmark_path:
            cmd.extend(["--benchmark", str(benchmark_path)])

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print(f"Review saved: {workspace / 'evolve' / 'review.html'}",
                  file=sys.stderr)
            return True
    except (subprocess.TimeoutExpired, OSError):
        pass

    return False


# ─────────────────────────────────────────────
# Cleanup helpers
# ─────────────────────────────────────────────

def _iter_num(name: str) -> int:
    """Extract the trailing integer from an iteration directory name.

    Handles both ``iteration-<N>`` (best_versions) and ``iteration-E<N>``
    (eval output) forms. Returns -1 for anything that doesn't match so
    unexpected entries sort first and get pruned first rather than
    silently shadowing real iterations.
    """
    import re
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else -1


def cleanup_best_versions(workspace: Path, keep_n: int = 3) -> list[str]:
    """Remove old best_versions, keeping only the most recent N.

    Sorts by iteration NUMBER, not string — otherwise ``iteration-10``
    sorts before ``iteration-2`` under lexicographic order and the newest
    iterations would get pruned once the run hits 10+ iterations.
    """
    import shutil
    bv_dir = workspace / "evolve" / "best_versions"
    if not bv_dir.exists():
        return []
    dirs = sorted(bv_dir.iterdir(), key=lambda d: _iter_num(d.name))
    removed = []
    while len(dirs) > keep_n:
        old = dirs.pop(0)
        if old.is_dir():
            shutil.rmtree(old)
            removed.append(str(old))
    return removed


def cleanup_eval_outputs(workspace: Path, keep_recent: int = 5) -> list[str]:
    """Remove old iteration-EN/ dirs, keeping recent N and all 'keep' iterations.

    Uses numeric sort (see _iter_num) so ``iteration-E10`` correctly ranks
    after ``iteration-E9`` — lexicographic sort would delete the newest
    iterations at 10+ rounds.
    """
    import shutil

    evolve_dir = workspace / "evolve"
    rows = parse_results_tsv(workspace)

    # Find which iterations were 'keep'
    keep_iters = {r.get("iteration") for r in rows if r.get("status") == "keep"}

    # List all iteration-E* dirs, numerically sorted by suffix.
    iter_dirs = sorted(
        [d for d in evolve_dir.iterdir() if d.is_dir() and d.name.startswith("iteration-E")],
        key=lambda d: _iter_num(d.name),
    )

    # Determine which to keep
    recent_dirs = set(d.name for d in iter_dirs[-keep_recent:])
    keep_dir_names = set()
    for ki in keep_iters:
        keep_dir_names.add(f"iteration-E{ki}")

    removed = []
    for d in iter_dirs:
        if d.name not in recent_dirs and d.name not in keep_dir_names:
            shutil.rmtree(d)
            removed.append(str(d))
    return removed


# auto_construct_gt moved to llm.py — re-exported at the top of this
# module for back-compat.


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
        evaluators_info = {k: v.__name__ for k, v in
                          __import__("evaluators").EVALUATOR_REGISTRY.items()}
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
