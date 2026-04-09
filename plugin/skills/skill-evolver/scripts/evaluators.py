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

        Backward-compatible thin wrapper around :meth:`judge_with_reasoning`
        that returns only the boolean verdict. Prefer
        ``judge_with_reasoning`` for new code — the reasoning string is
        what makes the eval trace diagnose-able (Meta-Harness paper §3
        "model outputs" trace component).
        """
        verdict, _ = self.judge_with_reasoning(question, context)
        return verdict

    def judge_with_reasoning(self, question: str,
                             context: str) -> tuple[bool, str]:
        """Ask the LLM a binary question and capture both verdict + reasoning.

        The prompt asks the LLM to produce a 1-2 sentence rationale on
        the first line(s) followed by YES or NO on the last line. The
        rationale is the "model outputs" trace component from the
        Meta-Harness paper §3 — capturing it is what lets the proposer
        diagnose WHY a semantic assertion failed, not just THAT it did.

        Args:
            question: A yes/no question (e.g., "Does this text mention X?")
            context: The text to evaluate against.

        Returns:
            ``(verdict, reasoning)`` where verdict is True/False and
            reasoning is the LLM's rationale (may be empty if the LLM
            output was malformed or the call crashed).
        """
        prompt = (
            f"You are a binary classifier. First state your reasoning in "
            f"1-2 short sentences. Then on the VERY LAST line, output "
            f"exactly YES or NO — nothing else on that line.\n\n"
            f"Context:\n{context[:8000]}\n\n"
            f"Question: {question}\n\n"
            f"Reasoning:"
        )

        call_llm = self._get_llm_caller()
        t0 = time.time()
        try:
            output = call_llm(prompt, model=self.model, timeout=self.timeout)
            duration = time.time() - t0
            self.total_duration += duration
            self.total_tokens += max(len(prompt) // 4, 1)

            output = (output or "").strip()
            # Split off the last non-empty line as the verdict.
            lines = [ln for ln in output.split("\n") if ln.strip()]
            if not lines:
                return False, ""
            last_line = lines[-1].strip().upper()
            reasoning = "\n".join(lines[:-1]).strip()
            if not reasoning and len(lines) == 1:
                # LLM didn't follow the template — single line. Treat
                # that line as both reasoning and verdict.
                reasoning = lines[0].strip()

            if "YES" in last_line and "NO" not in last_line:
                return True, reasoning
            if "NO" in last_line and "YES" not in last_line:
                return False, reasoning
            # Ambiguous last line — fall back to overall content scan.
            up = output.upper()
            if "YES" in up and "NO" not in up:
                return True, reasoning
            return False, reasoning

        except Exception as e:
            # Log the failure instead of silently returning False. A bare
            # except here used to make LLM-backend crashes (HTTP 500, bad
            # JSON, timeout, credential error) indistinguishable from a
            # legitimate "NO" answer, which poisoned Phase 2 diagnosis.
            self.total_duration += time.time() - t0
            err = f"{type(e).__name__}: {e}"
            print(f"[warn] BinaryLLMJudge.judge failed: {err}",
                  file=sys.stderr)
            return False, f"[llm_error] {err}"

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

        Note: the ``### <rel-path> ###`` header format matters — it's
        what :meth:`_locate` uses to map char offsets back to
        ``{file, line}`` pointers for the trace enrichment.
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

    def _build_skill_snapshot(self, skill_path: Path) -> dict:
        """Build the paper §3 "state updates" trace component for a
        skill evaluation.

        Captures:
          - path         str  — skill directory
          - size_bytes   int  — SKILL.md file size
          - skill_md_lines int — line count of SKILL.md body
          - description_chars int — length of the ``description`` field
            from the SKILL.md YAML frontmatter (0 if no frontmatter)
          - references_loaded [str] — *.md files under references/
            that the evaluator corpus-loaded (relative paths)
          - agents_loaded    [str] — *.md files under agents/ ditto

        This snapshot lets a proposer reading a historical case JSON
        see exactly what Claude's corpus looked like at evaluation
        time, without having to ``git checkout`` the commit to
        reconstruct it. Matches paper §2's "state updates" in the
        skill evaluation regime (which is otherwise mostly stateless).
        """
        skill_md = skill_path / "SKILL.md"
        size_bytes = skill_md.stat().st_size if skill_md.exists() else 0
        md_lines = 0
        description_chars = 0
        if skill_md.exists():
            md_text = skill_md.read_text()
            md_lines = md_text.count("\n") + 1
            # Parse front-matter description (simple — avoids YAML dep).
            fm_match = re.match(
                r"^---\s*\n(.*?)\n---\s*\n", md_text, re.DOTALL)
            if fm_match:
                frontmatter = fm_match.group(1)
                # description can be a single line or a multi-line block.
                desc_match = re.search(
                    r"^description\s*:\s*(.*?)(?=^\w|\Z)",
                    frontmatter,
                    re.MULTILINE | re.DOTALL,
                )
                if desc_match:
                    description_chars = len(desc_match.group(1).strip())

        def _rel_md_list(subdir: str) -> list[str]:
            dir_path = skill_path / subdir
            if not dir_path.is_dir():
                return []
            return sorted(
                str(p.relative_to(skill_path))
                for p in dir_path.rglob("*.md")
            )

        return {
            "path": str(skill_path),
            "size_bytes": size_bytes,
            "skill_md_lines": md_lines,
            "description_chars": description_chars,
            "references_loaded": _rel_md_list("references"),
            "agents_loaded": _rel_md_list("agents"),
        }

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
        # cases in the same run.
        skill_snapshot = self._build_skill_snapshot(skill_path)
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
    # Trace-enrichment helpers (Meta-Harness §3 four components:
    # prompts, tool calls, model outputs, state updates)
    # ─────────────────────────────────────────

    def _locate(self, content: str, char_idx: int) -> dict:
        """Map a char offset in the concatenated corpus back to a
        ``{file, line}`` pointer, using the ``### <path> ###`` headers
        inserted by ``_load_skill_corpus``. Returns a dict with at
        least ``line`` (int, 1-indexed) and optionally ``file`` (str
        relative path inside the skill). Used by contains/regex match
        enrichment so the proposer can Read the exact line.
        """
        if char_idx < 0 or char_idx > len(content):
            return {"line": -1}
        prefix = content[:char_idx]
        # Find the most recent header above char_idx.
        header_re = re.compile(r"### (.+?) ###", re.MULTILINE)
        last_header = None
        last_header_end = 0
        for m in header_re.finditer(prefix):
            last_header = m.group(1)
            last_header_end = m.end()
        # Line number within the file (1-indexed from the end of the header).
        section = content[last_header_end:char_idx]
        line_in_section = section.count("\n") + 1
        if last_header is None:
            # No header found — just an overall line offset (shouldn't
            # happen for SKILL.md since _load_skill_corpus prefixes it).
            return {"line": prefix.count("\n") + 1}
        return {"file": last_header, "line": line_in_section}

    def _excerpt(self, content: str, start: int, end: int,
                 margin: int = 40) -> str:
        """Return a clean ±margin-char window around a char range,
        collapsing newlines and stripping leading/trailing whitespace."""
        a = max(0, start - margin)
        b = min(len(content), end + margin)
        snippet = content[a:b].replace("\n", " ").strip()
        return re.sub(r"\s+", " ", snippet)

    def _nearest_match(self, content: str, needle: str) -> dict | None:
        """Find the longest prefix/suffix of ``needle`` that appears
        verbatim in ``content``. Returns None if fewer than half the
        needle's characters match anywhere.

        This is a diagnostic shortcut for ``contains`` failures — the
        most common failure mode is "close but not exact" (whitespace,
        punctuation, minor wording change), and the longest shared
        prefix reliably pinpoints the intended location. More formal
        edit-distance matching would need a library; the prefix/suffix
        approach is deterministic, library-free, and good enough for
        skill GT workloads.
        """
        if not needle:
            return None
        lower_content = content.lower()
        lower_needle = needle.lower()

        min_len = max(len(needle) // 2, 3)
        # Try progressively shorter prefixes.
        for length in range(len(needle) - 1, min_len - 1, -1):
            probe = lower_needle[:length]
            idx = lower_content.find(probe)
            if idx >= 0:
                return {
                    "matched_text": content[idx:idx + length],
                    "missing_suffix": needle[length:],
                    "match_ratio": round(length / len(needle), 2),
                    **self._locate(content, idx),
                    "excerpt": self._excerpt(content, idx, idx + length),
                }
        # Try progressively shorter suffixes.
        for length in range(len(needle) - 1, min_len - 1, -1):
            probe = lower_needle[-length:]
            idx = lower_content.find(probe)
            if idx >= 0:
                return {
                    "matched_text": content[idx:idx + length],
                    "missing_prefix": needle[:-length],
                    "match_ratio": round(length / len(needle), 2),
                    **self._locate(content, idx),
                    "excerpt": self._excerpt(content, idx, idx + length),
                }
        return None

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

        if atype == "contains":
            needle_lower = val.lower()
            content_lower = content.lower()
            idx = content_lower.find(needle_lower)
            if idx >= 0:
                return {
                    "pass": True,
                    "match": {
                        **self._locate(content, idx),
                        "excerpt": self._excerpt(content, idx, idx + len(val)),
                    },
                }
            return {"pass": False, "nearest_match": self._nearest_match(content, val)}

        if atype == "not_contains":
            idx = content.lower().find(val.lower())
            if idx < 0:
                return {"pass": True}
            return {
                "pass": False,
                "found_at": {
                    **self._locate(content, idx),
                    "excerpt": self._excerpt(content, idx, idx + len(val)),
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
                        **self._locate(content, m.start()),
                        "text": m.group(0)[:200],
                        "excerpt": self._excerpt(content, m.start(), m.end()),
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
            return self._check_json_schema_rich(val, content)

        if atype == "script_check":
            return self._check_script_rich(val, content, skill_path)

        # --- LLM binary assertions (semantic, YES/NO only) ---

        if atype == "path_hit":
            judge = self._get_judge()
            verdict, reasoning = judge.judge_with_reasoning(
                f"Does this text reference or mention the path '{val}'?",
                content,
            )
            return {"pass": verdict, "judge_reasoning": reasoning}

        if atype == "fact_coverage":
            return self._check_fact_coverage_rich(val, assertion, content)

        # Unknown assertion type — fail explicitly (don't silently pass).
        return {"pass": False, "error": f"unknown assertion type: {atype}"}

    def _check_json_schema_rich(self, schema_str: str, content: str) -> dict:
        """Validate content against a JSON schema and return a rich
        result dict including the specific validation failure path.

        Three failure classes the proposer needs to distinguish:

          schema_error   → the GT's schema itself didn't parse
          no_json        → no JSON block found inside the content at all
          parse_error    → JSON block found but couldn't be parsed
          schema_mismatch→ parsed fine but failed one schema constraint
                           (``path`` tells you which field)

        Keeps the old ``_check_json_schema`` → bool API as a thin
        wrapper below for any external caller that only needs the
        verdict, but internally everything goes through the rich path.
        """
        try:
            schema = json.loads(schema_str)
        except json.JSONDecodeError as e:
            return {"pass": False, "schema_error": str(e)}

        # Extract JSON from content (try ```json blocks first, then raw).
        json_match = re.search(r'```json\s*\n(.*?)\n```', content, re.DOTALL)
        if json_match:
            data_str = json_match.group(1)
            extracted_from = "fenced_code_block"
        else:
            data_str = content
            extracted_from = "raw_content"

        try:
            data = json.loads(data_str)
        except json.JSONDecodeError as e:
            return {
                "pass": False,
                "parse_error": str(e),
                "extracted_from": extracted_from,
            }

        ok, failure_path = _basic_schema_check_with_path(data, schema, "")
        if ok:
            return {"pass": True, "extracted_from": extracted_from}
        return {
            "pass": False,
            "schema_mismatch_path": failure_path,
            "extracted_from": extracted_from,
        }

    def _check_json_schema(self, schema_str: str, content: str) -> bool:
        """Back-compat bool wrapper (nothing else currently calls this,
        but keep it around so a future evaluator backend can depend on
        the old contract without a conditional import)."""
        return bool(self._check_json_schema_rich(schema_str, content).get("pass"))

    def _check_script_rich(self, script_path: str, content: str,
                           skill_path: Path) -> dict:
        """Run an external script and return a rich result dict.

        The result dict is the Meta-Harness paper §3 "tool calls" trace
        component for script_check — captures stdout, stderr, exit
        code, and wall-clock duration so the proposer can diagnose
        script failures WITHOUT re-running them.

        Script path resolution order (unchanged from the old
        ``_check_script``):
          1. Absolute path → used as-is.
          2. Workspace-relative → ``<workspace>/<script_path>``
             (the canonical home per ``eval_strategy.md``).
          3. Skill-relative → ``skill_path/<script_path>`` (legacy
             fallback for older GT files pointing inside the skill).

        The script runs with ``cwd=skill_path`` so ``Path.cwd()``
        inside the script resolves to the skill root regardless of
        where the script file physically lives.

        Output caps: stdout/stderr are truncated at 2000 chars each so
        a runaway script can't balloon the case JSON file.
        """
        from common import find_workspace  # local import to avoid cycles

        p = Path(script_path)
        resolved: Path | None
        if p.is_absolute():
            resolved = p if p.exists() else None
        else:
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
            return {
                "pass": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"[script not found] {script_path}",
                "duration_ms": 0,
                "resolved_path": None,
            }

        t0 = time.time()
        try:
            result = subprocess.run(
                [sys.executable, str(resolved)],
                input=content, capture_output=True, text=True,
                timeout=30, cwd=str(skill_path),
            )
            duration_ms = int((time.time() - t0) * 1000)
            return {
                "pass": result.returncode == 0,
                "exit_code": result.returncode,
                "stdout": (result.stdout or "")[:2000],
                "stderr": (result.stderr or "")[:2000],
                "duration_ms": duration_ms,
                "resolved_path": str(resolved),
            }
        except subprocess.TimeoutExpired:
            return {
                "pass": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": "[timeout] script exceeded 30s",
                "duration_ms": int((time.time() - t0) * 1000),
                "resolved_path": str(resolved),
            }
        except OSError as e:
            return {
                "pass": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"[os error] {e}",
                "duration_ms": int((time.time() - t0) * 1000),
                "resolved_path": str(resolved),
            }

    def _check_fact_coverage_rich(self, val: str, assertion: dict,
                                  content: str) -> dict:
        """Check fact coverage and return a rich per-fact breakdown.

        Two modes (both return structured verdicts so the proposer can
        see which specific facts were missing, not just "below
        threshold"):

          Preset: assertion has a 'facts' array → each fact is judged
            by ``BinaryLLMJudge.judge_with_reasoning`` and every
            verdict + rationale is recorded. Passes if ≥80% of facts
            are covered.

          Online: no preset facts → each comma-separated keyword in
            ``val`` is checked via substring match. Passes if ≥80% of
            keywords hit.

        The per-fact dict lines up with the Meta-Harness paper §3
        "model outputs" trace component — individual LLM verdicts
        become part of the structured case record.
        """
        facts = assertion.get("facts")

        if facts and isinstance(facts, list):
            judge = self._get_judge()
            verdicts = []
            covered = 0
            for fact in facts:
                verdict, reasoning = judge.judge_with_reasoning(
                    f"Does this text cover or address the following fact: '{fact}'?",
                    content,
                )
                if verdict:
                    covered += 1
                verdicts.append({
                    "fact": fact,
                    "verdict": verdict,
                    "reasoning": reasoning,
                })
            total = len(facts)
            return {
                "pass": (covered / total) >= 0.8 if total else True,
                "judge_verdicts": verdicts,
                "passed_facts": covered,
                "total_facts": total,
                "mode": "preset",
            }

        # Online mode (no preset facts): keyword matching.
        keywords = [k.strip() for k in val.split(",") if k.strip()]
        if not keywords:
            return {"pass": True, "mode": "online", "keyword_total": 0}
        hits = [k for k in keywords if k.lower() in content.lower()]
        return {
            "pass": (len(hits) / len(keywords)) >= 0.8,
            "keyword_hits": hits,
            "keyword_total": len(keywords),
            "mode": "online",
        }


def _basic_schema_check(data: Any, schema: dict) -> bool:
    """Lightweight JSON schema validation without jsonschema dependency.

    Thin wrapper around :func:`_basic_schema_check_with_path` that
    discards the failure-path string. Kept for any external caller
    that only needs the boolean verdict.
    """
    ok, _ = _basic_schema_check_with_path(data, schema, "")
    return ok


def _basic_schema_check_with_path(
    data: Any, schema: dict, path: str
) -> tuple[bool, str]:
    """Lightweight JSON schema validation that returns both a verdict
    and the path to the first failing constraint.

    The path is a dotted string like ``$.items[2].name`` identifying
    which field inside ``data`` violated its declared schema. Empty
    string on success. This lets the proposer jump straight to the
    offending field without re-running the validator — aligned with
    the Meta-Harness paper §3 state-updates trace component.
    """
    stype = schema.get("type")
    here = path or "$"
    if stype == "object":
        if not isinstance(data, dict):
            return False, f"{here} expected object, got {type(data).__name__}"
        for req in schema.get("required", []):
            if req not in data:
                return False, f"{here} missing required field '{req}'"
        props = schema.get("properties", {})
        for key, prop_schema in props.items():
            if key in data:
                ok, where = _basic_schema_check_with_path(
                    data[key], prop_schema, f"{here}.{key}")
                if not ok:
                    return False, where
        return True, ""
    if stype == "array":
        if not isinstance(data, list):
            return False, f"{here} expected array, got {type(data).__name__}"
        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(data):
                ok, where = _basic_schema_check_with_path(
                    item, items_schema, f"{here}[{i}]")
                if not ok:
                    return False, where
        return True, ""
    if stype == "string":
        if not isinstance(data, str):
            return False, f"{here} expected string, got {type(data).__name__}"
        return True, ""
    if stype == "number":
        if not isinstance(data, (int, float)):
            return False, f"{here} expected number, got {type(data).__name__}"
        return True, ""
    if stype == "integer":
        if not isinstance(data, int):
            return False, f"{here} expected integer, got {type(data).__name__}"
        return True, ""
    if stype == "boolean":
        if not isinstance(data, bool):
            return False, f"{here} expected boolean, got {type(data).__name__}"
        return True, ""
    return True, ""  # no type constraint


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
