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

    # Create directories
    dirs_to_create = [
        ws,
        ws / "evals",
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

## Skill 分析
- 类型：TODO — 分析 SKILL.md 确定
- 复杂度：TODO
- GT 数据：{gt_info}
- 关键 assertion 类型：TODO

## 评测策略

### Quick Gate（每轮必跑）
- YAML frontmatter 语法检查
- trigger 抽样 3 条
- hard assertion 抽样 2 条核心 dev case

### Dev Eval（每轮跑）
- 跑 dev split 全部 case
- 重点关注：TODO
- 调用 Creator 的 grader 协议打分

### Strict Eval（触发条件）
- 每 5 轮自动触发
- 或 dev pass_rate 超过 baseline + 10% 时
- 跑 holdout + regression

## 优化优先级
1. Layer 2 (Body)：TODO
2. TODO

## 门控阈值
- min_delta: 0.02
- trigger_tolerance: 0.05
- max_token_increase: 0.20
- regression_tolerance: 0.05

## 终止条件
- max_iterations: 20
- stuck_threshold: 连续 5 轮 discard
- exhaustion: 3 层都尝试后无提升

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
