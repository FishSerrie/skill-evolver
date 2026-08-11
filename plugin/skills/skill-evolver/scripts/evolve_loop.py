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
from common import find_workspace, validate_frontmatter, parse_skill_md
from aggregate_results import parse_results_tsv, calculate_summary
# Re-exported as well as used: external callers have long done
# `from evolve_loop import write_cases_to_dir`, and the function moving to
# its own module is not a reason to break them.
from case_store import write_cases_to_dir
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
# Git scope
#
# Every git command below is limited to the artifact's own pathspec, and
# that is the single property the loop's safety rests on. It is worth
# stating why, because the unscoped version looked correct and destroyed
# user work:
#
#   `git add -u` with no pathspec stages the whole working tree, not the
#   current directory's subtree (Git 2.0+). So a loop that changed
#   `prompts/answer.md` would also stage a user's unrelated edit to
#   `src/app.py`, commit both, and — when the gate rejected the
#   candidate — revert both. The user's work was then in neither the
#   working tree nor the index, and nothing could recover it.
#
# The invariant that prevents this is: **the experiment commit contains
# nothing but the artifact.** Undoing a commit is only safe if the commit
# had nothing of anyone else's in it, so the guarantee has to be
# established when committing, not worked around when reverting.
#
# Two consequences that are easy to get wrong:
#
#   * Narrowing `git add` is NOT enough. `git commit` commits the entire
#     index, including paths the *user* staged themselves before the run.
#     So the commit itself must carry the pathspec (`git commit -- <path>`),
#     which bypasses the index for everything outside it.
#   * The dirty-tree precondition must check **exactly the paths that get
#     staged** — no wider, no narrower. Wider, and any unrelated edit
#     anywhere in the user's repository refuses to let the loop run at
#     all. Narrower, and something reachable by the commit was never
#     checked. Equality is what makes "the commit contains only the
#     artifact" true.
# ─────────────────────────────────────────────

def _git(args: list[str], target, timeout: int = 10):
    """Run a git command at the target's repository root.

    Centralised for one reason: ``cwd`` must be a directory, and the
    engine used to pass the artifact path directly. For a skill that is
    a directory and it worked; for a prompt file it raised
    ``NotADirectoryError``, which several call sites caught and degraded
    to "no history" or "no untracked files" — so a file target reported
    an empty git log and an empty untracked set rather than an error.

    Running at ``vcs_root`` also makes every path in git's output
    repository-relative and therefore directly comparable with the
    pathspecs passed in, instead of relative to whichever directory the
    command happened to run in.
    """
    return subprocess.run(
        ["git"] + args, cwd=str(target.vcs_root),
        capture_output=True, text=True, timeout=timeout,
    )


# ─────────────────────────────────────────────
# Phase 0: Setup (fully automated)
# ─────────────────────────────────────────────

def phase_0_setup(skill_path: Path, gt_path: Path,
                  workspace: Path | None = None) -> dict:
    """Create workspace, initialize memory, generate evolve_plan template.

    On first use, auto-detects creator tools (skill-creator, third-party-creator, etc.)
    and configures the evaluation pipeline accordingly.

    Enforces the "clean artifact" precondition from
    ``references/evolve_protocol.md`` Phase 0. The check is scoped to the
    artifact rather than the whole repository, and the two facts are
    linked: it must cover exactly what ``phase_4_commit`` stages, because
    a discarded iteration undoes that commit. Checking *more* than the
    commit touches would refuse to run over unrelated edits elsewhere in
    the user's repository — which is its own kind of broken — and
    checking *less* would leave something in the commit that nobody
    verified was ours.

    Returns: {"workspace", "evolve_dir", "plan_path", "baseline_needed", "creator_config"}
    """
    from setup_workspace import setup_workspace  # noqa: sibling import
    from common import setup_creator_config
    from target import resolve_target  # noqa: sibling import

    target = resolve_target(skill_path)
    pathspec = target.vcs_pathspec

    # Precondition: the artifact must be under git AND its own paths must
    # be clean. Four-step decision tree mirrors evolve_protocol.md Phase 4:
    #   1. Already under git, artifact clean → proceed
    #   2. Already under git, artifact dirty → refuse (would co-opt user's work)
    #   3. Not under git, git installed → auto-init + initial commit
    #   4. Git not installed → refuse with install instructions
    try:
        status = _git(["status", "--porcelain", "--", pathspec], target)
    except FileNotFoundError as e:
        raise RuntimeError(
            f"Phase 0: git is not installed. Install git and retry:\n"
            f"  macOS:  brew install git  or  xcode-select --install\n"
            f"  Ubuntu: sudo apt-get install git\n"
            f"  CentOS: sudo yum install git\n"
            f"  Windows: https://git-scm.com/download/win"
        ) from e
    except (subprocess.TimeoutExpired, OSError) as e:
        raise RuntimeError(
            f"Phase 0: cannot run `git status` in {target.vcs_root}: {e}"
        ) from e

    if status.returncode != 0:
        # Not a git repo. Auto-init per protocol (step 3): git is
        # installed (we just ran it successfully enough to get a
        # non-zero exit), the user has authorized operating on this
        # artifact, and no prior commit means no user work to lose.
        #
        # Scoped to the artifact for the same reason every other command
        # is: `git add .` here would make the initial commit contain
        # whatever else happens to sit beside the artifact, and every
        # later revert would then be able to reach it.
        try:
            _git(["init"], target).check_returncode()
            _git(["add", "--", pathspec], target).check_returncode()
            _git(
                ["commit", "-m", "chore: init git for evolve tracking",
                 "--", pathspec],
                target,
            ).check_returncode()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            raise RuntimeError(
                f"Phase 0: auto-init failed in {target.vcs_root}: {e}\n"
                f"Run manually: git init && git add {pathspec} && "
                f"git commit -m 'init'"
            ) from e
    elif status.stdout.strip():
        # Already a git repo AND the artifact is dirty → refuse. Those
        # changes would land in the first experiment commit, and a
        # discarded iteration's revert would then delete them. Note this
        # reports only the artifact's own dirt: unrelated uncommitted
        # work elsewhere in the repository is none of the loop's
        # business, because nothing the loop commits can reach it.
        raise RuntimeError(
            f"Phase 0: {skill_path} has uncommitted changes. Commit or stash "
            f"them before running evolve — otherwise they would be swept "
            f"into the first experiment commit, and a discarded iteration "
            f"would revert them along with the experiment.\n\n"
            f"Dirty files:\n{status.stdout}"
        )

    ws = workspace or find_workspace(skill_path)
    result = setup_workspace(target, ws)

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
        skill_path: the artifact being optimized. Used to locate the
            repository, so the git log read happens inside it; earlier
            versions passed ``workspace.parent``, the GRANDPARENT of the
            skill and typically not a repository at all, so the log came
            back empty and Phase 2 had no history to reason from.

    Returns: {"iterations", "keeps", "discards", "stuck", "recent_failures",
              "successful_patterns", "current_best_metric", "git_log"}
    """
    from target import resolve_target  # noqa: sibling import

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

    # Git history for the artifact only. Scoped with a pathspec for the
    # same reason the staging is: on a repository that hosts more than
    # the artifact, an unscoped log is mostly other people's commits,
    # and Phase 2 would be diagnosing a history it did not create.
    git_log = ""
    try:
        target = resolve_target(skill_path)
        result = _git(
            ["log", "--oneline", "-15", "--", target.vcs_pathspec],
            target, timeout=5,
        )
        if result.returncode == 0:
            git_log = result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        # Degrading to an empty log is acceptable here and nowhere else
        # in this module: Phase 1's history is advisory input to a
        # proposer, so a missing one costs quality. Contrast
        # _list_untracked, where the same silence loses files.
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
    """Untracked (not ignored) paths inside the artifact, repo-relative.

    Used by the orchestrator to snapshot the untracked set before and
    after the mutation runs, so the difference can be handed to
    ``phase_4_commit`` as the files the mutation legitimately added.

    Raises ``RuntimeError`` rather than returning an empty set when git
    cannot be consulted. The empty set is a real answer — "the artifact
    has no untracked files" — and returning it for "we could not look"
    conflated two situations with opposite consequences: under the
    previous behaviour a file target made this raise ``NotADirectoryError``
    internally, the handler swallowed it, and every file a mutation added
    was silently left out of the commit. The log still said the commit
    succeeded, so the loop went on measuring a candidate whose new files
    were never committed and would be deleted by the next revert.

    Scoped to the artifact's pathspec, which is also what makes the
    caller's inference sound. "Phase 0 verified a clean start, therefore
    anything untracked now came from the mutation" only holds where
    Phase 0 actually checked, and Phase 0 checks the artifact. Outside
    it, a new file is just as likely to be the user's.
    """
    from target import resolve_target  # noqa: sibling import

    target = resolve_target(skill_path)
    try:
        result = _git(
            ["ls-files", "--others", "--exclude-standard",
             "--", target.vcs_pathspec],
            target,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise RuntimeError(
            f"cannot list untracked files under {skill_path}: {e}"
        ) from e
    if result.returncode != 0:
        raise RuntimeError(
            f"git ls-files failed for {skill_path}: "
            f"{result.stderr.strip() or f'exit {result.returncode}'}"
        )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def phase_4_commit(skill_path: Path, layer: str, description: str,
                   new_files: list[str] | None = None) -> dict:
    """Commit the artifact's changes, and nothing else.

    The commit is built with an explicit pathspec, which is the whole
    safety mechanism:

        git add   -- <new files>          # untracked additions, by name
        git commit -m <msg> -- <pathspec> # artifact paths only

    ``git commit -- <pathspec>`` rather than a plain ``git commit`` is
    load-bearing, and the reason is easy to miss: a plain commit records
    the entire index, so anything the *user* staged before the run —
    their own ``git add`` of an unrelated file — would be committed as
    part of the experiment. Narrowing ``git add`` alone does not prevent
    that, because the problem is not what we stage but what is already
    staged. A pathspec on the commit bypasses the index for everything
    outside it, and leaves the user's staged work staged.

    That is what makes a discarded iteration safe to undo. Undoing a
    commit can only be scoped to the artifact if the commit was; see
    :func:`git_revert_last`.

    Args:
        skill_path: the artifact being optimized.
        layer: current mutation layer string (``description`` / ``body``
            / ``script``), used in the commit message prefix.
        description: one-sentence commit message body.
        new_files: paths (repository-relative, as returned by
            :func:`_list_untracked`) that the mutation added. Untracked
            files need naming because a pathspec matches what git already
            knows about; paths outside the artifact are refused rather
            than staged.

    Returns: {"success", "commit_hash", "files_changed", "error"}
    """
    from target import resolve_target  # noqa: sibling import

    try:
        target = resolve_target(skill_path)
        pathspec = target.vcs_pathspec

        # Untracked additions must be named: a pathspec selects among
        # paths git already tracks, so a brand-new file is invisible to
        # `git commit -- <dir>` until it has been added once.
        for rel_path in new_files or []:
            if not _within_artifact(rel_path, target):
                # Refused, not staged. Outside the artifact there is no
                # basis for believing a new file came from the mutation
                # rather than from the user, and committing it would put
                # it within reach of a later revert.
                continue
            _git(["add", "--", rel_path], target)

        # Does the artifact have anything to commit? Scoped to the same
        # pathspec the commit uses, so the answer describes exactly what
        # is about to happen rather than the repository at large.
        status = _git(["status", "--porcelain", "--", pathspec], target)
        if not status.stdout.strip():
            return {"success": False, "commit_hash": None,
                    "files_changed": [], "error": "No changes to commit"}

        msg = f"experiment({layer}): {description}"
        result = _git(["commit", "-m", msg, "--", pathspec], target)
        if result.returncode != 0:
            return {"success": False, "commit_hash": None,
                    "files_changed": [],
                    "error": (result.stderr.strip() or result.stdout.strip())}

        commit_hash = _git(
            ["rev-parse", "--short", "HEAD"], target, timeout=5
        ).stdout.strip()

        diff_result = _git(
            ["diff", "--name-only", "HEAD~1", "--", pathspec],
            target, timeout=5,
        )
        files = [f.strip() for f in diff_result.stdout.strip().split("\n") if f.strip()]

        return {"success": True, "commit_hash": commit_hash,
                "files_changed": files, "error": None}
    except (subprocess.TimeoutExpired, OSError, RuntimeError, ValueError) as e:
        return {"success": False, "commit_hash": None,
                "files_changed": [], "error": str(e)}


def _within_artifact(rel_path: str, target) -> bool:
    """Whether ``rel_path`` (repository-relative) lies inside the artifact.

    Rejects absolute paths and ``..`` traversal before resolving, so a
    path cannot escape the artifact by being resolved somewhere else
    first. The comparison is then made on resolved paths, because a
    string prefix test would accept ``prompts/answer.md.bak`` as living
    inside ``prompts/answer.md``.

    A file-shaped artifact contains only itself. That is deliberate: a
    sibling file the mutation dropped next to a prompt is outside the
    region Phase 0 verified was clean, so there is no evidence it is
    ours rather than the user's.
    """
    if not rel_path or rel_path.startswith("/") or ".." in rel_path.split("/"):
        return False
    candidate = (target.vcs_root / rel_path).resolve()
    artifact = target.artifact_path.resolve()
    if not target.vcs_scope_is_tree:
        return candidate == artifact
    return candidate == artifact or artifact in candidate.parents


# ─────────────────────────────────────────────
# Phase 5: Verify — L1 gate (automated)
# L2 eval requires Claude orchestration (see run_l2_eval.py)
# ─────────────────────────────────────────────

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


def persist_holdout_cases(workspace: Path, iteration: int,
                          cases: list | None) -> Path | None:
    """Write per-case structured JSON to ``iteration-E{N}/holdout_cases/``.

    Sibling of :func:`persist_cases` but for the holdout split, in a
    SEPARATE directory (not ``cases/``). Architecture plan Module A
    §0.6 traced a real bug: ``orchestrator._eval_holdout_or_none`` only
    ever read ``result["pass_rate"]`` off the holdout ``full_eval()``
    result and discarded ``result["cases"]`` — so holdout case content
    was never written to disk at all. "The proposer can't see holdout"
    was therefore an accident of that discard, not an enforced
    guarantee. This function is the fix; ``isolation.build_diagnoser_
    prompt`` (scripts/isolation.py) is the other half — it only ever
    references ``cases/``, never ``holdout_cases/``, so the exclusion
    has an actual directory boundary to point at instead of relying on
    the accidental-discard behavior this function replaces.
    """
    if not cases:
        return None
    return write_cases_to_dir(
        workspace / "evolve" / f"iteration-E{iteration}" / "holdout_cases",
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

def git_revert_last(skill_path: Path, commit_hash: str | None = None) -> dict:
    """Undo an experiment commit, touching only the artifact.

    Implemented as restore-then-commit rather than ``git revert``:

        git restore --source=<commit>~1 --staged --worktree -- <pathspec>
        git commit -m 'Revert "..."' -- <pathspec>

    ``git revert`` was the obvious choice and is the wrong one, for two
    measured reasons.

    It accepts no pathspec, so its scope is the whole commit. That is
    safe *only* because :func:`phase_4_commit` puts nothing but the
    artifact in the commit — the safety comes from what was committed,
    never from how it is undone. Stating it the other way round is what
    produced the original bug: an unscoped ``git add -u`` swept a user's
    unrelated edit into the commit, and this function then dutifully
    deleted it, leaving no copy in the working tree or the index.

    It also refuses to run at all when the user has *staged* unrelated
    changes: ``error: your local changes would be overwritten by revert``,
    exit 128, nothing reverted. Since the orchestrator treats a failed
    revert as grounds for aborting the whole run, a user who happened to
    have something in their index would find the loop dying at its first
    discarded iteration. The restore/commit pair has no such
    precondition — verified with unrelated staged, unstaged and untracked
    changes present simultaneously, all three preserved.

    The result is equivalent in what matters: the artifact returns to its
    pre-experiment state, files the experiment added are deleted, and a
    real inverse commit records that it happened.

    Args:
        skill_path: the artifact, or the skill directory holding it.
        commit_hash: which commit to undo. Naming it is strongly preferred
            over the ``HEAD`` default, because ``HEAD~1`` only means "before
            the experiment" while the experiment is still the newest commit.
            Anything that lands a commit in between — a future phase, a
            concurrent process, a user committing mid-run — silently shifts
            what ``HEAD~1`` points at, and the artifact would be restored to
            some other revision with no error raised. Today's call sites do
            nothing of the sort (verified: no commit is created between
            ``phase_4_commit`` and either revert), so the default is correct
            *now*; passing the hash is what keeps it correct later.
    """
    from target import resolve_target  # noqa: sibling import

    try:
        target = resolve_target(skill_path)
        pathspec = target.vcs_pathspec
        base = f"{commit_hash}~1" if commit_hash else "HEAD~1"

        restore = _git(
            ["restore", f"--source={base}", "--staged", "--worktree",
             "--", pathspec],
            target,
        )
        if restore.returncode != 0:
            return {"success": False,
                    "output": (restore.stderr.strip() or restore.stdout.strip())}

        subject = _git(
            ["log", "-1", "--pretty=%s",
             *( [commit_hash] if commit_hash else [] )], target, timeout=5
        ).stdout.strip()
        commit = _git(
            ["commit", "-m", f'Revert "{subject}"', "--", pathspec], target
        )
        if commit.returncode != 0:
            return {"success": False,
                    "output": (commit.stderr.strip() or commit.stdout.strip())}
        return {"success": True, "output": commit.stdout + commit.stderr}
    except (subprocess.TimeoutExpired, OSError, RuntimeError, ValueError) as e:
        return {"success": False, "output": str(e)}



def save_best_version(skill_path: Path, workspace: Path, iteration: int) -> str:
    """Archive the current artifact under ``best_versions/``.

    Delegates the copy to the target because the two shapes need
    different operations: this used to call ``shutil.copytree``
    unconditionally, which raises ``NotADirectoryError`` for a prompt
    file — so for a file target the archive of best-scoring versions,
    the only record of what actually worked, could never be written.
    """
    from target import resolve_target  # noqa: sibling import

    target = resolve_target(skill_path)
    dest = workspace / "evolve" / "best_versions" / f"iteration-{iteration}"
    return str(target.copy_artifact_to(dest))


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
