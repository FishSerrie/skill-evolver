#!/usr/bin/env python3
"""Alternative (pluggable) evaluator backends, extracted from evaluators.py.

evaluators.py owns the default path: the ``Evaluator`` ABC, the
``BinaryLLMJudge`` primitive, ``LocalEvaluator`` (the always-available
deterministic evaluator), and the ``get_evaluator`` factory.

THIS module owns the three *alternative* backends a user can opt into
via ``evolve_plan.md``:

  * ``CreatorEvaluator``  — wraps LocalEvaluator and additionally calls
    skill-creator's ``scripts/run_eval.py`` for trigger F1. Use when
    Creator's full eval pipeline is available and you want its
    trigger metric on top of the program-only GT checks.
  * ``ScriptEvaluator``   — shells out to a user-provided Python script
    that takes ``(skill_path, gt_path, split)`` on argv and returns a
    JSON result dict on stdout. Use when you already have a non-Python
    eval harness you want to plug in.
  * ``PytestEvaluator``   — shells out to ``pytest`` (or any test
    command) in the skill's parent directory and counts the ``N passed``
    / ``N failed`` markers. Use for code-generation skills whose
    ground truth is a test suite.

All three inherit from ``evaluators.Evaluator`` and ship with a
``LocalEvaluator`` fallback for the ``quick_gate`` path so L1 checks
stay fast and deterministic regardless of backend.

The ``get_evaluator`` factory in evaluators.py lazy-imports this module
only when one of these backends is requested — keeping the default
import path (``from evaluators import LocalEvaluator``) free of any
subprocess / CLI assumptions.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import find_creator_path
from evaluators import Evaluator, LocalEvaluator


# ─────────────────────────────────────────────
# CreatorEvaluator — LocalEvaluator + skill-creator trigger eval
# ─────────────────────────────────────────────

class CreatorEvaluator(Evaluator):
    """Evaluator using binary LLM judgment + program scoring.

    For each test case and each assertion:
      - Deterministic assertions (contains, regex, etc.) → program-only
      - Semantic assertions (path_hit, fact_coverage) → binary LLM call
    Program aggregates all binary results into final scores.

    Falls back to LocalEvaluator if claude CLI unavailable.
    """

    name = "creator"

    def __init__(self, model: str | None = None):
        self.model = model
        self.creator_path = find_creator_path()
        self._fallback = LocalEvaluator(model=model)

    def quick_gate(self, skill_path: Path, gt_path: Path | None = None) -> dict:
        return self._fallback.quick_gate(skill_path, gt_path)

    def full_eval(self, skill_path: Path, gt_path: Path,
                  split: str = "dev",
                  traces_dir: Path | None = None) -> dict:
        # CreatorEvaluator uses the same binary approach as LocalEvaluator
        # but can additionally invoke Creator's scripts for trigger testing.
        # Forward traces_dir so auto-persistence reaches the delegate.
        result = self._fallback.full_eval(
            skill_path, gt_path, split, traces_dir=traces_dir)

        # Try to enhance with Creator's trigger evaluation if available
        if self.creator_path:
            trigger_result = self._run_creator_trigger_eval(
                skill_path, gt_path, split)
            if trigger_result is not None:
                result["trigger_f1"] = trigger_result.get("f1", 1.0)
                result["tokens"] += trigger_result.get("tokens", 0)

        return result

    def _run_creator_trigger_eval(self, skill_path: Path, gt_path: Path,
                                  split: str) -> dict | None:
        """Run Creator's trigger evaluation script if available."""
        if not self.creator_path:
            return None

        run_eval = self.creator_path / "scripts" / "run_eval.py"
        if not run_eval.exists():
            return None

        try:
            cmd = [
                sys.executable, str(run_eval),
                "--eval-set", str(gt_path),
                "--skill-path", str(skill_path),
            ]
            if self.model:
                cmd.extend(["--model", self.model])

            t0 = time.time()
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
            )
            duration = time.time() - t0

            if result.returncode == 0:
                # Parse trigger results from stdout
                for line in reversed(result.stdout.strip().split("\n")):
                    if line.strip().startswith("{"):
                        try:
                            return json.loads(line.strip())
                        except json.JSONDecodeError:
                            pass
        except (subprocess.TimeoutExpired, OSError):
            pass

        return None

    def info(self) -> dict:
        return {
            "name": self.name,
            "type": "CreatorEvaluator",
            "creator_path": str(self.creator_path) if self.creator_path else None,
            "model": self.model,
            "philosophy": "LLM binary classification + program scoring",
        }


# ─────────────────────────────────────────────
# ScriptEvaluator — user-provided eval script
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
            raise FileNotFoundError(
                f"Evaluator script not found: {self.script_path}")

    def quick_gate(self, skill_path: Path, gt_path: Path | None = None) -> dict:
        return self._fallback.quick_gate(skill_path, gt_path)

    def full_eval(self, skill_path: Path, gt_path: Path,
                  split: str = "dev") -> dict:
        cmd = [sys.executable, str(self.script_path),
               str(skill_path), str(gt_path), split]

        t0 = time.time()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout,
            )
            duration = time.time() - t0

            if result.returncode != 0:
                return {
                    "pass_rate": 0.0,
                    "total_passed": 0,
                    "total_assertions": 0,
                    "failed": [{"case_id": "script",
                                "assertion": f"Script failed: {result.stderr[:200]}"}],
                    "tokens": 0,
                    "duration": round(duration, 2),
                    "traces": {"script_stderr": result.stderr[:2000]},
                }

            # Parse JSON from stdout (last JSON line)
            for line in reversed(result.stdout.strip().split("\n")):
                line = line.strip()
                if line.startswith("{") and "pass_rate" in line:
                    try:
                        parsed = json.loads(line)
                        parsed.setdefault("tokens", 0)
                        parsed.setdefault("duration", round(duration, 2))
                        parsed.setdefault("total_passed", 0)
                        parsed.setdefault("total_assertions", 0)
                        parsed.setdefault("failed", [])
                        parsed.setdefault("traces", {})
                        return parsed
                    except json.JSONDecodeError:
                        pass

            return {
                "pass_rate": 0.0,
                "total_passed": 0,
                "total_assertions": 0,
                "failed": [{"case_id": "script",
                            "assertion": "Script did not output valid JSON"}],
                "tokens": 0,
                "duration": round(duration, 2),
                "traces": {"script_stdout": result.stdout[:2000]},
            }

        except subprocess.TimeoutExpired:
            return {
                "pass_rate": 0.0,
                "total_passed": 0,
                "total_assertions": 0,
                "failed": [{"case_id": "script",
                            "assertion": f"Script timed out ({self.timeout}s)"}],
                "tokens": 0,
                "duration": float(self.timeout),
                "traces": {},
            }

    def info(self) -> dict:
        return {
            "name": self.name,
            "type": "ScriptEvaluator",
            "script_path": str(self.script_path),
            "timeout": self.timeout,
        }


# ─────────────────────────────────────────────
# PytestEvaluator — shell out to pytest/jest/etc.
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
        t0 = time.time()
        try:
            result = subprocess.run(
                self.test_cmd.split(),
                capture_output=True, text=True, timeout=self.timeout,
                cwd=str(skill_path.parent),
            )
            duration = time.time() - t0
            output = result.stdout + result.stderr

            passed = failed_count = 0
            match = re.search(r"(\d+) passed", output)
            if match:
                passed = int(match.group(1))
            match = re.search(r"(\d+) failed", output)
            if match:
                failed_count = int(match.group(1))

            total = passed + failed_count
            if total == 0:
                total = 1

            return {
                "pass_rate": passed / total,
                "total_passed": passed,
                "total_assertions": total,
                "failed": ([{"case_id": "pytest",
                             "assertion": f"{failed_count} tests failed"}]
                           if failed_count else []),
                "tokens": 0,
                "duration": round(duration, 2),
                "traces": {"pytest_output": output[:4000]},
            }

        except (subprocess.TimeoutExpired, OSError) as e:
            return {
                "pass_rate": 0.0,
                "total_passed": 0,
                "total_assertions": 0,
                "failed": [{"case_id": "pytest", "assertion": str(e)}],
                "tokens": 0,
                "duration": time.time() - t0,
                "traces": {},
            }

    def info(self) -> dict:
        return {
            "name": self.name,
            "type": "PytestEvaluator",
            "test_cmd": self.test_cmd,
        }
