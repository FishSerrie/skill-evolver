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

    # Meta-Harness §2 filesystem access: give the proposer file paths,
    # not preloaded content. The paper says the proposer "retrieves via
    # standard operations such as grep and cat rather than ingesting them
    # as a single prompt." So Phase 1 returns pointers + a few suggested
    # grep patterns; Claude/proposer uses Read/Grep selectively in the
    # next step (Phase 2 diagnosis).
    #
    # Why this matters: preloading all trace content into Phase 1's
    # return value violated the paper's access model AND blew up Phase 1
    # context size for long runs. With pointers, Phase 1 output stays
    # O(kilobytes) regardless of how many iterations have accumulated.
    last_iteration_dir = None
    last_meta_json = None
    cases_dir = None
    failed_case_paths: list[str] = []
    # Track which assertion types actually failed in the most recent
    # iteration so we can tailor suggested_greps to the specific
    # failure modes the proposer needs to diagnose (instead of a
    # one-size-fits-all hardcoded list).
    failed_assertion_types: set[str] = set()
    if rows:
        for row in reversed(rows):
            iter_num = row.get("iteration", 0)
            candidate_dir = evolve_dir / f"iteration-E{iter_num}"
            if (candidate_dir / "meta.json").exists():
                last_iteration_dir = str(
                    candidate_dir.relative_to(workspace))
                last_meta_json = str(
                    (candidate_dir / "meta.json").relative_to(workspace))
                candidate_cases = candidate_dir / "cases"
                if candidate_cases.is_dir():
                    cases_dir = str(candidate_cases.relative_to(workspace))
                    # Read each case JSON's summary + failing-assertion
                    # types. This is a small targeted read (only the
                    # summary block and assertion.type fields) — not a
                    # full trace ingestion — so Phase 1 output stays
                    # O(kilobytes) even with dozens of iterations.
                    case_files = sorted(
                        candidate_cases.glob("case_*.json"),
                        key=lambda p: _iter_num(p.stem),
                    )
                    for cf in case_files:
                        try:
                            data = json.loads(cf.read_text())
                        except (json.JSONDecodeError, OSError):
                            continue
                        summary_block = data.get("summary") or {}
                        if summary_block.get("failed", 0) > 0:
                            failed_case_paths.append(
                                str(cf.relative_to(workspace)))
                            # Which assertion TYPES failed? Used to
                            # tailor suggested_greps below.
                            for idx in summary_block.get("failed_indexes", []):
                                try:
                                    atype = data["assertions"][idx].get("type")
                                    if atype:
                                        failed_assertion_types.add(atype)
                                except (IndexError, KeyError, TypeError):
                                    continue
                break  # only the most recent iteration with meta.json

    # Build suggested_greps dynamically based on what actually failed.
    # Each pattern targets a specific type's rich fields so the
    # proposer has the right starting query for each class of
    # failure — much better than a generic "find all pass:false" list.
    suggested_greps: list[str] = []
    if cases_dir:
        suggested_greps.append(
            f"grep -l '\"pass\": false' {cases_dir}/*.json")
    else:
        # No cases dir yet (pre-baseline) — nothing else to suggest.
        pass

    if "contains" in failed_assertion_types:
        # nearest_match tells us if the needle was close-but-wrong
        # (match_ratio ~0.9) vs entirely absent (null).
        suggested_greps.append(
            "grep -A6 '\"nearest_match\"' "
            "evolve/iteration-E*/cases/*.json")

    if "not_contains" in failed_assertion_types:
        # found_at tells us WHERE the forbidden string lives.
        suggested_greps.append(
            "grep -A4 '\"found_at\"' "
            "evolve/iteration-E*/cases/*.json")

    if "script_check" in failed_assertion_types:
        # stdout/stderr capture is the whole reason the rich
        # tool-call trace exists — surface it directly.
        suggested_greps.append(
            "grep -A2 '\"stderr\"' "
            "evolve/iteration-E*/cases/*.json")

    if "path_hit" in failed_assertion_types:
        suggested_greps.append(
            "grep -A2 '\"judge_reasoning\"' "
            "evolve/iteration-E*/cases/*.json")

    if "fact_coverage" in failed_assertion_types:
        # judge_verdicts array — look at the individual fact-level
        # verdicts (some are usually true, the failing ones are the
        # diagnostic target).
        suggested_greps.append(
            "grep -A6 '\"judge_verdicts\"' "
            "evolve/iteration-E*/cases/*.json")

    if "regex" in failed_assertion_types or not failed_assertion_types:
        # Regex failures often give null nearest_match, so recommend
        # reading the failing case file directly. Also applies as a
        # generic fallback when we have no failure-type signal yet.
        suggested_greps.append(
            "grep -B1 '\"failed_indexes\":' "
            "evolve/iteration-E*/cases/*.json")

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
        # NEW: file paths for the proposer to grep/cat (paper §2 model)
        "last_iteration_dir": last_iteration_dir,
        "last_meta_json": last_meta_json,
        "cases_dir": cases_dir,
        "failed_case_paths": failed_case_paths,
        "suggested_greps": suggested_greps,
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

def write_cases_to_dir(cases_dir: Path,
                       cases: list | None) -> Path | None:
    """Write per-case structured JSON files to an explicit target directory.

    Low-level primitive. Does not know about workspace/iteration
    conventions — just takes a target directory and a list of case
    dicts and writes one ``case_{case_id}.json`` per entry. Creates the
    directory if it doesn't exist. Returns the directory on success, or
    None if ``cases`` is empty.

    Each case dict is expected to have at minimum ``case_id``, ``prompt``,
    ``assertions``, and ``summary`` fields (the shape produced by
    ``LocalEvaluator.full_eval``). The files are laid out to be
    grep-friendly so a proposer can ``grep -l '"pass": false'
    iteration-E*/cases/*.json`` to find failing cases across history,
    matching the Meta-Harness paper §2 filesystem access pattern
    (arXiv 2603.28052).

    Case ids are zero-padded to 3 digits in the filename
    (``case_003.json``) so lexicographic listing also gives numeric
    order for typical skill GT sizes (< 1000 cases). This eliminates
    the lex-sort bug family entirely for case file iteration.
    """
    if not cases:
        return None
    cases_dir = Path(cases_dir)
    cases_dir.mkdir(parents=True, exist_ok=True)
    for case in cases:
        case_id = case.get("case_id", "?")
        # Zero-pad for lex-sort friendliness (case_003 < case_010)
        try:
            file_name = f"case_{int(case_id):03d}.json"
        except (TypeError, ValueError):
            file_name = f"case_{case_id}.json"
        (cases_dir / file_name).write_text(
            json.dumps(case, indent=2, ensure_ascii=False)
        )
    return cases_dir


def persist_cases(workspace: Path, iteration: int,
                  cases: list | None) -> Path | None:
    """Write per-case structured JSON to ``iteration-E{N}/cases/``.

    Convention-path wrapper around :func:`write_cases_to_dir`. Used by
    ``phase_7_log`` (CLI ``--run`` mode); in-conversation callers can
    call this directly after ``LocalEvaluator.full_eval`` to persist
    ``result['cases']`` for the next iteration's Phase 1/2 diagnosis.

    Args:
        workspace: the skill's workspace directory.
        iteration: the E-iteration number the cases belong to.
        cases: list of per-case structured dicts, or None/empty to skip.

    Returns:
        Path to the created ``cases/`` directory, or None if nothing
        was written.
    """
    if not cases:
        return None
    return write_cases_to_dir(
        workspace / "evolve" / f"iteration-E{iteration}" / "cases",
        cases,
    )


def write_meta_json(workspace: Path, iteration: int,
                    commit: str, split: str,
                    eval_result: dict) -> Path:
    """Write iteration-E{N}/meta.json — per-iteration metadata + aggregate.

    meta.json replaces the old benchmark.json. It contains:
      - iteration number, timestamp, commit hash, split
      - aggregate stats (total cases, total assertions, pass rate)
      - cases_dir pointer + list of case ids written to that dir

    The paper §2 filesystem model stores source code + scores +
    execution traces per candidate. In our layout:
      - source code → git (via commit hash recorded here)
      - scores      → results.tsv row (referenced by iteration number
                      recorded here; the aggregate sub-field is a
                      convenience snapshot for viewers that don't want
                      to tail results.tsv)
      - traces      → sibling cases/ directory, listed in cases_listed
    """
    from datetime import datetime, timezone
    evolve_dir = workspace / "evolve"
    iter_dir = evolve_dir / f"iteration-E{iteration}"
    iter_dir.mkdir(parents=True, exist_ok=True)

    cases = eval_result.get("cases") or []
    cases_listed = [c.get("case_id") for c in cases]

    meta = {
        "iteration": iteration,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": commit,
        "split": split,
        "aggregate": {
            "total_cases": len(cases),
            "total_assertions": eval_result.get("total_assertions", 0),
            "passed_assertions": eval_result.get("total_passed", 0),
            "pass_rate": round(eval_result.get("pass_rate", 0.0), 4),
            "tokens": eval_result.get("tokens", 0),
            "duration": eval_result.get("duration", 0.0),
        },
        "cases_dir": "cases/",
        "cases_listed": cases_listed,
    }
    out_path = iter_dir / "meta.json"
    out_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    return out_path


def phase_7_log(workspace: Path, iteration: int, commit: str,
                metric: float, delta: float, trigger_f1: float,
                tokens: int, guard: str, status: str,
                layer: str, description: str,
                experiment: dict | None = None,
                eval_result: dict | None = None,
                split: str = "dev") -> None:
    """Append to results.tsv, experiments.jsonl, and write per-iteration
    metadata + per-case trace files.

    Per-case structured traces are delegated to :func:`persist_cases`,
    the shared helper in-conversation executors can also call directly
    without going through the full phase_7_log pipeline. The
    per-iteration metadata (meta.json) is written by :func:`write_meta_json`.

    Together these land the Meta-Harness §2 filesystem layout:

        iteration-E{N}/
        ├── meta.json              # metadata + aggregate
        └── cases/
            └── case_{id}.json     # one structured file per GT case

    Args:
        eval_result: the full dict returned by
            ``LocalEvaluator.full_eval``. Contains ``cases`` (list of
            structured per-case dicts) and aggregate fields
            (``pass_rate``, ``total_assertions``, etc). Passed through
            to ``persist_cases`` and ``write_meta_json`` — if None or
            empty, neither meta.json nor cases/ are written (useful for
            pure logging calls that don't have eval output handy).
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

    # iteration-E{N}/meta.json + cases/ — paper §2 filesystem layout
    if eval_result:
        write_meta_json(workspace, iteration, commit, split, eval_result)
        persist_cases(workspace, iteration, eval_result.get("cases"))


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
