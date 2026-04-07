#!/usr/bin/env python3
"""Pluggable Evaluator Interface for Skill Evolver.

Design philosophy: "LLM does binary classification, programs do scoring."
  - LLM is only asked atomic YES/NO questions (semantic matching, fact coverage)
  - Programs handle all scoring, aggregation, and deterministic checks
  - Same classification results always produce the same score

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
        "tokens": int,            # total tokens consumed
        "duration": float,        # wall-clock seconds
        "traces": {               # full output per case (for Meta-Harness diagnosis)
            "case_1": "full skill output...",
            ...
        },
    }
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from common import find_creator_path, validate_frontmatter


# ─────────────────────────────────────────────
# BinaryLLMJudge — atomic YES/NO LLM calls
# ─────────────────────────────────────────────

class BinaryLLMJudge:
    """Makes atomic binary (YES/NO) calls to an LLM.

    Core principle: LLM only classifies, never scores.
    Each call asks exactly one question with a YES or NO answer.
    Programs aggregate the binary results into scores.

    Uses the pluggable backend system from evolve_loop.py — supports
    claude, codex, opencode, and HTTP endpoints. Auto-detects the
    available backend, or respects LLM_BACKEND env var.
    """

    def __init__(self, model: str | None = None, timeout: int = 60):
        self.model = model
        self.timeout = timeout
        self.total_tokens = 0
        self.total_duration = 0.0
        self._call_llm = None  # lazy import to avoid circular dependency

    def _get_llm_caller(self):
        """Lazy import of _call_llm from evolve_loop to avoid circular imports."""
        if self._call_llm is None:
            try:
                from evolve_loop import _call_llm
                self._call_llm = _call_llm
            except ImportError:
                # Fallback: inline implementation for standalone use
                self._call_llm = self._fallback_call_llm
        return self._call_llm

    def _fallback_call_llm(self, prompt: str, model: str | None = None,
                           timeout: int = 120, backend: str | None = None) -> str:
        """Fallback LLM caller when evolve_loop is not importable.
        Auto-detects available CLI (claude > codex > opencode)."""
        for cli in ["claude", "codex", "opencode"]:
            cmd = [cli]
            if cli == "claude":
                cmd.extend(["-p", prompt, "--output-format", "text"])
            elif cli == "codex":
                cmd.extend(["-q", prompt])
            else:
                cmd.extend(["run", prompt])
            if model:
                cmd.extend(["--model", model])
            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=timeout, env=env)
                return result.stdout.strip()
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                continue
        return "[ERROR: No LLM CLI found — install claude, codex, or opencode]"

    def judge(self, question: str, context: str) -> bool:
        """Ask the LLM a single binary question about the context.

        Args:
            question: A yes/no question (e.g., "Does this text mention X?")
            context: The text to evaluate against.

        Returns:
            True if YES, False if NO or unavailable.
        """
        prompt = (
            f"You are a binary classifier. Answer ONLY with YES or NO.\n\n"
            f"Context:\n{context[:8000]}\n\n"
            f"Question: {question}\n\n"
            f"Answer (YES or NO):"
        )

        call_llm = self._get_llm_caller()
        t0 = time.time()
        try:
            output = call_llm(prompt, model=self.model, timeout=self.timeout)
            duration = time.time() - t0
            self.total_duration += duration
            self.total_tokens += max(len(prompt) // 4, 1)

            output = output.strip().upper()
            # Parse YES/NO from output — handle variations
            last_line = output.split("\n")[-1] if output else ""
            if "YES" in last_line:
                return True
            if "NO" in last_line:
                return False
            return "YES" in output and "NO" not in output

        except Exception:
            self.total_duration += time.time() - t0
            return False

    def judge_batch(self, questions: list[tuple[str, str]]) -> list[bool]:
        """Judge multiple questions sequentially.

        Args:
            questions: List of (question, context) tuples.

        Returns:
            List of boolean results.
        """
        return [self.judge(q, c) for q, c in questions]

    def reset_stats(self):
        """Reset accumulated token and duration counters."""
        self.total_tokens = 0
        self.total_duration = 0.0


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
    """Built-in evaluator using deterministic checks + binary LLM for semantic assertions.

    Always available. Implements all 8 assertion types:
      Program-only: contains, not_contains, regex, file_exists, json_schema, script_check
      LLM binary:   path_hit, fact_coverage

    LLM is only used for semantic assertions and only asked YES/NO questions.
    """

    name = "local"

    def __init__(self, model: str | None = None):
        self.model = model
        self._llm_judge: BinaryLLMJudge | None = None

    def _get_judge(self) -> BinaryLLMJudge:
        if self._llm_judge is None:
            self._llm_judge = BinaryLLMJudge(model=self.model)
        return self._llm_judge

    def quick_gate(self, skill_path: Path, gt_path: Path | None = None) -> dict:
        from run_l1_gate import run_l1_gate
        return run_l1_gate(skill_path, gt_path)

    def full_eval(self, skill_path: Path, gt_path: Path,
                  split: str = "dev") -> dict:
        t0 = time.time()
        skill_content = (skill_path / "SKILL.md").read_text()
        data = json.loads(gt_path.read_text())

        cases = data if isinstance(data, list) else data.get("evals", [])
        if split:
            cases = [c for c in cases if c.get("split", "dev") == split]

        total_p = total_t = 0
        failed = []
        traces = {}

        for c in cases:
            case_id = c.get("id", "?")
            # Build trace: record what was evaluated and how
            case_trace_lines = [f"Case {case_id}: {c.get('prompt', '')}\n"]

            for a in c.get("assertions", []):
                total_t += 1
                atype = a.get("type", "contains")
                val = a.get("value", "")
                desc = a.get("description", val)

                ok = self._evaluate_assertion(
                    atype, val, a, skill_content, skill_path)

                case_trace_lines.append(
                    f"  [{atype}] {'PASS' if ok else 'FAIL'}: {desc}")

                if ok:
                    total_p += 1
                else:
                    failed.append({
                        "case_id": case_id,
                        "assertion": desc,
                        "type": atype,
                    })

            traces[str(case_id)] = "\n".join(case_trace_lines)

        duration = time.time() - t0
        judge = self._llm_judge
        tokens = judge.total_tokens if judge else 0

        return {
            "pass_rate": total_p / total_t if total_t else 0,
            "total_passed": total_p,
            "total_assertions": total_t,
            "failed": failed,
            "tokens": tokens,
            "duration": round(duration, 2),
            "traces": traces,
        }

    def _evaluate_assertion(self, atype: str, val: str, assertion: dict,
                            content: str, skill_path: Path) -> bool:
        """Evaluate a single assertion. Deterministic types use program logic;
        semantic types use BinaryLLMJudge for YES/NO classification."""

        # --- Program-only assertions (deterministic) ---

        if atype == "contains":
            return val.lower() in content.lower()

        if atype == "not_contains":
            return val.lower() not in content.lower()

        if atype == "regex":
            return bool(re.search(val, content))

        if atype == "file_exists":
            return (skill_path / val).exists() if val else False

        if atype == "json_schema":
            return self._check_json_schema(val, content)

        if atype == "script_check":
            return self._check_script(val, content, skill_path)

        # --- LLM binary assertions (semantic, YES/NO only) ---

        if atype == "path_hit":
            judge = self._get_judge()
            return judge.judge(
                f"Does this text reference or mention the path '{val}'?",
                content)

        if atype == "fact_coverage":
            return self._check_fact_coverage(val, assertion, content)

        # Unknown assertion type — fail explicitly (don't silently pass)
        return False

    def _check_json_schema(self, schema_str: str, content: str) -> bool:
        """Validate content against a JSON schema (program-only)."""
        try:
            schema = json.loads(schema_str)
            # Extract JSON from content (try ```json blocks first, then raw)
            json_match = re.search(
                r'```json\s*\n(.*?)\n```', content, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(1))
            else:
                data = json.loads(content)
            # Basic validation: check required keys and types
            return _basic_schema_check(data, schema)
        except (json.JSONDecodeError, KeyError, TypeError):
            return False

    def _check_script(self, script_path: str, content: str,
                      skill_path: Path) -> bool:
        """Run an external script with content on stdin (program-only)."""
        try:
            result = subprocess.run(
                [sys.executable, script_path],
                input=content, capture_output=True, text=True,
                timeout=30, cwd=str(skill_path),
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def _check_fact_coverage(self, val: str, assertion: dict,
                             content: str) -> bool:
        """Check fact coverage using binary LLM judgment per fact point.

        Two modes:
          Preset: assertion has 'facts' array → LLM binary per fact
          Online: no 'facts' → keyword matching against val
        """
        facts = assertion.get("facts")

        if facts and isinstance(facts, list):
            # Preset mode: LLM binary judgment per fact
            judge = self._get_judge()
            covered = 0
            for fact in facts:
                if judge.judge(
                    f"Does this text cover or address the following fact: '{fact}'?",
                    content
                ):
                    covered += 1
            # Pass if ≥80% of facts are covered
            return (covered / len(facts)) >= 0.8 if facts else True
        else:
            # Online mode (no preset facts): keyword matching
            # Split val into keywords and check coverage
            keywords = [k.strip() for k in val.split(",") if k.strip()]
            if not keywords:
                return True
            hits = sum(1 for k in keywords if k.lower() in content.lower())
            return (hits / len(keywords)) >= 0.8


def _basic_schema_check(data: Any, schema: dict) -> bool:
    """Lightweight JSON schema validation without jsonschema dependency."""
    stype = schema.get("type")
    if stype == "object":
        if not isinstance(data, dict):
            return False
        for req in schema.get("required", []):
            if req not in data:
                return False
        props = schema.get("properties", {})
        for key, prop_schema in props.items():
            if key in data:
                if not _basic_schema_check(data[key], prop_schema):
                    return False
        return True
    if stype == "array":
        if not isinstance(data, list):
            return False
        items_schema = schema.get("items")
        if items_schema:
            return all(_basic_schema_check(item, items_schema)
                       for item in data)
        return True
    if stype == "string":
        return isinstance(data, str)
    if stype == "number":
        return isinstance(data, (int, float))
    if stype == "integer":
        return isinstance(data, int)
    if stype == "boolean":
        return isinstance(data, bool)
    return True  # no type constraint


# ─────────────────────────────────────────────
# Built-in: Creator Evaluator (skill-creator)
# ─────────────────────────────────────────────

class CreatorEvaluator(Evaluator):
    """Evaluator using binary LLM judgment + program scoring.

    For each test case and each assertion:
      - Deterministic assertions (contains, regex, etc.) → program-only
      - Semantic assertions (path_hit, fact_coverage) → binary LLM call
    Program aggregates all binary results into final scores.

    Falls back to LocalEvaluator if LLM CLI unavailable.
    """

    name = "creator"

    def __init__(self, model: str | None = None):
        self.model = model
        self.creator_path = find_creator_path()
        self._fallback = LocalEvaluator(model=model)

    def quick_gate(self, skill_path: Path, gt_path: Path | None = None) -> dict:
        return self._fallback.quick_gate(skill_path, gt_path)

    def full_eval(self, skill_path: Path, gt_path: Path,
                  split: str = "dev") -> dict:
        # CreatorEvaluator uses the same binary approach as LocalEvaluator
        # but can additionally invoke Creator's scripts for trigger testing
        result = self._fallback.full_eval(skill_path, gt_path, split)

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


# ─────────────────────────────────────────────
# Factory: get_evaluator()
# ─────────────────────────────────────────────

EVALUATOR_REGISTRY: dict[str, type[Evaluator]] = {
    "local": LocalEvaluator,
    "creator": CreatorEvaluator,
    "script": ScriptEvaluator,
    "pytest": PytestEvaluator,
}


def get_evaluator(config: dict[str, Any] | None = None) -> Evaluator:
    """Create an evaluator from config.

    Config keys:
        evaluator: str          — "local" | "creator" | "script" | "pytest"
        evaluator_script: str   — path to script (for ScriptEvaluator)
        evaluator_test_cmd: str — test command (for PytestEvaluator)
        model: str              — LLM model (for binary judge)
        evaluator_timeout: int  — timeout in seconds
    """
    config = config or {}
    name = config.get("evaluator", "creator")

    if name == "creator":
        return CreatorEvaluator(model=config.get("model"))
    elif name == "script":
        script = config.get("evaluator_script")
        if not script:
            raise ValueError(
                "ScriptEvaluator requires 'evaluator_script' in config")
        return ScriptEvaluator(
            script_path=script,
            timeout=config.get("evaluator_timeout", 300),
        )
    elif name == "pytest":
        return PytestEvaluator(
            test_cmd=config.get("evaluator_test_cmd",
                                "pytest tests/ -v --tb=short"),
            timeout=config.get("evaluator_timeout", 300),
        )
    elif name == "local":
        return LocalEvaluator(model=config.get("model"))
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
        model: claude-sonnet-4-6
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
        elif line.startswith("- evaluator_script:") or \
                line.startswith("evaluator_script:"):
            val = line.split(":", 1)[1].strip()
            config["evaluator_script"] = val
        elif line.startswith("- evaluator_test_cmd:") or \
                line.startswith("evaluator_test_cmd:"):
            val = line.split(":", 1)[1].strip()
            config["evaluator_test_cmd"] = val
        elif line.startswith("- evaluator_timeout:") or \
                line.startswith("evaluator_timeout:"):
            val = line.split(":", 1)[1].strip()
            try:
                config["evaluator_timeout"] = int(val)
            except ValueError:
                pass
        elif line.startswith("- model:") or line.startswith("model:"):
            val = line.split(":", 1)[1].strip()
            if val:
                config["model"] = val

    return config
