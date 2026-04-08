#!/usr/bin/env python3
"""Workspace cleanup + eval viewer integration, extracted from evolve_loop.py.

Contents:

  * ``_iter_num`` — numeric suffix extractor shared by every cleanup
    / sort path in the loop
  * ``cleanup_best_versions`` — prune old best_versions/iteration-N/
    snapshots (sorts numerically, not lex)
  * ``cleanup_eval_outputs`` — prune old iteration-EN/ eval output dirs
    (sorts numerically, keeps all 'keep' iterations)
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

import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import require_creator, parse_skill_md
from aggregate_results import parse_results_tsv


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
