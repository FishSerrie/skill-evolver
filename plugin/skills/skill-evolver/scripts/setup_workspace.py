#!/usr/bin/env python3
"""Set up an evolve workspace for a target.

Usage: python setup_workspace.py <target-path> [--section<heading>]
                [--workspace <path>]

The target may be a skill directory, a standalone prompt file, or one
section of a file (with``--section``). Which of those it is is decided by
``target.resolve_target``, not here — see that module for why the decision
lives in exactly one place.

Creates the evolve/ subdirectory within<target-name>-workspace/,
initializes results.tsv, and generates an evolve_plan.md template.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow importing siblings
sys.path.insert(0, str(Path(__file__).parent))
from target import Target, resolve_target


def setup_workspace(target: Target, workspace: Path | None = None) -> dict:
    """Create the workspace evolve/ structure for a target.

    Takes a :class:`Target`, never a path. Accepting both would mean
    inspecting the argument's type here, and the one place allowed to
    decide what shape an artifact has is ``resolve_target`` — callers
    holding a path resolve it there, which keeps that decision in a single
    location instead of one copy per entry point.

    Returns dict with created paths.
    """
    ws = (workspace or target.workspace).resolve()
    evolve_dir = ws / "evolve"

    # Create directories. evals/checks/ is the canonical home for
    # GT-referenced script_check helper scripts — creating it here
    # instantiates the convention documented in eval_strategy.md so
    # fresh workspaces don't force users to mkdir it the first time
    # they write a script_check assertion.
    dirs_to_create = [
        ws,
        ws / "evals",
        ws / "evals" / "checks",
        evolve_dir,
        evolve_dir / "best_versions",
    ]
    created = []
    for d in dirs_to_create:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d))

    # Initialize results.tsv if not exists
    results_tsv = evolve_dir / "results.tsv"
    if not results_tsv.exists():
        header = (
            "# metric_direction: higher_is_better\n"
            "iteration\tcommit\tmetric\tdelta\ttrigger_f1\t"
            "tokens\tguard\tstatus\tlayer\tdescription\n"
        )
        results_tsv.write_text(header)
        created.append(str(results_tsv))

    # Initialize experiments.jsonl if not exists
    experiments = evolve_dir / "experiments.jsonl"
    if not experiments.exists():
        experiments.write_text("")
        created.append(str(experiments))

    # Generate evolve_plan.md template if not exists
    plan_path = evolve_dir / "evolve_plan.md"
    if not plan_path.exists():
        name = target.name
        description = target.summary()

        # Count GT cases if evals exist
        gt_info = "No GT data found yet."
        evals_json = ws / "evals" / "evals.json"
        if evals_json.exists():
            try:
                evals = json.loads(evals_json.read_text())
                n = len(evals.get("evals", []))
                gt_info = f"Found {n} eval cases in evals.json."
            except (json.JSONDecodeError, KeyError):
                pass

        plan_content = f"""# Evolve Plan for: {name}

> Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
> Target: {name} ({type(target).__name__})
> Path: {target.artifact_path}
> Summary: {description[:100]}...

## Evaluation Philosophy

LLM does binary classification only; programs do all scoring.
Same classification always produces the same score.

Assertion types:
- Program-only: contains, not_contains, regex, file_exists, json_schema, script_check
- LLM binary (YES/NO): path_hit, fact_coverage

## Target Analysis
- Type: TODO — analyze the target to determine
- Complexity: TODO
- GT data: {gt_info}
- Key assertion types: TODO

## Structural Baseline
{json.dumps(target.snapshot(), indent=2, ensure_ascii=False)}

## Evaluation Strategy

### Quick Gate (every iteration)
- YAML frontmatter syntax check
- Trigger sampling: 3 cases
- Hard assertion sampling: 2 core dev cases

### Dev Eval (every iteration)
- Run all dev split cases
- Focus areas: TODO
- Use binary LLM judge for semantic assertions

### Strict Eval (triggered conditionally)
- Auto-trigger every 5 iterations
- Or when dev pass_rate exceeds baseline + 10%
- Run holdout + regression sets
- Anti-Goodhart: holdout cases never exposed to proposer

evaluator: local
model:

## Optimization Priority
1. Layer 2 (Body): TODO
2. TODO

## Gate Thresholds
- min_delta: 0.02
- trigger_tolerance: 0.05
- max_token_increase: 0.20
- regression_tolerance: 0.05

## Loop Control
- max_iterations: 20 (hard terminate)
- exhaustion: all 3 layers attempted with no improvement (terminate)
- stuck_switch: 5 consecutive discards → switch to radical strategy (NOT terminate;
  phase_8_loop_control keeps running with a different ideation path)

---
*This is a template. Claude should analyze the target and GT data to fill in TODOs before starting evolve.*
"""
        plan_path.write_text(plan_content)
        created.append(str(plan_path))

    return {
        "workspace": str(ws),
        "evolve_dir": str(evolve_dir),
        "created": created,
        "target_name": target.name,
        "target_type": type(target).__name__,
        "target_path": str(target.artifact_path),
        # Retained under its original key so existing callers and log
        # parsers keep working; targets that are not skills report the
        # same value as `target_name`.
        "skill_name": target.name,
    }


def main():
    parser = argparse.ArgumentParser(description="Set up evolve workspace")
    parser.add_argument(
        "target_path", type=Path,
        help="Skill directory, or a prompt file",
    )
    parser.add_argument(
        "--section", default=None,
        help="Optimize only this heading's body within the file",
    )
    parser.add_argument("--workspace", type=Path, default=None,
                        help="Override workspace path")
    args = parser.parse_args()

    # Errors from resolve_target already say what is wrong and what the
    # valid alternative is, so they are reported verbatim rather than
    # replaced with a generic message that would lose that detail.
    # UnicodeDecodeError is included because a non-UTF-8 target is a
    # usage error, not a bug, and a traceback tells the user nothing they
    # can act on. OSError covers the unreadable-file case for the same
    # reason.
    try:
        target = resolve_target(args.target_path, args.section)
        result = setup_workspace(target, args.workspace)
    except UnicodeDecodeError as exc:
        print(
            f"Error: {args.target_path} is not valid UTF-8 text "
            f"({exc.reason}). Optimization targets must be text files.",
            file=sys.stderr,
        )
        sys.exit(1)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
