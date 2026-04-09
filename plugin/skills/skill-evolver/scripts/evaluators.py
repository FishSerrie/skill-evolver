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
        "cases": [                # per-case structured trace (Meta-Harness
            {                     # aligned: paper §2 "source code + scores +
                "case_id": 3,     # execution traces" filesystem model)
                "prompt": "...",
                "skill_loaded": {"path": "...", "size_bytes": 24331},
                "assertions": [
                    {
                        "index": 0,
                        "type": "contains",
                        "value": "...",
                        "description": "...",
                        "pass": True,
                        # type-specific fields populated progressively
                        # (match.location, nearest_match, stdout/stderr,
                        #  judge_verdicts[].reasoning — see
                        #  docs/private/migration-trace-architecture.md)
                    },
                    ...
                ],
                "summary": {"total_assertions": 3, "passed": 1, "failed": 2,
                            "failed_indexes": [1, 2]},
            },
            ...
        ],
    }

Reference: Lee et al. 2026, "Meta-Harness: End-to-End Optimization of Model
Harnesses", arXiv 2603.28052. The paper's proposer reads a median of 82
files/iteration via grep/cat; our per-case JSON layout under
iteration-E{N}/cases/ matches that access pattern.
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
from common import require_creator, find_creator_path, validate_frontmatter


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
        """Lazy import of _call_llm from llm module to avoid circular imports.

        Falls back to a self-contained CLI-detecting implementation if
        scripts/llm.py isn't importable (standalone copies of this file).
        """
        if self._call_llm is None:
            try:
                from llm import _call_llm
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

        except Exception as e:
            # Log the failure instead of silently returning False. A bare
            # except here used to make LLM-backend crashes (HTTP 500, bad
            # JSON, timeout, credential error) indistinguishable from a
            # legitimate "NO" answer, which poisoned Phase 2 diagnosis.
            self.total_duration += time.time() - t0
            print(
                f"[warn] BinaryLLMJudge.judge failed: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
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

    def _load_skill_corpus(self, skill_path: Path) -> str:
        """Load the full skill corpus: SKILL.md + references/*.md + agents/*.md.

        Claude reads all of a skill's files when running it; an evaluator
        that scores only SKILL.md misses content that legitimately lives
        in references/ and agents/. This mirrors dev/run_loop.py's
        build_corpus() so local eval reflects real Claude behavior.
        """
        parts = []
        skill_md = skill_path / "SKILL.md"
        if skill_md.exists():
            parts.append(f"### SKILL.md ###\n{skill_md.read_text()}")
        for subdir in ("references", "agents"):
            dir_path = skill_path / subdir
            if not dir_path.is_dir():
                continue
            for md in sorted(dir_path.rglob("*.md")):
                rel = md.relative_to(skill_path)
                parts.append(f"### {rel} ###\n{md.read_text()}")
        return "\n\n".join(parts)

    def full_eval(self, skill_path: Path, gt_path: Path,
                  split: str = "dev",
                  cases_dir: Path | None = None) -> dict:
        """Run full eval against GT assertions.

        Args:
            skill_path: the skill to evaluate.
            gt_path: the GT evals.json file.
            split: which GT split to run (``dev`` / ``holdout`` / ``regression``).
            cases_dir: optional directory to auto-persist per-case JSON
                files (``case_{id}.json`` under this dir). When set, the
                returned ``cases`` list is ALSO written to disk so
                in-conversation callers don't have to remember to call
                ``persist_cases`` separately — essential for the next
                iteration's Phase 1 / Phase 2 Meta-Harness diagnosis,
                which reads these files via grep/cat. The conventional
                path is ``<workspace>/evolve/iteration-E{N}/cases``.

        Reference: paper §2 filesystem layout. Each case gets its own
        structured JSON file so the proposer can grep across iterations
        (``grep -l '"pass": false' iteration-E*/cases/*.json``).
        """
        t0 = time.time()
        skill_content = self._load_skill_corpus(skill_path)
        skill_md_path = skill_path / "SKILL.md"
        skill_md_size = skill_md_path.stat().st_size if skill_md_path.exists() else 0
        data = json.loads(gt_path.read_text())

        raw_cases = data if isinstance(data, list) else data.get("evals", [])
        if split:
            raw_cases = [c for c in raw_cases if c.get("split", "dev") == split]

        total_p = total_t = 0
        failed = []
        cases = []

        for c in raw_cases:
            case_id = c.get("id", "?")
            case_prompt = c.get("prompt", "")
            case_assertions = []
            case_passed = 0
            case_failed_indexes = []

            for idx, a in enumerate(c.get("assertions", [])):
                total_t += 1
                atype = a.get("type", "contains")
                val = a.get("value", "")
                desc = a.get("description", val)

                ok = self._evaluate_assertion(
                    atype, val, a, skill_content, skill_path)

                # Structured assertion record. Type-specific fields (match
                # location, stdout/stderr, judge reasoning) will be filled
                # in progressively by later meta-evolution iterations —
                # the schema has room for them without breaking grep.
                assertion_record = {
                    "index": idx,
                    "type": atype,
                    "value": val,
                    "description": desc,
                    "pass": ok,
                }
                case_assertions.append(assertion_record)

                if ok:
                    total_p += 1
                    case_passed += 1
                else:
                    case_failed_indexes.append(idx)
                    failed.append({
                        "case_id": case_id,
                        "assertion": desc,
                        "type": atype,
                    })

            case_total = len(case_assertions)
            cases.append({
                "case_id": case_id,
                "split": c.get("split", "dev"),
                "prompt": case_prompt,
                "skill_loaded": {
                    "path": str(skill_path),
                    "size_bytes": skill_md_size,
                },
                "assertions": case_assertions,
                "summary": {
                    "total_assertions": case_total,
                    "passed": case_passed,
                    "failed": case_total - case_passed,
                    "failed_indexes": case_failed_indexes,
                },
            })

        # Auto-persist cases when an explicit directory is requested.
        # Lazy-import to avoid a top-level cycle with evolve_loop (which
        # already imports from this module).
        if cases_dir is not None and cases:
            from evolve_loop import write_cases_to_dir
            write_cases_to_dir(Path(cases_dir), cases)

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
            "cases": cases,
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
        """Run an external script and use its exit code as pass/fail.

        Script path resolution order:
          1. Absolute path → used as-is.
          2. Workspace-relative → ``<skill-parent>/<skill-name>-workspace/<script_path>``
             for standalone skills, or ``<repo-root-parent>/<skill-name>-workspace/<script_path>``
             for plugin-hosted skills (mirrors ``find_workspace``).
             **Preferred location**: eval-harness check scripts belong in the
             workspace (a gitignored, per-user artifact), not inside the
             shipped skill body.
          3. Skill-relative → ``skill_path/<script_path>`` (legacy fallback
             for older GT files that still point inside the skill).

        The script runs with ``cwd=skill_path``, so ``Path.cwd()`` inside the
        script resolves to the skill root regardless of where the script
        file physically lives.
        """
        from common import find_workspace  # local import to avoid cycles

        p = Path(script_path)
        if p.is_absolute():
            resolved: Path | None = p if p.exists() else None
        else:
            # Resolve skill_path first — Path('.').name == '' would otherwise
            # produce a bogus workspace path.
            skill_root = skill_path.resolve()
            workspace = find_workspace(skill_root)
            workspace_candidate = workspace / script_path
            skill_candidate = skill_root / script_path
            if workspace_candidate.exists():
                resolved = workspace_candidate
            elif skill_candidate.exists():
                resolved = skill_candidate
            else:
                resolved = None

        if resolved is None:
            return False

        try:
            result = subprocess.run(
                [sys.executable, str(resolved)],
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
# Pluggable backends (CreatorEvaluator / ScriptEvaluator / PytestEvaluator)
# moved to scripts/evaluator_backends.py in iter 19. They are lazy-imported
# by get_evaluator() below to avoid a circular import (backends inherit
# from Evaluator + LocalEvaluator in this module).
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# Factory: get_evaluator()
# ─────────────────────────────────────────────

# Evaluator registry — lazy strings resolved inside get_evaluator() so
# importing evaluators.py doesn't pull in evaluator_backends.py unless
# one of the non-default backends is actually requested.
EVALUATOR_NAMES: tuple[str, ...] = ("local", "creator", "script", "pytest")


def get_evaluator(config: dict[str, Any] | None = None) -> Evaluator:
    """Create an evaluator from config.

    Config keys:
        evaluator: str          — "local" | "creator" | "script" | "pytest"
        evaluator_script: str   — path to script (for ScriptEvaluator)
        evaluator_test_cmd: str — test command (for PytestEvaluator)
        model: str              — LLM model (for binary judge)
        evaluator_timeout: int  — timeout in seconds

    The three non-default backends live in ``scripts/evaluator_backends.py``
    and are lazy-imported here so evaluators.py has no load-time dependency
    on them.
    """
    config = config or {}
    name = config.get("evaluator", "creator")

    if name == "local":
        return LocalEvaluator(model=config.get("model"))

    # All other backends live in evaluator_backends.py (lazy import
    # breaks the circular dependency — backends inherit from Evaluator
    # + LocalEvaluator in this module).
    if name == "creator":
        from evaluator_backends import CreatorEvaluator
        return CreatorEvaluator(model=config.get("model"))
    elif name == "script":
        script = config.get("evaluator_script")
        if not script:
            raise ValueError(
                "ScriptEvaluator requires 'evaluator_script' in config")
        from evaluator_backends import ScriptEvaluator
        return ScriptEvaluator(
            script_path=script,
            timeout=config.get("evaluator_timeout", 300),
        )
    elif name == "pytest":
        from evaluator_backends import PytestEvaluator
        return PytestEvaluator(
            test_cmd=config.get("evaluator_test_cmd",
                                "pytest tests/ -v --tb=short"),
            timeout=config.get("evaluator_timeout", 300),
        )
    else:
        raise ValueError(
            f"Unknown evaluator '{name}'. "
            f"Available: {', '.join(EVALUATOR_NAMES)}"
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
