#!/usr/bin/env python3
"""Workspace cleanup + eval viewer integration, extracted from evolve_loop.py.

Contents:

  * ``_iter_num`` — numeric suffix extractor shared by every cleanup
    / sort path in the loop
  * ``cleanup_best_versions`` — prune old best_versions/iteration-N/
    snapshots (sorts numerically, not lex)
  * ``cleanup_eval_outputs`` — prune old iteration-EN/ eval output dirs
    (sorts numerically, keeps all 'keep' iterations)
  * ``_prepare_viewer_data`` — run skill on holdout prompts and build
    eval-viewer compatible directory structure with real outputs
  * ``_try_launch_eval_viewer`` — bridge to Creator's
    ``eval-viewer/generate_review.py`` for the post-run HTML review

Split rationale: every function here is about picking the RIGHT
iteration by number, which only started working correctly after
iter 3-4 of the self-evolve sprint. Keeping them together makes the
lex-sort bug class greppable from a single file — any future
iteration-dir consumer that forgets to use ``_iter_num`` is obvious
next to the three that already do.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import require_creator, parse_skill_md
from aggregate_results import parse_results_tsv
from llm import _call_llm


def _iter_num(name: str) -> int:
    """Extract the trailing integer from an iteration directory name.

    Handles both ``iteration-<N>`` (best_versions) and ``iteration-E<N>``
    (eval output) forms. Returns -1 for anything that doesn't match so
    unexpected entries sort first and get pruned first rather than
    silently shadowing real iterations.
    """
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else -1


def cleanup_best_versions(workspace: Path, keep_n: int = 3) -> list[str]:
    """Remove old best_versions, keeping only the most recent N.

    Sorts by iteration NUMBER, not string — otherwise ``iteration-10``
    sorts before ``iteration-2`` under lexicographic order and the newest
    iterations would get pruned once the run hits 10+ iterations.
    """
    bv_dir = workspace / "evolve" / "best_versions"
    if not bv_dir.exists():
        return []
    dirs = sorted(bv_dir.iterdir(), key=lambda d: _iter_num(d.name))
    removed: list[str] = []
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

    removed: list[str] = []
    for d in iter_dirs:
        if d.name not in recent_dirs and d.name not in keep_dir_names:
            shutil.rmtree(d)
            removed.append(str(d))
    return removed


# ─────────────────────────────────────────────
# Viewer data adapter: run skill → build outputs/ structure
# ─────────────────────────────────────────────

def _find_project_root(skill_path: Path) -> str:
    """Walk up from skill_path to find the git root (or fall back to parent)."""
    for parent in [skill_path] + list(skill_path.parents):
        if (parent / ".git").exists():
            return str(parent)
    return str(skill_path.parent)


def _run_skill_for_viewer(prompt: str, skill_path: Path,
                          timeout: int = 180) -> str:
    """Run ``claude -p`` with the skill project loaded and return response.

    Delegates to ``llm._call_llm`` with ``cwd`` set to the project root
    so Claude Code discovers and loads the skill automatically.
    """
    return _call_llm(prompt, timeout=timeout,
                     cwd=_find_project_root(skill_path))


def _format_grading(case: dict) -> dict:
    """Convert evolver assertion results to eval-viewer grading format.

    Eval-viewer expects::

        {"assertions": [{"name": str, "passed": bool, "evidence": str}]}
    """
    assertions = []
    for a in case.get("assertions", []):
        evidence_parts = []

        # Extract evidence from type-specific fields
        if a.get("match"):
            m = a["match"]
            evidence_parts.append(f"Found in {m.get('file', '?')} line {m.get('line', '?')}")
            if m.get("excerpt"):
                evidence_parts.append(f"Excerpt: {m['excerpt'][:200]}")
        if a.get("nearest_match") and not a.get("pass"):
            nm = a["nearest_match"]
            evidence_parts.append(f"Nearest match in {nm.get('file', '?')}: {nm.get('excerpt', '')[:200]}")
        if a.get("found_at"):
            fa = a["found_at"]
            evidence_parts.append(f"Unexpectedly found in {fa.get('file', '?')} line {fa.get('line', '?')}")
        if a.get("judge_reasoning"):
            evidence_parts.append(f"Judge: {a['judge_reasoning']}")
        if a.get("judge_verdicts"):
            for v in a["judge_verdicts"]:
                status = "✓" if v.get("verdict") else "✗"
                evidence_parts.append(f"  {status} {v.get('fact', '')}: {v.get('reasoning', '')}")
        if a.get("error"):
            evidence_parts.append(f"Error: {a['error']}")

        assertions.append({
            "name": a.get("description", a.get("value", f"assertion-{a.get('index', '?')}")),
            "passed": bool(a.get("pass", False)),
            "evidence": "\n".join(evidence_parts) if evidence_parts else ("PASS" if a.get("pass") else "FAIL"),
        })

    return {"assertions": assertions}


def _prepare_viewer_data(workspace: Path, holdout_result: dict,
                         skill_path: Path,
                         max_workers: int = 5) -> Path | None:
    """Run skill on holdout prompts and build eval-viewer compatible dir.

    For each holdout case:
    1. Run ``claude -p <prompt>`` in skill's project dir → real response
    2. Create viewer-compatible directory::

        viewer-data/case-NNN/
        ├── eval_metadata.json  ← {prompt, eval_id}
        ├── grading.json        ← {assertions: [{name, passed, evidence}]}
        └── outputs/
            └── response.md     ← actual claude response

    Returns the viewer-data directory path, or None on failure.
    """
    cases = holdout_result.get("cases", [])
    if not cases:
        return None

    viewer_dir = workspace / "viewer-data"
    # Clean rebuild
    if viewer_dir.exists():
        shutil.rmtree(viewer_dir)
    viewer_dir.mkdir(parents=True)

    print(f"Running skill on {len(cases)} holdout cases for eval viewer...",
          file=sys.stderr)

    # Collect prompts for parallel execution
    def _run_one(case: dict) -> tuple[dict, str]:
        prompt = case.get("prompt", "")
        if not prompt:
            return case, "(No prompt)"
        response = _run_skill_for_viewer(prompt, skill_path)
        return case, response

    # Run skill on all cases in parallel
    results: list[tuple[dict, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_run_one, c): c for c in cases}
        for fut in concurrent.futures.as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:
                c = futures[fut]
                results.append((c, f"(Error: {exc})"))

    # Sort by case_id for stable ordering
    results.sort(key=lambda r: r[0].get("case_id", 0))

    # Build viewer-compatible directory structure
    for case, response in results:
        case_id = case.get("case_id", 0)
        prompt = case.get("prompt", "")
        case_dir = viewer_dir / f"case-{case_id:03d}"
        outputs_dir = case_dir / "outputs"
        outputs_dir.mkdir(parents=True)

        # eval_metadata.json
        (case_dir / "eval_metadata.json").write_text(json.dumps({
            "prompt": prompt,
            "eval_id": case_id,
        }, ensure_ascii=False, indent=2))

        # grading.json
        grading = _format_grading(case)
        (case_dir / "grading.json").write_text(json.dumps(
            grading, ensure_ascii=False, indent=2))

        # outputs/response.md — the real skill response
        (outputs_dir / "response.md").write_text(response)

    print(f"Viewer data prepared: {len(results)} cases → {viewer_dir}",
          file=sys.stderr)
    return viewer_dir


def _try_launch_eval_viewer(workspace: Path, skill_path: Path,
                            viewer_data_dir: Path | None = None) -> bool:
    """Try to launch Creator's eval viewer (generate_review.py) if available.

    Launches an interactive HTTP server that auto-opens in the browser,
    AND saves a static HTML copy to ``<workspace>/evolve/review.html``
    for offline access. When ``viewer_data_dir`` is provided (from
    ``_prepare_viewer_data``), uses that directory as the workspace for
    the viewer so it finds the ``outputs/`` directories with real skill
    responses.

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

    # Find the latest meta.json. Sort numerically by iteration so
    # iteration-E10 ranks after iteration-E9 (lex sort would render
    # the stale E9 meta as "latest" once the run hits 10+ iterations).
    evolve_dir = workspace / "evolve"
    meta_path = None
    if evolve_dir.is_dir():
        iter_dirs = [
            d for d in evolve_dir.iterdir()
            if d.is_dir() and d.name.startswith("iteration-E")
        ]
        for d in sorted(iter_dirs, key=lambda p: _iter_num(p.name), reverse=True):
            mp = d / "meta.json"
            if mp.exists():
                meta_path = mp
                break

    # Use viewer_data_dir as the workspace if provided (has outputs/ structure),
    # otherwise fall back to the evolve workspace (legacy path).
    viewer_workspace = str(viewer_data_dir) if viewer_data_dir else str(workspace)

    # Base command shared by static and server modes
    base_cmd = [
        sys.executable, str(viewer_script),
        viewer_workspace,
        "--skill-name", name,
    ]
    if meta_path:
        base_cmd.extend(["--benchmark", str(meta_path)])

    # Step 1: Save a static HTML copy for offline access
    static_path = workspace / "evolve" / "review.html"
    try:
        result = subprocess.run(
            base_cmd + ["--static", str(static_path)],
            capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print(f"Review saved: {static_path}", file=sys.stderr)
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Step 2: Launch interactive HTTP server (auto-opens browser)
    try:
        process = subprocess.Popen(
            base_cmd,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        # Give it a moment to start and open the browser
        try:
            process.wait(timeout=3)
            if process.returncode != 0:
                print("Eval viewer server failed to start", file=sys.stderr)
        except subprocess.TimeoutExpired:
            # Still running — that's what we want (server is up)
            print(f"Eval viewer server running (pid {process.pid})",
                  file=sys.stderr)

        return True
    except OSError:
        # Server launch failed — still have the static HTML
        return static_path.exists()
