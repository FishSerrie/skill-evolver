#!/usr/bin/env python3
"""Aggregate evolution results from workspace.

Usage: python aggregate_results.py <workspace-path> [--format json|md|both]

Reads evolve/results.tsv and produces summary statistics.
"""

import argparse
import csv
import json
import sys
from io import StringIO
from pathlib import Path


def parse_results_tsv(workspace: Path) -> list[dict]:
    """Parse evolve/results.tsv into typed row dicts."""
    results_path = workspace / "evolve" / "results.tsv"
    if not results_path.exists():
        return []

    content = results_path.read_text()
    # Skip comment lines
    lines = [l for l in content.split("\n") if l and not l.startswith("#")]
    if not lines:
        return []

    reader = csv.DictReader(StringIO("\n".join(lines)), delimiter="\t")
    rows = []
    for row in reader:
        typed = {}
        for k, v in row.items():
            if k is None:
                continue
            v = (v or "").strip()
            if k == "iteration":
                try:
                    typed[k] = int(v)
                except ValueError:
                    typed[k] = v
            elif k in ("metric", "delta", "trigger_f1"):
                try:
                    typed[k] = float(v)
                except ValueError:
                    typed[k] = v
            elif k == "tokens":
                try:
                    typed[k] = int(v)
                except ValueError:
                    typed[k] = v
            else:
                typed[k] = v
        rows.append(typed)
    return rows


def calculate_summary(rows: list[dict]) -> dict:
    """Calculate aggregate summary from results rows."""
    if not rows:
        return {
            "total_iterations": 0, "keep_count": 0, "discard_count": 0,
            "crash_count": 0, "best_metric": None, "best_iteration": None,
            "latest_metric": None, "trajectory": [], "is_stuck": False,
        }

    statuses = [r.get("status", "").lower() for r in rows]
    keep_count = sum(1 for s in statuses if s == "keep")
    discard_count = sum(1 for s in statuses if s == "discard")
    crash_count = sum(1 for s in statuses if s in ("crash", "revert"))

    # Best metric
    metrics = [(r.get("iteration", 0), r["metric"]) for r in rows
               if isinstance(r.get("metric"), (int, float))]
    best_metric = best_iteration = None
    if metrics:
        best_iteration, best_metric = max(metrics, key=lambda x: x[1])

    latest_metric = None
    for r in reversed(rows):
        if isinstance(r.get("metric"), (int, float)):
            latest_metric = r["metric"]
            break

    # Trajectory: metric at each keep
    trajectory = []
    for r in rows:
        if r.get("status", "").lower() in ("keep", "baseline"):
            m = r.get("metric")
            if isinstance(m, (int, float)):
                trajectory.append({"iteration": r.get("iteration", "?"), "metric": m})

    # Stuck detection
    recent = statuses[-5:] if len(statuses) >= 5 else statuses
    is_stuck = len(recent) >= 5 and all(s in ("discard", "crash", "revert") for s in recent)

    return {
        "total_iterations": len(rows),
        "keep_count": keep_count,
        "discard_count": discard_count,
        "crash_count": crash_count,
        "best_metric": best_metric,
        "best_iteration": best_iteration,
        "latest_metric": latest_metric,
        "trajectory": trajectory,
        "is_stuck": is_stuck,
    }


def format_markdown(summary: dict, rows: list[dict]) -> str:
    """Format summary as markdown."""
    lines = ["# Evolution Results Summary", ""]

    if summary["total_iterations"] == 0:
        lines.append("_No iterations recorded yet._")
        return "\n".join(lines)

    lines.append("## Overview")
    lines.append(f"- **Total iterations**: {summary['total_iterations']}")
    lines.append(f"- **Kept**: {summary['keep_count']} | "
                 f"**Discarded**: {summary['discard_count']} | "
                 f"**Crashed**: {summary['crash_count']}")

    if summary["best_metric"] is not None:
        lines.append(f"- **Best metric**: {summary['best_metric']:.1f}% "
                     f"(iteration {summary['best_iteration']})")
    if summary["latest_metric"] is not None:
        lines.append(f"- **Latest metric**: {summary['latest_metric']:.1f}%")

    if summary.get("is_stuck"):
        lines.extend(["", "> **STUCK**: Last 5+ iterations all discarded/crashed."])

    if summary["trajectory"]:
        lines.extend(["", "## Trajectory", "", "| Iteration | Metric |", "|-----------|--------|"])
        for t in summary["trajectory"]:
            lines.append(f"| {t['iteration']} | {t['metric']:.1f}% |")

    # Recent rows
    lines.extend(["", "## Recent", "", "| Iter | Metric | Delta | Status | Layer | Description |",
                  "|------|--------|-------|--------|-------|-------------|"])
    for r in rows[-10:]:
        m = f"{r['metric']:.1f}" if isinstance(r.get("metric"), (int, float)) else str(r.get("metric", ""))
        d = f"{r['delta']:+.1f}" if isinstance(r.get("delta"), (int, float)) else str(r.get("delta", ""))
        lines.append(f"| {r.get('iteration', '?')} | {m} | {d} | "
                     f"{r.get('status', '')} | {r.get('layer', '')} | "
                     f"{str(r.get('description', ''))[:40]} |")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Aggregate evolution results")
    parser.add_argument("workspace", type=Path, help="Path to workspace directory")
    parser.add_argument("--format", choices=["json", "md", "both"], default="both")
    args = parser.parse_args()

    if not (args.workspace / "evolve" / "results.tsv").exists():
        print(f"Error: No results.tsv at {args.workspace / 'evolve' / 'results.tsv'}", file=sys.stderr)
        sys.exit(1)

    rows = parse_results_tsv(args.workspace)
    summary = calculate_summary(rows)

    if args.format in ("json", "both"):
        print(json.dumps(summary, indent=2, default=str))

    if args.format in ("md", "both"):
        md = format_markdown(summary, rows)
        if args.format == "md":
            print(md)
        else:
            md_path = args.workspace / "evolve" / "results_summary.md"
            md_path.write_text(md)
            print(f"\nMarkdown: {md_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
