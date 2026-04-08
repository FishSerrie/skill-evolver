#!/usr/bin/env python3
"""L1 Quick Gate — fast validation before running full eval.

Usage: python run_l1_gate.py <skill-path> [--gt <gt-json>]

Exit code 0 = pass, 1 = fail.
Outputs JSON: {"pass": bool, "checks": [...], "errors": [...]}
"""

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import require_creator, CreatorNotFoundError, validate_frontmatter


def check_skill_structure(skill_path: Path) -> list[dict]:
    """Validate skill directory structure."""
    checks = []

    # SKILL.md exists
    skill_md = skill_path / "SKILL.md"
    checks.append({
        "name": "skill_md_exists",
        "pass": skill_md.exists(),
        "detail": "SKILL.md exists" if skill_md.exists() else "SKILL.md not found",
    })

    if not skill_md.exists():
        return checks

    # Frontmatter valid
    valid, msg = validate_frontmatter(skill_path)
    checks.append({
        "name": "frontmatter_valid",
        "pass": valid,
        "detail": msg,
    })

    # Check file not empty
    content = skill_md.read_text()
    body_start = content.find("---", 3)
    if body_start > 0:
        body = content[body_start + 3:].strip()
        has_body = len(body) > 10
    else:
        has_body = False
    checks.append({
        "name": "has_body",
        "pass": has_body,
        "detail": "SKILL.md has body content" if has_body else "SKILL.md body is empty or too short",
    })

    return checks


def quick_gt_sample(gt_path: Path, n_samples: int = 3) -> list[dict]:
    """Quick-sample a few GT cases and check basic structure."""
    checks = []

    try:
        data = json.loads(gt_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        checks.append({
            "name": "gt_readable",
            "pass": False,
            "detail": f"Cannot read GT file: {e}",
        })
        return checks

    # Support both flat list and {"evals": [...]} format
    if isinstance(data, list):
        cases = data
    elif isinstance(data, dict) and "evals" in data:
        cases = data["evals"]
    else:
        checks.append({
            "name": "gt_format",
            "pass": False,
            "detail": "GT must be a list or {evals: [...]}",
        })
        return checks

    checks.append({
        "name": "gt_readable",
        "pass": True,
        "detail": f"GT has {len(cases)} cases",
    })

    if not cases:
        checks.append({
            "name": "gt_nonempty",
            "pass": False,
            "detail": "GT has 0 cases",
        })
        return checks

    # Sample a few and check structure
    samples = random.sample(cases, min(n_samples, len(cases)))
    for i, case in enumerate(samples):
        has_prompt = "prompt" in case or "query" in case
        has_assertions = "assertions" in case or "expectations" in case or "expected_output" in case
        ok = has_prompt and has_assertions
        checks.append({
            "name": f"gt_case_{case.get('id', i)}_structure",
            "pass": ok,
            "detail": f"Case {case.get('id', i)}: prompt={'ok' if has_prompt else 'MISSING'}, "
                      f"assertions={'ok' if has_assertions else 'MISSING'}",
        })

    return checks


def creator_validate(skill_path: Path) -> list[dict]:
    """Run creator's quick_validate.py. Creator MUST be available."""
    creator = require_creator()  # raises CreatorNotFoundError if missing

    validate_script = creator / "scripts" / "quick_validate.py"
    if not validate_script.exists():
        return [{
            "name": "creator_validate",
            "pass": False,
            "detail": f"Creator's quick_validate.py not found at {validate_script}. "
                      "Your skill-creator installation may be incomplete or outdated.",
        }]

    try:
        result = subprocess.run(
            [sys.executable, str(validate_script), str(skill_path)],
            capture_output=True, text=True, timeout=10,
        )
        return [{
            "name": "creator_validate",
            "pass": result.returncode == 0,
            "detail": result.stdout.strip() or result.stderr.strip() or "creator validation complete",
        }]
    except subprocess.TimeoutExpired:
        return [{
            "name": "creator_validate",
            "pass": False,
            "detail": "Creator validation timed out (10s)",
        }]
    except OSError as e:
        return [{
            "name": "creator_validate",
            "pass": False,
            "detail": f"Creator validation error: {e}",
        }]


def run_l1_gate(skill_path: Path, gt_path: Path | None = None) -> dict:
    """Run L1 quick gate validation.

    Returns {"pass": bool, "checks": [...], "errors": [...]}.
    """
    all_checks = []

    # Structure checks
    all_checks.extend(check_skill_structure(skill_path))

    # Creator validation
    all_checks.extend(creator_validate(skill_path))

    # GT sampling
    if gt_path and gt_path.exists():
        all_checks.extend(quick_gt_sample(gt_path))

    errors = [c["detail"] for c in all_checks if not c["pass"]]
    return {"pass": len(errors) == 0, "checks": all_checks, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description="Run L1 gate validation")
    parser.add_argument("skill_path", type=Path, help="Path to skill directory")
    parser.add_argument("--gt", type=Path, default=None, help="Path to GT JSON file")
    args = parser.parse_args()

    if not args.skill_path.is_dir():
        result = {"pass": False, "checks": [], "errors": [f"Not a directory: {args.skill_path}"]}
        print(json.dumps(result))
        sys.exit(1)

    result = run_l1_gate(args.skill_path, args.gt)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["pass"] else 1)


if __name__ == "__main__":
    main()
