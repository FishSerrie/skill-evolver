#!/usr/bin/env python3
"""Aggregate evolution results from workspace.

Usage: python aggregate_results.py <workspace-path> [--format json|md|both]

Reads evolve/results.tsv and produces summary statistics.
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from evaluators import get_evaluator


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


# ─────────────────────────────────────────────
# Benchmark Mode: A/B comparison of two skills
# ─────────────────────────────────────────────

def run_benchmark(skill_a: Path, skill_b: Path, gt_path: Path,
                  evaluator_config: dict | None = None,
                  split: str = "dev") -> dict:
    """Run A/B benchmark comparison between two skills.

    Evaluates both skills against the same GT using the same evaluator,
    then produces a structured comparison report.

    Args:
        skill_a: Path to skill A directory (baseline).
        skill_b: Path to skill B directory (candidate).
        gt_path: Path to ground-truth JSON (evals.json).
        evaluator_config: Config dict for get_evaluator(). None = default.
        split: GT split to evaluate against.

    Returns:
        Structured comparison dict with per-case breakdown.
    """
    evaluator = get_evaluator(evaluator_config)

    print(f"[benchmark] Evaluating skill A: {skill_a}", file=sys.stderr)
    result_a = evaluator.full_eval(skill_a, gt_path, split=split)

    print(f"[benchmark] Evaluating skill B: {skill_b}", file=sys.stderr)
    result_b = evaluator.full_eval(skill_b, gt_path, split=split)

    # Build per-case comparison from failed lists (normalize to str)
    failed_a_ids = {str(f["case_id"]) for f in result_a.get("failed", [])}
    failed_b_ids = {str(f["case_id"]) for f in result_b.get("failed", [])}

    # Collect all case IDs from traces (traces keys are str)
    all_case_ids = sorted(
        set(result_a.get("traces", {}).keys())
        | set(result_b.get("traces", {}).keys())
        | failed_a_ids | failed_b_ids
    )

    per_case = []
    for cid in all_case_ids:
        a_pass = str(cid) not in failed_a_ids
        b_pass = str(cid) not in failed_b_ids
        per_case.append({
            "case_id": cid,
            "a_pass": a_pass,
            "b_pass": b_pass,
        })

    pr_a = result_a.get("pass_rate", 0.0)
    pr_b = result_b.get("pass_rate", 0.0)
    delta = pr_b - pr_a

    if abs(delta) < 1e-9:
        winner = "tie"
    elif delta > 0:
        winner = "b"
    else:
        winner = "a"

    return {
        "skill_a": {
            "path": str(skill_a),
            "pass_rate": pr_a,
            "failed": result_a.get("failed", []),
        },
        "skill_b": {
            "path": str(skill_b),
            "pass_rate": pr_b,
            "failed": result_b.get("failed", []),
        },
        "winner": winner,
        "delta": round(delta, 6),
        "per_case_comparison": per_case,
        "metadata": {
            "evaluator": evaluator.info(),
            "split": split,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tokens_a": result_a.get("tokens", 0),
            "tokens_b": result_b.get("tokens", 0),
            "duration_a": result_a.get("duration", 0),
            "duration_b": result_b.get("duration", 0),
        },
    }


def format_benchmark_markdown(report: dict) -> str:
    """Format a benchmark comparison report as markdown."""
    lines = ["# A/B Benchmark Comparison", ""]

    sa = report["skill_a"]
    sb = report["skill_b"]
    w = report["winner"]
    winner_label = {"a": "Skill A", "b": "Skill B", "tie": "Tie"}.get(w, w)

    lines.append("## Summary")
    lines.append(f"- **Skill A**: `{sa['path']}`")
    lines.append(f"- **Skill B**: `{sb['path']}`")
    lines.append(f"- **Winner**: {winner_label}")
    lines.append(f"- **Delta (B - A)**: {report['delta']:+.4f}")
    lines.append("")

    lines.append("## Pass Rates")
    lines.append(f"| Skill | Pass Rate |")
    lines.append(f"|-------|-----------|")
    lines.append(f"| A | {sa['pass_rate']:.2%} |")
    lines.append(f"| B | {sb['pass_rate']:.2%} |")
    lines.append("")

    pcc = report.get("per_case_comparison", [])
    if pcc:
        lines.append("## Per-Case Comparison")
        lines.append("| Case | A | B | Diff |")
        lines.append("|------|---|---|------|")
        for pc in pcc:
            a_mark = "PASS" if pc["a_pass"] else "FAIL"
            b_mark = "PASS" if pc["b_pass"] else "FAIL"
            if pc["a_pass"] == pc["b_pass"]:
                diff = ""
            elif pc["b_pass"]:
                diff = "B+"
            else:
                diff = "A+"
            lines.append(f"| {pc['case_id']} | {a_mark} | {b_mark} | {diff} |")
        lines.append("")

    # Failed assertions detail
    for label, skill in [("A", sa), ("B", sb)]:
        if skill["failed"]:
            lines.append(f"## Failures — Skill {label}")
            for f in skill["failed"]:
                lines.append(f"- **{f.get('case_id', '?')}**: {f.get('assertion', '')}")
            lines.append("")

    meta = report.get("metadata", {})
    if meta:
        lines.append("## Metadata")
        lines.append(f"- Split: {meta.get('split', '?')}")
        lines.append(f"- Evaluator: {meta.get('evaluator', {}).get('name', '?')}")
        lines.append(f"- Timestamp: {meta.get('timestamp', '?')}")
        lines.append(f"- Tokens: A={meta.get('tokens_a', 0)}, B={meta.get('tokens_b', 0)}")
        lines.append(f"- Duration: A={meta.get('duration_a', 0):.1f}s, B={meta.get('duration_b', 0):.1f}s")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Aggregate evolution results")
    parser.add_argument("workspace", nargs="?", type=Path,
                        help="Path to workspace directory (for aggregate mode)")
    parser.add_argument("--format", choices=["json", "md", "both"], default="both")

    # Benchmark mode
    parser.add_argument("--benchmark", nargs=2, metavar=("SKILL_A", "SKILL_B"),
                        help="Run A/B benchmark: paths to two skill directories")
    parser.add_argument("--gt", type=Path,
                        help="Path to ground-truth JSON (required for --benchmark)")
    parser.add_argument("--split", default="dev",
                        help="GT split to evaluate (default: dev)")
    parser.add_argument("--evaluator", default=None,
                        help="Evaluator name: local|creator|script|pytest")
    parser.add_argument("--model", default=None,
                        help="LLM model for evaluator")

    args = parser.parse_args()

    # ── Benchmark mode ──
    if args.benchmark:
        skill_a = Path(args.benchmark[0])
        skill_b = Path(args.benchmark[1])

        if not args.gt:
            print("Error: --gt is required for --benchmark mode", file=sys.stderr)
            sys.exit(1)
        if not args.gt.exists():
            print(f"Error: GT file not found: {args.gt}", file=sys.stderr)
            sys.exit(1)
        for label, p in [("skill_a", skill_a), ("skill_b", skill_b)]:
            if not p.exists():
                print(f"Error: {label} path not found: {p}", file=sys.stderr)
                sys.exit(1)

        evaluator_config = {}
        if args.evaluator:
            evaluator_config["evaluator"] = args.evaluator
        if args.model:
            evaluator_config["model"] = args.model

        report = run_benchmark(
            skill_a, skill_b, args.gt,
            evaluator_config=evaluator_config or None,
            split=args.split,
        )

        if args.format in ("json", "both"):
            print(json.dumps(report, indent=2, default=str))

        if args.format in ("md", "both"):
            md = format_benchmark_markdown(report)
            if args.format == "md":
                print(md)
            else:
                print("\n" + md, file=sys.stderr)

        return

    # ── Aggregate mode (original) ──
    if not args.workspace:
        print("Error: workspace path is required (or use --benchmark)", file=sys.stderr)
        sys.exit(1)

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
