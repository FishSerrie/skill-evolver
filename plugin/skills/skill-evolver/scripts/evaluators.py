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
from common import build_skill_corpus, validate_frontmatter

# Re-exported for back-compat so ``from evaluators import BinaryLLMJudge``
# and ``from evaluators import basic_schema_check`` keep working after
# the 2026-04-09 slim split. External callers don't need to know where
# these symbols physically live.
from case_store import write_cases_to_dir
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

    def full_eval(self, skill_path: Path, gt_path: Path,
                  split: str = "dev", **kwargs) -> dict:
        """Evaluate, then attach the figures the gate needs.

        A template method, and deliberately not left to each backend. The
        gate enforces a plan's size thresholds by reading ``snapshot`` from
        this dict, and treats its absence as "no structural signal exists",
        which passes. So a backend that forgot the key did not fail
        loudly — it silently switched off `max_structure` and
        `max_structure_growth` for everyone using it. Five of six backends
        had forgotten it, including the default one, so a plan capping the
        artifact's size was accepted, checked nothing, and reported `keep`.

        Adding it here rather than in each implementation makes that
        omission impossible rather than merely discouraged: there is one
        place left where it could go missing, instead of one per backend
        and one per early-return inside each.
        """
        result = self._run_full_eval(skill_path, gt_path, split, **kwargs)
        # setdefault, not assignment: a backend that measured the artifact
        # itself — the grader-based one takes its snapshot from the same
        # target it just ran — knows more precisely what was evaluated than
        # a re-read from disk afterwards would.
        result.setdefault("snapshot", self.structural_snapshot(skill_path))
        return result

    @abstractmethod
    def _run_full_eval(self, skill_path: Path, gt_path: Path,
                       split: str = "dev", **kwargs) -> dict:
        """Full evaluation against GT. Returns the standard result dict.

        Implemented by each backend; called by :meth:`full_eval`, which is
        what callers use.
        """
        ...

    def structural_snapshot(self, skill_path: Path) -> dict:
        """The size figures the gate compares against its thresholds.

        Defined here, once, so that every backend reports the same thing.
        Each one used to be free not to, and all but one took that option:
        a plan setting `max_structure` was accepted, the gate received
        nothing, and the run reported `keep` having checked no size at all.
        The user saw a threshold in their plan and a pass in their log, and
        the two were unrelated.

        Delegated to the target so that every artifact shape reports the
        same keys, which is what lets `check_structure` avoid asking what
        kind of artifact it is looking at.

        Do not confuse this with ``trace_enrichment.build_skill_snapshot``.
        That one records ``size_bytes`` / ``references_loaded`` for
        diagnosis and shares none of the gate's keys; substituting it would
        satisfy the key's presence while leaving every threshold
        unevaluated.

        Returns ``{}`` rather than raising when the artifact cannot be
        resolved: this is a measurement taken alongside the real work, and
        losing a whole evaluation because a size could not be counted
        trades a complete result for none.
        """
        try:
            from target import resolve_target

            return resolve_target(skill_path).snapshot()
        except (FileNotFoundError, ValueError, OSError):
            return {}

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
        """Load the full skill corpus: SKILL.md + prose subdirectories.

        Thin wrapper over :func:`common.build_skill_corpus`, which owns
        the concatenation and the list of directories a skill is made of.
        Kept as a method because subclasses and tests patch it to
        substitute a different corpus (see ``BehavioralEvaluator``), and
        that seam is worth preserving even though the logic moved.
        """
        return build_skill_corpus(skill_path)

    def _run_full_eval(self, skill_path: Path, gt_path: Path,
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
            # Reported at the top level because that is where the gate
            # reads it. Note this is NOT `skill_snapshot` above: that one
            # is a trace record for diagnosis (`size_bytes`,
            # `references_loaded`, ...) and carries none of the keys the
            # gate looks for. The two are easy to confuse — they are both
            # called a snapshot — and confusing them is what made the
            # structural thresholds silently do nothing: every backend
            # except one omitted this key, so `check_structure` received
            # None and returned "pass" for lack of data. A user who set
            # `max_structure` saw `keep` and had no protection at all.
            "snapshot": self.structural_snapshot(skill_path),
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
EVALUATOR_NAMES: tuple[str, ...] = (
    "local", "creator", "script", "pytest", "behavioral", "grader",
)


def get_evaluator(config: dict[str, Any] | None = None) -> Evaluator:
    """Create an evaluator from config.

    Config keys:
        evaluator: str          — "local" | "creator" | "script" | "pytest"
                                  | "behavioral" | "grader"
        evaluator_script: str   — path to script (for ScriptEvaluator)
        evaluator_test_cmd: str — test command (for PytestEvaluator)
        model: str              — LLM model (for binary judge)
        evaluator_timeout: int  — timeout in seconds

    The non-default backends live in ``scripts/evaluator_backends.py`` and
    ``scripts/grader_evaluator.py``, lazy-imported here so evaluators.py has
    no load-time dependency on them.
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
    elif name == "behavioral":
        from evaluator_backends import BehavioralEvaluator
        return BehavioralEvaluator(
            model=config.get("model"),
            backend=config.get("behavioral_backend"),
            sample_size=config.get("behavioral_sample_size", 8),
            fidelity=config.get("behavioral_fidelity", "assume_loaded"),
            timeout=config.get("evaluator_timeout", 120),
            workspace=config.get("workspace"),
        )
    elif name == "grader":
        # Runs the artifact and grades its output, rather than matching
        # assertions against the artifact's text. Lazy-imported for the
        # same reason as the others: nothing should pay for it unless it
        # is asked for.
        from grader_evaluator import GraderEvaluator, PromptRunner, build_grader
        from datasets import ColumnMap

        columns = config.get("columns")
        if isinstance(columns, dict):
            columns = ColumnMap(**columns)
        return GraderEvaluator(
            grader=build_grader(config),
            runner=config.get("runner") or PromptRunner(
                model=config.get("model"),
                backend=config.get("runner_backend"),
                timeout=config.get("evaluator_timeout", 120),
                input_key=config.get("input_key", "input"),
            ),
            columns=columns,
            splits=config.get("splits"),
            stratify=config.get("stratify", True),
            section=config.get("section"),
        )
    else:
        raise ValueError(
            f"Unknown evaluator '{name}'. "
            f"Available: {', '.join(EVALUATOR_NAMES)}"
        )


# Configuration a plan file may set, and the type each value parses as.
# One table rather than a branch per key: adding a setting should not mean
# adding a parsing rule, and a key listed here is guaranteed to reach
# whoever reads the config — the failure this replaces was a plan setting a
# size cap that nothing ever read.
PLAN_KEYS: dict[str, type] = {
    # Evaluator selection and transport
    "evaluator": str,
    "evaluator_script": str,
    "evaluator_test_cmd": str,
    "evaluator_timeout": int,
    "model": str,
    "judge_model": str,
    "behavioral_sample_size": int,
    "behavioral_backend": str,
    "behavioral_fidelity": str,
    # Grader selection and the case fields it reads
    "grader": str,
    "grader_timeout": int,
    "points_key": str,
    "expectations_key": str,
    "rubric_key": str,
    "input_key": str,
    "commit_first": bool,
    "rubric_instruction": str,
    "partial_weight": float,
    "pass_threshold": float,
    "runner_backend": str,
    "judge_backend": str,
    "section": str,
    # Dataset shaping — JSON values, since they are structured
    "columns": dict,
    "splits": dict,
    "stratify": bool,
    # Gate thresholds
    "min_delta": float,
    "noise_threshold": float,
    "trigger_tolerance": float,
    "max_token_increase": float,
    "max_latency_increase": float,
    "regression_tolerance": float,
    "max_structure_growth": float,
    "max_structure": dict,
    "min_metrics": dict,
    "max_metric_regression": dict,
}

# A key must look like this to be considered configuration at all. Prose in
# a plan is written as "Notes:", "URL:", "Step 1:" — capitalised, spaced, or
# punctuated — so requiring lower-case snake_case separates a misspelled
# setting from a sentence without needing a list of either.
#
# An earlier version enumerated look-alike names by hand. That failed on the
# misspellings people actually make: of fourteen realistic typos
# (`mn_metrics`, `min_metrcis`, `commit_frist`, `points_ky`, `colums` …) only
# one happened to be on the list, so thirteen stayed silent — in a mechanism
# whose entire purpose is to catch "I set a threshold and it did nothing".
# Naming every possible mistake in advance is not something one can do;
# recognising the shape is.
_PLAN_KEY_SHAPE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


def parse_evaluator_from_plan(plan_path: Path) -> dict[str, Any]:
    """Extract evaluator and gate configuration from evolve_plan.md.

    Recognised keys are declared in :data:`PLAN_KEYS` with the type each
    parses as, so adding one is a table entry rather than another branch.
    A line may be written with or without a leading ``- ``.

    Anything that fails to become configuration is reported under
    ``_unknown`` — both an unrecognised key and a recognised key whose value
    could not be parsed. Those two mistakes have the same symptom, "I set a
    threshold and it did nothing", so they get the same treatment. Parsing
    never fails, because a plan is mostly prose and a stray colon must not
    stop a run; the caller decides how loudly to complain.
    """
    config: dict[str, Any] = {}
    if not plan_path.exists():
        return config

    unknown: list[str] = []
    for raw in plan_path.read_text().split("\n"):
        line = raw.strip()
        if line.startswith("- "):
            line = line[2:].strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not _PLAN_KEY_SHAPE.match(key):
            # Prose, not configuration.
            continue
        if key not in PLAN_KEYS:
            unknown.append(key)
            continue
        parsed = _parse_plan_value(value, PLAN_KEYS[key])
        if parsed is None:
            # A known key whose value is unusable. Reported rather than
            # quietly left at its default: the author wrote it down because
            # they wanted it to take effect.
            unknown.append(f"{key} (value {value!r} is not a valid "
                           f"{PLAN_KEYS[key].__name__})")
            continue
        config[key] = parsed

    if unknown:
        config["_unknown"] = unknown
    return config


def _parse_plan_value(value: str, kind: type) -> Any:
    """Parse one plan value, or return ``None`` when it is unusable.

    ``None`` means "leave the key unset" rather than "the value is None", so
    a blank or malformed entry falls back to the default instead of
    overriding it with something nonsensical.
    """
    if not value:
        return None
    if kind is str:
        return value
    if kind is bool:
        lowered = value.lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
        return None
    try:
        if kind is int:
            return int(value)
        if kind is float:
            return float(value)
        if kind in (dict, list):
            parsed = json.loads(value)
            return parsed if isinstance(parsed, kind) else None
    except (ValueError, json.JSONDecodeError):
        return None
    return None

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
        elif line.startswith("- behavioral_sample_size:") or \
                line.startswith("behavioral_sample_size:"):
            val = line.split(":", 1)[1].strip()
            try:
                config["behavioral_sample_size"] = int(val)
            except ValueError:
                pass
        elif line.startswith("- behavioral_backend:") or \
                line.startswith("behavioral_backend:"):
            val = line.split(":", 1)[1].strip()
            if val:
                config["behavioral_backend"] = val
        elif line.startswith("- behavioral_fidelity:") or \
                line.startswith("behavioral_fidelity:"):
            val = line.split(":", 1)[1].strip()
            if val:
                config["behavioral_fidelity"] = val
        elif line.startswith("- model:") or line.startswith("model:"):
            val = line.split(":", 1)[1].strip()
            if val:
                config["model"] = val

    return config
