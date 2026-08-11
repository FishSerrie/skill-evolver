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
from case_store import write_cases_to_dir
from evaluators import Evaluator, LocalEvaluator
from json_extract import extract_json_object
from trace_enrichment import build_skill_snapshot


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

    def _run_full_eval(self, skill_path: Path, gt_path: Path,
                  split: str = "dev",
                  cases_dir: Path | None = None) -> dict:
        # CreatorEvaluator uses the same binary approach as LocalEvaluator
        # but can additionally invoke Creator's scripts for trigger testing.
        # Forward cases_dir so auto-persistence reaches the delegate.
        result = self._fallback.full_eval(
            skill_path, gt_path, split, cases_dir=cases_dir)

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
                # Parse trigger results from stdout. No required_key here:
                # the trigger eval's field set is Creator's to define, so
                # pinning a key would couple us to its current schema.
                return extract_json_object(result.stdout)
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

    def _run_full_eval(self, skill_path: Path, gt_path: Path,
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

            parsed = extract_json_object(result.stdout, required_key="pass_rate")
            if parsed is not None:
                parsed.setdefault("tokens", 0)
                parsed.setdefault("duration", round(duration, 2))
                parsed.setdefault("total_passed", 0)
                parsed.setdefault("total_assertions", 0)
                parsed.setdefault("failed", [])
                parsed.setdefault("traces", {})
                return parsed

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

    def _run_full_eval(self, skill_path: Path, gt_path: Path,
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
# BehavioralEvaluator — real skill execution instead of static-doc matching
# ─────────────────────────────────────────────

class BehavioralEvaluator(LocalEvaluator):
    """Evaluator that scores real agent transcripts instead of skill docs.

    ``LocalEvaluator`` (evaluators.py) always evaluates assertions
    against ``_load_skill_corpus()`` — the concatenated static text of
    SKILL.md + references/*.md + agents/*.md. Every assertion type
    therefore measures "does the documentation contain the right
    words", never "does the skill actually behave correctly when run".
    This evaluator fixes that by running each sampled case through
    ``behavioral_runner.run_case_behaviorally`` and scoring the real
    transcript. See
    ``docs/private/multi-agent-evolution-upgrade/architecture.md``
    Module A for the full design and the CLI/conversation split.

    ``quick_gate`` is inherited from ``LocalEvaluator`` UNCHANGED — no
    override here. That's not a design choice being made silently;
    it's how Python inheritance works, and the architecture plan
    depends on it being true (quick_gate stays free of behavioral
    cost regardless of which full_eval path runs).

    Per-assertion routing: each GT assertion may carry an optional
    ``target: "output" | "skill_doc"`` field (default ``"output"``).
    ``target="output"`` assertions are scored against the real
    transcript; ``target="skill_doc"`` assertions are scored against
    the same static corpus ``LocalEvaluator`` would use (some
    assertions — e.g. "SKILL.md must reference references/foo.md" —
    are legitimately about documentation structure, not behavior, and
    switching them to transcript scoring would just make them fail
    for the wrong reason). ``scripts/migrate_to_behavioral.py``
    back-fills this field on existing GT files.

    Conversation-mode callers (a Claude driving the evolve loop
    in-conversation, which can issue real Agent tool calls) MUST use
    ``build_full_eval_specs()`` / ``finish_full_eval()`` instead of
    ``full_eval()`` directly — see those methods' docstrings for why
    a plain synchronous call can't work for that path.
    """

    name = "behavioral"

    def __init__(self, model: str | None = None, backend: str | None = None,
                 sample_size: int = 8, fidelity: str = "assume_loaded",
                 timeout: int = 120, workspace: str | Path | None = None):
        super().__init__(model=model)
        self.backend = backend
        self.sample_size = sample_size
        self.fidelity = fidelity
        self.timeout = timeout
        self.workspace = Path(workspace) if workspace else None

    def _resolve_workspace(self, gt_path: Path) -> Path:
        """Derive the workspace root for the rotation-state cache file
        when the caller didn't supply one explicitly. Convention (see
        ``setup_workspace.py``): GT lives at
        ``<workspace>/evals/evals.json``, so ``gt_path.parent.parent``
        recovers ``<workspace>`` without requiring orchestrator.py to
        be changed to pass it through explicitly (Stage A's file
        manifest does not touch orchestrator.py)."""
        return self.workspace or gt_path.parent.parent

    def _load_raw_cases(self, gt_path: Path, split: str) -> list[dict]:
        data = json.loads(gt_path.read_text())
        raw_cases = data if isinstance(data, list) else data.get("evals", [])
        if split:
            raw_cases = [c for c in raw_cases if c.get("split", "dev") == split]
        return raw_cases

    def _select_cases(self, gt_path: Path, split: str) -> list[dict]:
        from behavioral_runner import get_rotation_sample, rotation_state_path
        raw_cases = self._load_raw_cases(gt_path, split)
        state_path = rotation_state_path(self._resolve_workspace(gt_path))
        return get_rotation_sample(raw_cases, self.sample_size, state_path, split)

    def _run_full_eval(self, skill_path: Path, gt_path: Path,
                  split: str = "dev", cases_dir: Path | None = None) -> dict:
        """CLI-mode synchronous behavioral eval — each sampled case runs
        through an independent subprocess (behavioral_runner.run_case_
        behaviorally), blocking normally, no Agent tool call needed.
        Conversation-mode callers cannot use this method (see class
        docstring) — a Python method has no way to suspend itself and
        wait for the driving Claude to issue an Agent tool call and
        come back with a result.
        """
        t0 = time.time()
        from behavioral_runner import run_case_behaviorally

        sampled = self._select_cases(gt_path, split)
        skill_snapshot = build_skill_snapshot(skill_path)
        skill_doc_content = self._load_skill_corpus(skill_path)

        total_p = total_t = 0
        failed: list[dict] = []
        cases: list[dict] = []
        for c in sampled:
            transcript = run_case_behaviorally(
                skill_path, c, backend=self.backend, model=self.model,
                timeout=self.timeout, fidelity=self.fidelity)
            case_record, passed, total, case_failed = self._score_case(
                c, transcript["output_text"], skill_doc_content, skill_path,
                skill_snapshot, transcript)
            total_p += passed
            total_t += total
            failed.extend(case_failed)
            cases.append(case_record)

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
        }

    def build_full_eval_specs(self, skill_path: Path, gt_path: Path,
                              split: str = "dev") -> dict:
        """Conversation-mode stage 1. A Python method cannot block and
        wait for the driving Claude to issue an Agent tool call, so
        full_eval() is split into two stages for this path (see
        architecture plan Module A, "conversation 模式的控制流断点"):

          1. build_full_eval_specs() — this method. Picks the rotation
             sample and returns one Agent-tool-call spec per case.
          2. The driving Claude issues each Agent tool call itself and
             collects the returned text per case.
          3. finish_full_eval() — feeds the (spec, transcript_text)
             pairs back in and does the actual assertion scoring,
             returning the same dict shape full_eval() would have.

        Returns {"specs": [task_spec, ...], "skill_doc_content": str,
        "skill_snapshot": dict} — the caller must round-trip
        skill_doc_content/skill_snapshot back into finish_full_eval
        unchanged (computed once here to guarantee both stages agree
        on the same static-corpus snapshot).
        """
        from behavioral_runner import build_behavioral_task_spec

        sampled = self._select_cases(gt_path, split)
        specs = [build_behavioral_task_spec(skill_path, c) for c in sampled]
        return {
            "specs": specs,
            # Falls back to the position, not to a shared `"?"`. Two cases
            # without an id both answered to `"?"`, so the second replaced
            # the first in this dict — the run then measured fewer cases
            # than it sampled, with every count downstream still internally
            # consistent. Cases loaded through `datasets` always carry an
            # id; these are read straight from the GT file, where nothing
            # requires one.
            "cases_by_id": {
                (str(c.get("id", "")).strip() or f"case-{i}"): c
                for i, c in enumerate(sampled)
            },
            "skill_doc_content": self._load_skill_corpus(skill_path),
            "skill_snapshot": build_skill_snapshot(skill_path),
        }

    def finish_full_eval(self, skill_path: Path, staged: dict,
                         transcripts: dict[str, str],
                         cases_dir: Path | None = None) -> dict:
        """Conversation-mode stage 2 — see build_full_eval_specs().

        Args:
            staged: the exact dict build_full_eval_specs() returned.
            transcripts: {case_id: output_text} — the driving Claude's
                Agent tool call results, keyed by the same case_id
                values as staged["cases_by_id"]. A case_id missing from
                this dict is scored as an "error" transcript (empty
                output, all its assertions fail) rather than raising —
                a single stuck/failed Agent call shouldn't crash the
                whole eval round.
        """
        from behavioral_runner import build_transcript_from_text

        t0 = time.time()
        total_p = total_t = 0
        failed: list[dict] = []
        cases: list[dict] = []
        for case_id, c in staged["cases_by_id"].items():
            output_text = transcripts.get(case_id)
            transcript = build_transcript_from_text(
                output_text or "",
                runner_backend="agent_tool",
                isolation="subagent_context",
                fidelity="assume_loaded",
                exit_status="ok" if output_text is not None else "error",
            )
            case_record, passed, total, case_failed = self._score_case(
                c, transcript["output_text"], staged["skill_doc_content"],
                skill_path, staged["skill_snapshot"], transcript)
            total_p += passed
            total_t += total
            failed.extend(case_failed)
            cases.append(case_record)

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
        }

    def _score_case(self, c: dict, output_content: str,
                    skill_doc_content: str, skill_path: Path,
                    skill_snapshot: dict, transcript: dict) -> tuple:
        """Evaluate one case's assertions against real content.

        Mirrors LocalEvaluator.full_eval's inner loop (same assertion
        dispatch via self._evaluate_assertion, same case-record shape)
        — LocalEvaluator itself is NOT modified (architecture plan:
        "LocalEvaluator 本体不动"), so this duplicates that loop rather
        than extending it; the one behavioral difference is that
        content is picked per-assertion via ``target`` instead of
        being the same static corpus for every assertion in every
        case. Returns (case_record, passed_count, total_count,
        failed_list) — the caller accumulates these across cases.
        """
        case_id = c.get("id", "?")
        case_prompt = c.get("prompt", "")
        case_assertions = []
        case_passed = 0
        case_failed_indexes: list[int] = []
        case_failed: list[dict] = []

        for idx, a in enumerate(c.get("assertions", [])):
            atype = a.get("type", "contains")
            val = a.get("value", "")
            desc = a.get("description", val)
            target = a.get("target", "output")
            content = output_content if target == "output" else skill_doc_content

            result = self._evaluate_assertion(atype, val, a, content, skill_path)
            ok = bool(result.get("pass", False))

            assertion_record = {
                "index": idx,
                "type": atype,
                "value": val,
                "description": desc,
                "target": target,
                "pass": ok,
            }
            for k, v in result.items():
                if k != "pass":
                    assertion_record[k] = v
            case_assertions.append(assertion_record)

            if ok:
                case_passed += 1
            else:
                case_failed_indexes.append(idx)
                case_failed.append({
                    "case_id": case_id, "assertion": desc, "type": atype,
                })

        case_total = len(case_assertions)
        case_record = {
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
            "transcript": transcript,
        }
        return case_record, case_passed, case_total, case_failed

    def info(self) -> dict:
        return {
            "name": self.name,
            "type": "BehavioralEvaluator",
            "backend": self.backend or "auto",
            "sample_size": self.sample_size,
            "fidelity": self.fidelity,
            "model": self.model,
            "philosophy": "score real agent transcripts, not skill docs",
        }
