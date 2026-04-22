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
import re
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from common import require_creator, find_creator_path, validate_frontmatter

# Re-exported for back-compat so ``from evaluators import BinaryLLMJudge``
# and ``from evaluators import basic_schema_check`` keep working after
# the 2026-04-09 slim split. External callers don't need to know where
# these symbols physically live.
from binary_judge import BinaryLLMJudge
from trace_enrichment import (
    build_skill_snapshot,
    check_fact_coverage_rich,
    check_json_schema_rich,
    check_script_rich,
    excerpt,
    locate_in_corpus,
    nearest_match,
    basic_schema_check,
    basic_schema_check_with_path,
)

# Back-compat alias: the old underscored module-level helper name is
# still accepted so older external callers (if any) keep working.
# New code should use ``basic_schema_check`` / ``basic_schema_check_with_path``
# imported from ``trace_enrichment`` (or the re-exports above).
_basic_schema_check = basic_schema_check
_basic_schema_check_with_path = basic_schema_check_with_path


# ─────────────────────────────────────────────
# BinaryLLMJudge lives in ``binary_judge.py`` (extracted 2026-04-09).
# Re-exported at the top of this file so ``from evaluators import
# BinaryLLMJudge`` keeps working for every existing caller.
# ─────────────────────────────────────────────


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

        Note: the ``### <rel-path> ###`` header format matters — it's
        what :func:`trace_enrichment.locate_in_corpus` uses to map
        char offsets back to ``{file, line}`` pointers for the trace
        enrichment.
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
        # Rich skill snapshot (paper §3 "state updates" trace component).
        # Computed once per full_eval since it doesn't change across
        # cases in the same run. Delegates to trace_enrichment module.
        skill_snapshot = build_skill_snapshot(skill_path)
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

                result = self._evaluate_assertion(
                    atype, val, a, skill_content, skill_path)
                ok = bool(result.get("pass", False))

                # Merge type-specific rich fields (match.location,
                # nearest_match, stdout/stderr, judge_reasoning, etc.)
                # into the assertion record so the proposer can diagnose
                # without re-running the evaluator. This is the paper
                # §3 alignment — each assertion carries its own trace
                # components.
                assertion_record = {
                    "index": idx,
                    "type": atype,
                    "value": val,
                    "description": desc,
                    "pass": ok,
                }
                for k, v in result.items():
                    if k == "pass":
                        continue
                    assertion_record[k] = v
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
                "skill_loaded": skill_snapshot,
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

    # ─────────────────────────────────────────
    # Trace-enrichment helpers live in ``trace_enrichment.py``
    # (paper §3 four components: prompts, tool calls, model outputs,
    # state updates). The dispatcher below calls those module
    # functions directly — the old instance methods are gone.
    # ─────────────────────────────────────────

    def _evaluate_assertion(self, atype: str, val: str, assertion: dict,
                            content: str, skill_path: Path) -> dict:
        """Evaluate a single assertion and return a structured result dict.

        The returned dict always has a ``pass`` boolean. Type-specific
        extras populate the Meta-Harness paper §3 trace components
        (prompts / tool calls / model outputs / state updates) so the
        proposer can diagnose WHY each assertion failed, not just THAT
        it did.

        Extras by type:
          - contains / regex  pass  → ``match: {file, line, excerpt}``
          - contains          fail  → ``nearest_match: {...} | None``
          - not_contains      fail  → ``found_at: {file, line, excerpt}``
          - script_check      both  → ``exit_code, stdout, stderr, duration_ms, resolved_path``
          - path_hit          both  → ``judge_reasoning: str``
          - fact_coverage     preset→ ``judge_verdicts: [{fact, verdict, reasoning}, ...], passed_facts, total_facts``
          - fact_coverage     online→ ``keyword_hits, keyword_total``
        """

        # --- Program-only assertions (deterministic) ---
        # All rich helpers (locate_in_corpus / excerpt / nearest_match /
        # check_script_rich / check_json_schema_rich) come from the
        # trace_enrichment module — they're pure functions, no self
        # state needed.

        if atype == "contains":
            idx = content.lower().find(val.lower())
            if idx >= 0:
                return {
                    "pass": True,
                    "match": {
                        **locate_in_corpus(content, idx),
                        "excerpt": excerpt(content, idx, idx + len(val)),
                    },
                }
            return {"pass": False, "nearest_match": nearest_match(content, val)}

        if atype == "not_contains":
            idx = content.lower().find(val.lower())
            if idx < 0:
                return {"pass": True}
            return {
                "pass": False,
                "found_at": {
                    **locate_in_corpus(content, idx),
                    "excerpt": excerpt(content, idx, idx + len(val)),
                },
            }

        if atype == "regex":
            try:
                m = re.search(val, content)
            except re.error as e:
                return {"pass": False, "regex_error": str(e)}
            if m:
                return {
                    "pass": True,
                    "match": {
                        **locate_in_corpus(content, m.start()),
                        "text": m.group(0)[:200],
                        "excerpt": excerpt(content, m.start(), m.end()),
                    },
                }
            return {"pass": False, "nearest_match": None}

        if atype == "file_exists":
            ok = bool(val) and (skill_path / val).exists()
            out = {"pass": ok}
            if not ok and val:
                out["expected_path"] = str(skill_path / val)
            return out

        if atype == "json_schema":
            return check_json_schema_rich(val, content)

        if atype == "script_check":
            return check_script_rich(val, content, skill_path)

        # --- LLM binary assertions (semantic, YES/NO only) ---

        if atype == "path_hit":
            judge = self._get_judge()
            verdict, reasoning = judge.judge_with_reasoning(
                f"Does this text reference or mention the path '{val}'?",
                content,
            )
            return {"pass": verdict, "judge_reasoning": reasoning}

        if atype == "fact_coverage":
            # Pass the judge instance explicitly — keeps the
            # trace_enrichment function pure / free of class coupling.
            return check_fact_coverage_rich(
                val, assertion, content, self._get_judge())

        # Unknown assertion type — fail explicitly (don't silently pass).
        return {"pass": False, "error": f"unknown assertion type: {atype}"}


# ─────────────────────────────────────────────
# The rich-helper methods that used to live in LocalEvaluator
# (_check_json_schema_rich, _check_json_schema, _check_script_rich,
# _check_fact_coverage_rich) were extracted to
# ``scripts/trace_enrichment.py`` on 2026-04-09 as pure module
# functions (check_json_schema_rich, check_script_rich,
# check_fact_coverage_rich). The schema validation helpers
# (_basic_schema_check, _basic_schema_check_with_path) are also there
# as ``basic_schema_check`` and ``basic_schema_check_with_path``.
# Re-exported at the top of this file for back-compat.
# ─────────────────────────────────────────────


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
