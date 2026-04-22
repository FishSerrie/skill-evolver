#!/usr/bin/env python3
"""Trace enrichment helpers — paper §3 four-component capture.

Pure functions that take the skill corpus + assertion value as input
and return structured dicts carrying the Meta-Harness paper §3
(Lee et al. 2026, arXiv 2603.28052) trace components:

  - prompts        → captured by the evaluator wrapper
                     (``case.prompt`` + ``case.skill_loaded``)
  - tool calls     → ``check_script_rich`` captures stdout, stderr,
                     exit code, duration, resolved path
  - model outputs  → ``check_fact_coverage_rich`` captures per-fact
                     ``judge_verdicts[].reasoning`` (the LLM rationale)
  - state updates  → ``check_json_schema_rich`` captures the exact
                     ``schema_mismatch_path``; ``nearest_match``
                     captures matching state for contains/regex

None of these functions hold state. Extracting them from the
``LocalEvaluator`` class removes ~430 lines from evaluators.py
without any behavior change — callers just pass the skill corpus
and assertion value directly, and ``check_fact_coverage_rich``
takes a ``BinaryLLMJudge`` instance as an explicit parameter
instead of reaching through ``self``.

Reference: ``docs/private/migration-trace-architecture.md`` has the
full rationale and paper alignment table.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────
# Location / excerpt / near-miss helpers
# ─────────────────────────────────────────────

def locate_in_corpus(content: str, char_idx: int) -> dict:
    """Map a char offset in the concatenated corpus back to a
    ``{file, line}`` pointer.

    Uses the ``### <rel-path> ###`` headers inserted by
    ``LocalEvaluator._load_skill_corpus`` to identify which file the
    offset falls inside, then counts newlines from that header to
    produce a 1-indexed line number. Returns a dict with at least
    ``line`` (int) and optionally ``file`` (str relative path inside
    the skill).

    Used by contains/regex/not_contains match enrichment so the
    proposer can Read the exact source line without scanning.
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


def excerpt(content: str, start: int, end: int, margin: int = 40) -> str:
    """Return a clean ±margin-char window around a char range,
    collapsing newlines and stripping leading/trailing whitespace."""
    a = max(0, start - margin)
    b = min(len(content), end + margin)
    snippet = content[a:b].replace("\n", " ").strip()
    return re.sub(r"\s+", " ", snippet)


def nearest_match(content: str, needle: str) -> dict | None:
    """Find the longest prefix/suffix of ``needle`` that appears
    verbatim in ``content``.

    Returns None if fewer than half the needle's characters match
    anywhere. This is a diagnostic shortcut for ``contains`` failures
    — the most common failure mode is "close but not exact"
    (whitespace, punctuation, minor wording change), and the longest
    shared prefix reliably pinpoints the intended location. More
    formal edit-distance matching would need a library; the
    prefix/suffix approach is deterministic, library-free, and good
    enough for skill GT workloads.
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
                **locate_in_corpus(content, idx),
                "excerpt": excerpt(content, idx, idx + length),
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
                **locate_in_corpus(content, idx),
                "excerpt": excerpt(content, idx, idx + length),
            }
    return None


# ─────────────────────────────────────────────
# Skill snapshot (state updates trace component)
# ─────────────────────────────────────────────

def build_skill_snapshot(skill_path: Path) -> dict:
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


# ─────────────────────────────────────────────
# script_check — tool calls trace component
# ─────────────────────────────────────────────

def check_script_rich(script_path: str, content: str,
                      skill_path: Path) -> dict:
    """Run an external script and return a rich result dict.

    The result dict is the Meta-Harness paper §3 "tool calls" trace
    component for script_check — captures stdout, stderr, exit
    code, and wall-clock duration so the proposer can diagnose
    script failures WITHOUT re-running them.

    Script path resolution order:
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


# ─────────────────────────────────────────────
# fact_coverage — per-fact model outputs
# ─────────────────────────────────────────────

def check_fact_coverage_rich(val: str, assertion: dict,
                             content: str, judge: Any) -> dict:
    """Check fact coverage and return a rich per-fact breakdown.

    Takes an explicit ``judge`` parameter (a ``BinaryLLMJudge``
    instance) instead of reaching through ``self`` — this makes the
    function pure and testable outside any class.

    Two modes (both return structured verdicts so the proposer can
    see which specific facts were missing, not just "below
    threshold"):

      Preset: assertion has a 'facts' array → each fact is judged
        by ``judge.judge_with_reasoning`` and every verdict +
        rationale is recorded. Passes if ≥80% of facts are covered.

      Online: no preset facts → each comma-separated keyword in
        ``val`` is checked via substring match. Passes if ≥80% of
        keywords hit.

    The per-fact dict lines up with the Meta-Harness paper §3
    "model outputs" trace component — individual LLM verdicts
    become part of the structured case record.
    """
    facts = assertion.get("facts")

    if facts and isinstance(facts, list):
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


# ─────────────────────────────────────────────
# json_schema — structured state with failure path
# ─────────────────────────────────────────────

def check_json_schema_rich(schema_str: str, content: str) -> dict:
    """Validate content against a JSON schema and return a rich
    result dict including the specific validation failure path.

    Four outcomes the proposer needs to distinguish:

      success        → {"pass": true, "extracted_from": ...}
      schema_error   → the GT's schema itself didn't parse
      parse_error    → content JSON didn't parse
      schema_mismatch→ parsed fine but failed one schema constraint
                       (``schema_mismatch_path`` tells you which field)
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

    ok, failure_path = basic_schema_check_with_path(data, schema, "")
    if ok:
        return {"pass": True, "extracted_from": extracted_from}
    return {
        "pass": False,
        "schema_mismatch_path": failure_path,
        "extracted_from": extracted_from,
    }


def basic_schema_check(data: Any, schema: dict) -> bool:
    """Lightweight JSON schema validation without jsonschema dependency.

    Thin wrapper around :func:`basic_schema_check_with_path` that
    discards the failure-path string. Kept for any caller that only
    needs the boolean verdict.
    """
    ok, _ = basic_schema_check_with_path(data, schema, "")
    return ok


def basic_schema_check_with_path(
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
                ok, where = basic_schema_check_with_path(
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
                ok, where = basic_schema_check_with_path(
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
