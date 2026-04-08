#!/usr/bin/env python3
"""L2 Dev Eval — behavior evaluation against GT assertions.

Usage: python run_l2_eval.py <skill-path> --gt <gt-json> --workspace <workspace>

This script provides library functions for L2 evaluation. The actual eval
execution (spawn subagent, run skill, collect output) is orchestrated by Claude.
This script handles: GT loading, result aggregation, and results.tsv writing.
"""

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import require_creator, find_evolve_dir


def load_gt(gt_path: Path, split: str | None = "dev") -> list[dict]:
    """Load GT cases, optionally filtering by split.

    Supports both flat list and {"evals": [...]} format.
    """
    data = json.loads(gt_path.read_text())

    if isinstance(data, list):
        cases = data
    elif isinstance(data, dict) and "evals" in data:
        cases = data["evals"]
    else:
        raise ValueError("GT must be a list or {evals: [...]}")

    if split:
        cases = [c for c in cases if c.get("split", "dev") == split]

    return cases


def calculate_stats(values: list[float]) -> dict:
    """Calculate mean, stddev, min, max."""
    if not values:
        return {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0}
    n = len(values)
    mean = sum(values) / n
    if n > 1:
        variance = sum((x - mean) ** 2 for x in values) / (n - 1)
        stddev = math.sqrt(variance)
    else:
        stddev = 0.0
    return {
        "mean": round(mean, 4),
        "stddev": round(stddev, 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def aggregate_grades(gradings: list[dict]) -> dict:
    """Aggregate individual grading results into benchmark.

    Each grading dict should have:
      case_id, assertions: [{passed, ...}], pass_rate, tokens, duration
    """
    if not gradings:
        return {"pass_rate": 0.0, "tokens_mean": 0, "duration_mean": 0, "n_cases": 0}

    pass_rates = [g.get("pass_rate", 0.0) for g in gradings]
    tokens = [g.get("tokens", 0) for g in gradings if g.get("tokens")]
    durations = [g.get("duration", 0) for g in gradings if g.get("duration")]

    total_assertions = sum(
        len(g.get("assertions", [])) for g in gradings
    )
    passed_assertions = sum(
        sum(1 for a in g.get("assertions", []) if a.get("passed"))
        for g in gradings
    )

    return {
        "pass_rate": round(passed_assertions / total_assertions, 4) if total_assertions else 0.0,
        "pass_rate_stats": calculate_stats(pass_rates),
        "tokens_stats": calculate_stats(tokens) if tokens else None,
        "duration_stats": calculate_stats(durations) if durations else None,
        "n_cases": len(gradings),
        "n_assertions": total_assertions,
        "n_passed": passed_assertions,
        "n_failed": total_assertions - passed_assertions,
    }


def write_benchmark(workspace: Path, iteration: int, benchmark: dict) -> Path:
    """Write benchmark.json to the evolve iteration directory."""
    evolve_dir = workspace / "evolve"
    iter_dir = evolve_dir / f"iteration-E{iteration}"
    iter_dir.mkdir(parents=True, exist_ok=True)

    out_path = iter_dir / "benchmark.json"
    out_path.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "iteration": iteration,
        **benchmark,
    }, indent=2))

    return out_path


def write_grading(workspace: Path, iteration: int, gradings: list[dict]) -> Path:
    """Write grading.json to the evolve iteration directory."""
    evolve_dir = workspace / "evolve"
    iter_dir = evolve_dir / f"iteration-E{iteration}"
    iter_dir.mkdir(parents=True, exist_ok=True)

    out_path = iter_dir / "grading.json"
    out_path.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "iteration": iteration,
        "cases": gradings,
    }, indent=2))

    return out_path


def main():
    parser = argparse.ArgumentParser(description="L2 eval helper")
    parser.add_argument("skill_path", type=Path, help="Path to skill directory")
    parser.add_argument("--gt", type=Path, required=True, help="Path to GT JSON")
    parser.add_argument("--workspace", type=Path, required=True, help="Path to workspace")
    parser.add_argument("--split", default="dev", help="GT split to use (default: dev)")
    parser.add_argument("--info", action="store_true", help="Just print GT info, don't run eval")
    args = parser.parse_args()

    cases = load_gt(args.gt, args.split)

    if args.info:
        print(json.dumps({
            "split": args.split,
            "n_cases": len(cases),
            "assertion_types": list(set(
                a["type"] for c in cases
                for a in c.get("assertions", [])
            )),
            "sample_ids": [c.get("id") for c in cases[:5]],
        }, indent=2))
        return

    print(f"L2 eval: {len(cases)} {args.split} cases loaded.", file=sys.stderr)
    print("Note: Actual eval execution requires Claude to orchestrate subagents.", file=sys.stderr)
    print("Use the library functions (load_gt, aggregate_grades, write_benchmark) programmatically.", file=sys.stderr)


if __name__ == "__main__":
    main()
