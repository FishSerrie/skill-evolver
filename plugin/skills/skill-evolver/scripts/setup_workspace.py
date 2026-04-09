#!/usr/bin/env python3
"""Set up evolve workspace for a target skill.

Usage: python setup_workspace.py <target-skill-path> [--workspace <path>]

Creates the evolve/ subdirectory within <skill-name>-workspace/, initializes
results.tsv, and generates an evolve_plan.md template.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow importing siblings
sys.path.insert(0, str(Path(__file__).parent))
from common import find_workspace, parse_skill_md


def setup_workspace(skill_path: Path, workspace: Path | None = None) -> dict:
    """Create workspace evolve/ structure for a target skill.

    Returns dict with created paths.
    """
    skill_path = skill_path.resolve()
    ws = (workspace or find_workspace(skill_path)).resolve()
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
        try:
            name, description, _ = parse_skill_md(skill_path)
        except (ValueError, FileNotFoundError):
            name = skill_path.name
            description = "(could not parse SKILL.md)"

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
> Skill: {name}
> Description: {description[:100]}...

## Evaluation Philosophy

LLM does binary classification only; programs do all scoring.
Same classification always produces the same score.

Assertion types:
- Program-only: contains, not_contains, regex, file_exists, json_schema, script_check
- LLM binary (YES/NO): path_hit, fact_coverage

## Skill Analysis
- Type: TODO — analyze SKILL.md to determine
- Complexity: TODO
- GT data: {gt_info}
- Key assertion types: TODO

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
*This is a template. Claude should analyze the skill and GT data to fill in TODOs before starting evolve.*
"""
        plan_path.write_text(plan_content)
        created.append(str(plan_path))

    return {
        "workspace": str(ws),
        "evolve_dir": str(evolve_dir),
        "created": created,
        "skill_name": skill_path.name,
    }


def main():
    parser = argparse.ArgumentParser(description="Set up evolve workspace")
    parser.add_argument("skill_path", type=Path, help="Path to target skill directory")
    parser.add_argument("--workspace", type=Path, default=None, help="Override workspace path")
    args = parser.parse_args()

    if not args.skill_path.is_dir():
        print(f"Error: Skill directory not found: {args.skill_path}", file=sys.stderr)
        sys.exit(1)

    result = setup_workspace(args.skill_path, args.workspace)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
