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
from cleanup import (  # extracted in iter 17
    _iter_num, cleanup_best_versions, cleanup_eval_outputs,
    _try_launch_eval_viewer,
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

def _list_untracked(skill_path: Path) -> set[str]:
    """Return the set of untracked (but not ignored) file paths in the
    skill directory, relative to skill_path.

    Used by the orchestrator to snapshot the untracked set before and
    after ``phase_2_3_ideate_and_modify`` so the diff can be passed to
    ``phase_4_commit`` as ``new_files`` — the files the mutation
    legitimately added and wants staged by name.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(skill_path), capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return set()
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}
    except (subprocess.TimeoutExpired, OSError):
        return set()


def phase_4_commit(skill_path: Path, layer: str, description: str,
                   new_files: list[str] | None = None) -> dict:
    """Git add + commit the changes.

    Staging strategy (three layers of safety, accumulated across iters):

    * **Tracked modifications** — always staged via ``git add -u``
      (iter 8 safety: never sweep untracked debris the user may have
      dropped into the skill dir during the loop).
    * **Mutation-added new files** — staged explicitly by name via
      the ``new_files`` parameter. The caller (``run_evolve_loop``)
      snapshots the untracked file set before and after
      ``phase_2_3_ideate_and_modify`` and passes the diff here. This
      closes iter 8's only remaining gap: Layer 3 mutations that add
      a new helper script / reference file can now be committed
      automatically without re-opening the ``git add -A`` footgun.
    * **User-dropped debris** — files that appeared in the working
      tree during the iteration but were NOT reported by the
      orchestrator are left untouched. The Phase 0 clean-tree check
      (iter 7 + iter 12) guarantees the starting state is empty, so
      anything not in ``new_files`` is by elimination not from the
      mutation.

    Args:
        skill_path: the skill directory under git.
        layer: current mutation layer string (``description`` / ``body``
            / ``script``), used in the commit message prefix.
        description: one-sentence commit message body.
        new_files: optional list of paths (relative to ``skill_path``)
            that the mutation added. If provided, each is staged with
            ``git add <path>`` alongside the ``git add -u`` for
            tracked modifications. ``None`` or ``[]`` disables new-file
            staging — the legacy pre-iter-25 behavior.

    Returns: {"success", "commit_hash", "files_changed", "error"}
    """
    try:
        # Stage tracked modifications — iter 8 safety baseline.
        subprocess.run(["git", "add", "-u"], cwd=str(skill_path),
                       capture_output=True, timeout=10)

        # Stage mutation-added new files explicitly by name (iter 25).
        # Using explicit paths avoids `git add -A` (which would pull
        # in any untracked file, breaking the iter 8 safety invariant)
        # while still enabling Layer 3 new-file mutations.
        if new_files:
            for rel_path in new_files:
                # Defensive: don't let a path traverse out of the skill
                # dir via "..", and skip empties. Path.resolve() is
                # intentionally NOT used — we want the user-supplied
                # relative path to stay relative so git treats it
                # correctly against cwd=skill_path.
                if not rel_path or rel_path.startswith("/") or ".." in rel_path.split("/"):
                    continue
                subprocess.run(
                    ["git", "add", "--", rel_path],
                    cwd=str(skill_path),
                    capture_output=True, text=True, timeout=10,
                )

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

# _eval_holdout_or_none was moved to orchestrator.py in iter 18
# (it was only ever called by run_evolve_loop).


# ─────────────────────────────────────────────
# Phase 6: Gate Decision (fully automated)
# ─────────────────────────────────────────────

# phase_6_gate_decision lives in gate.py (imported at top of module).
# Re-exported via the top-level import for `from evolve_loop import ...`
# callers that still reference it as a sibling of other phase_* functions.


# ─────────────────────────────────────────────
# Phase 7: Log (fully automated)
# ─────────────────────────────────────────────

def write_traces_to_dir(traces_dir: Path,
                        traces: dict | None) -> Path | None:
    """Write per-case execution traces to an explicit target directory.

    Low-level primitive. Does not know about workspace/iteration
    conventions — just takes a target directory and a trace dict and
    writes one ``case_{case_id}.md`` per entry. Creates the directory
    if it doesn't exist. Returns the directory on success, or None if
    ``traces`` is empty.

    This is the shared helper used by both ``persist_traces`` (below,
    which layers on the workspace/iteration naming convention) and
    ``LocalEvaluator.full_eval`` (which takes an explicit ``traces_dir``
    kwarg for in-conversation callers who want Meta-Harness files
    written without going through the full phase_7_log pipeline).
    """
    if not traces:
        return None
    traces_dir = Path(traces_dir)
    traces_dir.mkdir(parents=True, exist_ok=True)
    for case_id, trace_content in traces.items():
        trace_file = traces_dir / f"case_{case_id}.md"
        trace_file.write_text(str(trace_content))
    return traces_dir


def persist_traces(workspace: Path, iteration: int,
                   traces: dict | None) -> Path | None:
    """Write per-case execution traces to ``iteration-E{N}/traces/``.

    Convention-path wrapper around :func:`write_traces_to_dir`. Used by
    ``phase_7_log`` (CLI ``--run`` mode); in-conversation callers can
    call this directly after ``LocalEvaluator.full_eval`` to persist
    ``result['traces']`` for the next iteration's Phase 1/2 diagnosis.

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
    return write_traces_to_dir(
        workspace / "evolve" / f"iteration-E{iteration}" / "traces",
        traces,
    )


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
# Orchestrator + CLI moved to orchestrator.py in iter 18
# ─────────────────────────────────────────────
#
# run_evolve_loop, main, and _eval_holdout_or_none now live in
# scripts/orchestrator.py. We lazy re-export them via __getattr__ so
# `from evolve_loop import run_evolve_loop` still works without
# forming a circular top-level import (orchestrator.py imports phase
# functions from this module at load time).

_ORCHESTRATOR_REEXPORTS = {
    "run_evolve_loop", "main", "_eval_holdout_or_none",
}


def __getattr__(name: str):
    """PEP 562 lazy module attribute for back-compat orchestrator re-exports."""
    if name in _ORCHESTRATOR_REEXPORTS:
        import importlib
        orch = importlib.import_module("orchestrator")
        return getattr(orch, name)
    raise AttributeError(f"module 'evolve_loop' has no attribute {name!r}")


if __name__ == "__main__":
    # Delegate CLI to the orchestrator module so `python evolve_loop.py`
    # continues to work without duplicating the argparse + error handling
    # plumbing.
    from orchestrator import main as _orchestrator_main
    try:
        _orchestrator_main()
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
