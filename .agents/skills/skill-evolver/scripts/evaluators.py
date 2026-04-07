#!/usr/bin/env python3
"""Pluggable Evaluator Interface for Skill Evolver.

The Evaluator is the abstraction layer between the evolve loop and any
evaluation engine. By default, skill-creator is used. Users can plug in
custom scripts, test frameworks, or alternative eval engines.

Usage:
    evaluator = get_evaluator(config)
    result = evaluator.quick_gate(skill_path, gt_path)
    result = evaluator.full_eval(skill_path, gt_path)

Evaluator Protocol — any evaluator must return this shape:
    {
        "pass_rate": float,       # 0.0 to 1.0
        "total_passed": int,
        "total_assertions": int,
        "failed": [{"case_id": ..., "assertion": ...}],
        "tokens": int,            # optional, 0 if not tracked
        "duration": float,        # optional, 0.0 if not tracked
    }
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from common import find_creator_path, validate_frontmatter


# ─────────────────────────────────────────────
# Evaluator Protocol (abstract base)
# ─────────────────────────────────────────────

class Evaluator(ABC):
    """Base class for all evaluators."""

    name: str = "base"

    @abstractmethod
    def quick_gate(self, skill_path: Path, gt_path: Path | None = None) -> dict:
        """Fast validation (seconds). Returns {"pass": bool, "checks": [...], "errors": [...]}."""
        ...

    @abstractmethod
    def full_eval(self, skill_path: Path, gt_path: Path,
                  split: str = "dev") -> dict:
        """Full evaluation against GT. Returns the standard result dict."""
        ...

    def info(self) -> dict:
        """Return evaluator metadata."""
        return {"name": self.name, "type": self.__class__.__name__}


# ─────────────────────────────────────────────
# Built-in: Local Evaluator (always available)
# ─────────────────────────────────────────────

class LocalEvaluator(Evaluator):
    """Built-in evaluator using simple text matching against SKILL.md content.

    Always available. No external dependencies. Used as fallback.
    Suitable for static assertion types: contains, not_contains, regex.
    """

    name = "local"

    def quick_gate(self, skill_path: Path, gt_path: Path | None = None) -> dict:
        from run_l1_gate import run_l1_gate
        return run_l1_gate(skill_path, gt_path)

    def full_eval(self, skill_path: Path, gt_path: Path,
                  split: str = "dev") -> dict:
        skill_content = (skill_path / "SKILL.md").read_text()
        data = json.loads(gt_path.read_text())

        cases = data if isinstance(data, list) else data.get("evals", [])
        if split:
            cases = [c for c in cases if c.get("split", "dev") == split]

        total_p = total_t = 0
        failed = []
        for c in cases:
            for a in c.get("assertions", []):
                total_t += 1
                ok = False
                atype = a.get("type", "contains")
                val = a.get("value", "")

                if atype == "contains":
                    ok = val.lower() in skill_content.lower()
                elif atype == "not_contains":
                    ok = val.lower() not in skill_content.lower()
                elif atype == "regex":
                    ok = bool(re.search(val, skill_content))
                elif atype == "file_exists":
                    ok = (skill_path / val).exists() if val else False
                else:
                    # Unknown assertion type — skip (don't fail)
                    ok = True

                if ok:
                    total_p += 1
                else:
                    failed.append({
                        "case_id": c.get("id", "?"),
                        "assertion": a.get("description", val),
                    })

        return {
            "pass_rate": total_p / total_t if total_t else 0,
            "total_passed": total_p,
            "total_assertions": total_t,
            "failed": failed,
            "tokens": 0,
            "duration": 0.0,
        }


# ─────────────────────────────────────────────
# Built-in: Creator Evaluator (skill-creator)
# ─────────────────────────────────────────────

class CreatorEvaluator(Evaluator):
    """Evaluator that uses skill-creator's evaluation capabilities.

    Uses codex -q to spawn evaluation subagents for behavior testing.
    Falls back to LocalEvaluator if creator or Codex CLI unavailable.
    """

    name = "creator"

    def __init__(self, model: str | None = None):
        self.model = model
        self.creator_path = find_creator_path()
        self._fallback = LocalEvaluator()

    def quick_gate(self, skill_path: Path, gt_path: Path | None = None) -> dict:
        # L1 gate always uses our built-in (fast, no LLM needed)
        return self._fallback.quick_gate(skill_path, gt_path)

    def full_eval(self, skill_path: Path, gt_path: Path,
                  split: str = "dev") -> dict:
        # Try codex -q grading first
        result = self._claude_eval(skill_path, gt_path, split)
        if result is not None:
            return result

        # Fallback to local eval
        return self._fallback.full_eval(skill_path, gt_path, split)

    def _claude_eval(self, skill_path: Path, gt_path: Path,
                     split: str) -> dict | None:
        """Use codex -q to evaluate. Returns None if unavailable."""
        data = json.loads(gt_path.read_text())
        cases = data if isinstance(data, list) else data.get("evals", [])
        if split:
            cases = [c for c in cases if c.get("split", "dev") == split]

        prompt = (
            f"You are a grader. Evaluate the skill at {skill_path / 'SKILL.md'} "
            f"against these test cases.\n\n"
            f"Read the SKILL.md first, then for each case, check every assertion "
            f"against the SKILL.md content.\n\n"
            f"Test cases:\n{json.dumps(cases, indent=2, ensure_ascii=False)}\n\n"
            f'Output EXACTLY this JSON format on the last line:\n'
            f'{{"pass_rate": 0.95, "total_passed": 19, "total_assertions": 20, '
            f'"failed": [{{"case_id": 1, "assertion": "description"}}]}}'
        )

        cmd = ["codex", "-q", prompt, "--output-format", "text"]
        if self.model:
            cmd.extend(["--model", self.model])

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, env=env,
            )
            for line in reversed(result.stdout.split("\n")):
                line = line.strip()
                if line.startswith("{") and "pass_rate" in line:
                    try:
                        parsed = json.loads(line)
                        parsed.setdefault("tokens", 0)
                        parsed.setdefault("duration", 0.0)
                        return parsed
                    except json.JSONDecodeError:
                        pass
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

        return None

    def info(self) -> dict:
        return {
            "name": self.name,
            "type": "CreatorEvaluator",
            "creator_path": str(self.creator_path) if self.creator_path else None,
            "model": self.model,
        }


# ─────────────────────────────────────────────
# Built-in: Script Evaluator (user-provided)
# ─────────────────────────────────────────────

class ScriptEvaluator(Evaluator):
    """Evaluator that runs a user-provided script.

    The script receives:
        argv[1] = skill_path
        argv[2] = gt_path
        argv[3] = split (optional)

    And must output JSON to stdout matching the Evaluator Protocol format:
        {"pass_rate": 0.85, "total_passed": 17, "total_assertions": 20, "failed": [...]}

    Configure in evolve_plan.md:
        evaluator: script
        evaluator_script: ./my_eval.py
    """

    name = "script"

    def __init__(self, script_path: str | Path, timeout: int = 300):
        self.script_path = Path(script_path).resolve()
        self.timeout = timeout
        self._fallback = LocalEvaluator()

        if not self.script_path.exists():
            raise FileNotFoundError(f"Evaluator script not found: {self.script_path}")

    def quick_gate(self, skill_path: Path, gt_path: Path | None = None) -> dict:
        # Use built-in L1 for fast gate
        return self._fallback.quick_gate(skill_path, gt_path)

    def full_eval(self, skill_path: Path, gt_path: Path,
                  split: str = "dev") -> dict:
        cmd = [sys.executable, str(self.script_path),
               str(skill_path), str(gt_path), split]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout,
            )

            if result.returncode != 0:
                return {
                    "pass_rate": 0.0,
                    "total_passed": 0,
                    "total_assertions": 0,
                    "failed": [{"case_id": "script", "assertion": f"Script failed: {result.stderr[:200]}"}],
                    "tokens": 0,
                    "duration": 0.0,
                }

            # Parse JSON from stdout (last JSON line)
            for line in reversed(result.stdout.strip().split("\n")):
                line = line.strip()
                if line.startswith("{") and "pass_rate" in line:
                    try:
                        parsed = json.loads(line)
                        parsed.setdefault("tokens", 0)
                        parsed.setdefault("duration", 0.0)
                        parsed.setdefault("total_passed", 0)
                        parsed.setdefault("total_assertions", 0)
                        parsed.setdefault("failed", [])
                        return parsed
                    except json.JSONDecodeError:
                        pass

            return {
                "pass_rate": 0.0,
                "total_passed": 0,
                "total_assertions": 0,
                "failed": [{"case_id": "script", "assertion": "Script did not output valid JSON"}],
                "tokens": 0,
                "duration": 0.0,
            }

        except subprocess.TimeoutExpired:
            return {
                "pass_rate": 0.0,
                "total_passed": 0,
                "total_assertions": 0,
                "failed": [{"case_id": "script", "assertion": f"Script timed out ({self.timeout}s)"}],
                "tokens": 0,
                "duration": 0.0,
            }

    def info(self) -> dict:
        return {
            "name": self.name,
            "type": "ScriptEvaluator",
            "script_path": str(self.script_path),
            "timeout": self.timeout,
        }


# ─────────────────────────────────────────────
# Built-in: Pytest Evaluator
# ─────────────────────────────────────────────

class PytestEvaluator(Evaluator):
    """Evaluator that runs pytest/jest and counts pass/fail.

    Configure in evolve_plan.md:
        evaluator: pytest
        evaluator_test_cmd: pytest tests/ -v --tb=short
    """

    name = "pytest"

    def __init__(self, test_cmd: str = "pytest tests/ -v --tb=short",
                 timeout: int = 300):
        self.test_cmd = test_cmd
        self.timeout = timeout
        self._fallback = LocalEvaluator()

    def quick_gate(self, skill_path: Path, gt_path: Path | None = None) -> dict:
        return self._fallback.quick_gate(skill_path, gt_path)

    def full_eval(self, skill_path: Path, gt_path: Path,
                  split: str = "dev") -> dict:
        try:
            result = subprocess.run(
                self.test_cmd.split(),
                capture_output=True, text=True, timeout=self.timeout,
                cwd=str(skill_path.parent),
            )

            # Parse pytest output for pass/fail counts
            output = result.stdout + result.stderr
            passed = failed_count = 0

            # Match "X passed, Y failed" pattern
            import re as _re
            match = _re.search(r"(\d+) passed", output)
            if match:
                passed = int(match.group(1))
            match = _re.search(r"(\d+) failed", output)
            if match:
                failed_count = int(match.group(1))

            total = passed + failed_count
            if total == 0:
                total = 1  # avoid division by zero

            return {
                "pass_rate": passed / total,
                "total_passed": passed,
                "total_assertions": total,
                "failed": [{"case_id": "pytest", "assertion": f"{failed_count} tests failed"}] if failed_count else [],
                "tokens": 0,
                "duration": 0.0,
            }

        except (subprocess.TimeoutExpired, OSError) as e:
            return {
                "pass_rate": 0.0,
                "total_passed": 0,
                "total_assertions": 0,
                "failed": [{"case_id": "pytest", "assertion": str(e)}],
                "tokens": 0,
                "duration": 0.0,
            }

    def info(self) -> dict:
        return {
            "name": self.name,
            "type": "PytestEvaluator",
            "test_cmd": self.test_cmd,
        }


# ─────────────────────────────────────────────
# Factory: get_evaluator()
# ─────────────────────────────────────────────

# Registry of built-in evaluators
EVALUATOR_REGISTRY: dict[str, type[Evaluator]] = {
    "local": LocalEvaluator,
    "creator": CreatorEvaluator,
    "script": ScriptEvaluator,
    "pytest": PytestEvaluator,
}


def get_evaluator(config: dict[str, Any] | None = None) -> Evaluator:
    """Create an evaluator from config.

    Config keys:
        evaluator: str          — "local" | "creator" | "script" | "pytest" (default: "creator")
        evaluator_script: str   — path to script (for ScriptEvaluator)
        evaluator_test_cmd: str — test command (for PytestEvaluator)
        model: str              — LLM model (for CreatorEvaluator)
        evaluator_timeout: int  — timeout in seconds

    Usage:
        evaluator = get_evaluator({"evaluator": "script", "evaluator_script": "./my_eval.py"})
        result = evaluator.full_eval(skill_path, gt_path)
    """
    config = config or {}
    name = config.get("evaluator", "creator")

    if name == "creator":
        return CreatorEvaluator(model=config.get("model"))
    elif name == "script":
        script = config.get("evaluator_script")
        if not script:
            raise ValueError("ScriptEvaluator requires 'evaluator_script' in config")
        return ScriptEvaluator(
            script_path=script,
            timeout=config.get("evaluator_timeout", 300),
        )
    elif name == "pytest":
        return PytestEvaluator(
            test_cmd=config.get("evaluator_test_cmd", "pytest tests/ -v --tb=short"),
            timeout=config.get("evaluator_timeout", 300),
        )
    elif name == "local":
        return LocalEvaluator()
    else:
        raise ValueError(
            f"Unknown evaluator '{name}'. "
            f"Available: {', '.join(EVALUATOR_REGISTRY.keys())}"
        )


def parse_evaluator_from_plan(plan_path: Path) -> dict[str, Any]:
    """Extract evaluator config from evolve_plan.md.

    Looks for lines like:
        evaluator: script
        evaluator_script: ./my_eval.py
        evaluator_timeout: 300
    """
    config: dict[str, Any] = {}

    if not plan_path.exists():
        return config

    content = plan_path.read_text()
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("- evaluator:") or line.startswith("evaluator:"):
            val = line.split(":", 1)[1].strip()
            config["evaluator"] = val
        elif line.startswith("- evaluator_script:") or line.startswith("evaluator_script:"):
            val = line.split(":", 1)[1].strip()
            config["evaluator_script"] = val
        elif line.startswith("- evaluator_test_cmd:") or line.startswith("evaluator_test_cmd:"):
            val = line.split(":", 1)[1].strip()
            config["evaluator_test_cmd"] = val
        elif line.startswith("- evaluator_timeout:") or line.startswith("evaluator_timeout:"):
            val = line.split(":", 1)[1].strip()
            try:
                config["evaluator_timeout"] = int(val)
            except ValueError:
                pass

    return config
