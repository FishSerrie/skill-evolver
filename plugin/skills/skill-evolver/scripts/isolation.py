#!/usr/bin/env python3
"""Proposer/evaluator isolation — narrow function signatures instead of
"prompt says don't look".

Stage B of the multi-agent evolution architecture plan (see
docs/private/multi-agent-evolution-upgrade/architecture.md, Module B).
Root problem: today ``phase_2_3_ideate_and_modify`` (llm.py) diagnoses
failures AND makes the change in one call/one context, and in the
"primary" in-conversation execution path (see SKILL.md: "Claude IS the
LLM, there is zero claude -p shell-out") the SAME top-level Claude does
Phase 1 review, Phase 2 diagnose, Phase 3 modify, all in one
continuous, unbroken context — the diagnoser and the mutator are, in
that path, literally the same reasoning trace, not just "the same
model". Panickssery et al. 2024 (arXiv 2404.13076) diagnosed a related
but not identical failure mode (cross-model identity-label bias); our
actual problem here is closer to same-context confirmation bias — see
architecture plan Module B for the honest boundary between what that
paper proved and what this module is designed to fix.

This module provides the narrow-signature builders shared by BOTH
execution paths (CLI subprocess in llm.py, and in-conversation Agent
tool spawning), mirroring behavioral_runner.py's CLI/conversation
split for Module A:

  * ``build_diagnoser_prompt`` / ``build_diagnoser_task_spec`` —
    diagnoser only ever gets dev-split evidence. Never given holdout
    paths, not because the prompt says not to look, but because the
    function's own inputs are filtered to exclude anything
    holdout-derived before a single line of the prompt is assembled.
  * ``build_mutator_prompt`` / ``build_mutator_task_spec`` — mutator
    only ever gets the diagnoser's structured JSON output. The
    function signature has no ``review``/``gt_path``/``workspace``
    parameter at all — there is no code path by which the mutator's
    prompt could include raw evidence text or holdout content, because
    those values were never passed in to begin with.
  * ``parse_diagnosis_response`` / ``parse_mutation_response`` — shared
    normalizers so llm.py's CLI-mode subprocess path and the
    in-conversation Agent-call path parse the exact same response
    shape the exact same way (mirrors
    ``behavioral_runner.build_transcript_from_text`` in spirit).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def _strip_holdout_items(items: list) -> list:
    """Defense in depth: filters anything holdout-related out of a list
    of path-like strings before they can reach the diagnoser's prompt.

    ``phase_1_review`` (evolve_loop.py) is dev-derived today and should
    never put holdout paths into these fields in the first place — but
    the physical-exclusion guarantee this module exists to provide
    should not depend on every current and future caller of
    ``build_diagnoser_prompt`` getting that right. This is the actual
    enforcement point.
    """
    return [s for s in items if "holdout" not in str(s).lower()]


# ─────────────────────────────────────────────
# Diagnoser (Phase 2) — narrow signature, dev-only evidence
# ─────────────────────────────────────────────

def build_diagnoser_prompt(skill_path: Path, workspace: Path, review: dict,
                           gt_path: Path, current_layer: str = "body") -> str:
    """Build the diagnosis-only prompt (no modification instructions).

    Physical holdout exclusion: every path-like field pulled from
    ``review`` is filtered through :func:`_strip_holdout_items` (or,
    for the single ``cases_dir`` value, dropped outright if it mentions
    holdout) before it is interpolated into the returned string. This
    is what the architecture plan's regression test
    (``assert "holdout" not in build_diagnoser_prompt(...).lower()``)
    verifies — not that the prompt asks nicely, but that there is no
    code path left by which holdout content could reach this string.
    """
    skill_md_path = skill_path / "SKILL.md"
    skill_content = skill_md_path.read_text() if skill_md_path.exists() else ""

    recent_failures = json.dumps(review.get("recent_failures", []), ensure_ascii=False)
    successful = json.dumps(review.get("successful_patterns", []), ensure_ascii=False)

    cases_dir = review.get("cases_dir")
    if cases_dir is not None and "holdout" in str(cases_dir).lower():
        cases_dir = None
    failed_case_paths = _strip_holdout_items(review.get("failed_case_paths", []))
    suggested_greps = _strip_holdout_items(review.get("suggested_greps", []))
    last_meta_json = review.get("last_meta_json")

    path_context_lines = []
    if last_meta_json:
        path_context_lines.append(f"- Last iteration metadata: {last_meta_json}")
    if cases_dir:
        path_context_lines.append(
            f"- Per-case JSONs (grep-friendly, dev split only): {cases_dir}/case_*.json")
    if failed_case_paths:
        path_context_lines.append(
            f"- Failing cases (read these first): {', '.join(failed_case_paths[:10])}")
    if suggested_greps:
        path_context_lines.append("- Suggested greps:")
        for g in suggested_greps:
            path_context_lines.append(f"    {g}")
    path_context = "\n".join(path_context_lines)

    diagnosis_context = ""
    past_diagnoses = _strip_holdout_items(review.get("past_diagnoses", []))
    if past_diagnoses:
        diagnosis_context = "\n".join(f"- {d}" for d in past_diagnoses)

    return f"""You are DIAGNOSING why a skill's SKILL.md is failing GT assertions. You do NOT modify anything in this step — Read/Grep only. A separate step (which you are not part of) will make the change based on your diagnosis.

Current SKILL.md ({len(skill_content)} chars) is at: {skill_md_path}
Current layer: {current_layer}
Recent failures: {recent_failures}
Successful patterns: {successful}
Current best metric: {review.get('current_best_metric', 'unknown')}
Is stuck: {review.get('stuck', False)}

{"## Trace files (read selectively with the Read and Grep tools — do NOT try to read all of them; dev split only)" + chr(10) + path_context if path_context else ""}

{"## Past Diagnoses (insights from prior iterations)" + chr(10) + diagnosis_context if diagnosis_context else ""}

MANDATORY PROTOCOL:
1. If failed_case_paths are listed, READ THEM FIRST using the Read tool —
   each case_{{id}}.json has a "summary.failed_indexes" array pointing
   at which assertions failed.
2. Inside each failing assertion, look at the type-specific rich fields
   (match/nearest_match/found_at/judge_reasoning/etc.) — read the exact
   trace evidence, not just "pass": false.
3. For cross-iteration patterns, use the suggested greps with the Grep
   tool.
4. Do NOT guess — if no case JSON evidence points to a clear cause, say
   so explicitly in your diagnosis rather than inventing a hypothesis.
5. Do NOT edit any files. Do NOT propose exact code. Your output is a
   diagnosis, not a fix.

Output EXACTLY this JSON on the last line (no other text after it):
{{"failure_patterns": [{{"case_id": "...", "assertion_index": 0, "symptom": "...", "hypothesis": "..."}}], "recommended_focus": "one sentence for the next step to act on", "layer_suggestion": "body", "evidence_refs": ["case_3.json"]}}

If no clear pattern was found, output:
{{"failure_patterns": [], "recommended_focus": "", "layer_suggestion": "{current_layer}", "evidence_refs": []}}
"""


def build_diagnoser_task_spec(skill_path: Path, workspace: Path, review: dict,
                              gt_path: Path, current_layer: str = "body",
                              subagent_type: str = "general-purpose") -> dict:
    """Conversation-mode Agent tool call spec for the diagnoser.

    Mirrors ``behavioral_runner.build_behavioral_task_spec`` — this
    function does NOT invoke the Agent tool itself; it only prepares
    inputs. The Claude driving the evolve loop must issue the actual
    Agent tool call with ``prompt=spec["prompt"]`` and pass the
    sub-agent's returned text to :func:`parse_diagnosis_response` to
    get the diagnosis dict.

    The sub-agent spawned this way has no access to the main
    conversation's history (the Agent tool's own isolation guarantee)
    — it only ever sees the prompt text :func:`build_diagnoser_prompt`
    built, which itself never contains holdout content. That is the
    same physical-isolation mechanism Module A's behavioral runner and
    Module D's verifier panel use: an independent Agent tool call, not
    "same context, asked not to look."
    """
    return {
        "prompt": build_diagnoser_prompt(
            skill_path, workspace, review, gt_path, current_layer),
        "subagent_type": subagent_type,
        "description": f"Diagnose failures for {skill_path.name} (layer={current_layer})",
        "isolation": "subagent_context",
    }


def parse_diagnosis_response(text: str) -> dict:
    """Parse the diagnoser's raw text output into the diagnosis dict shape.

    Shared by llm.py's CLI-mode subprocess path (parses
    ``_call_claude``'s stdout) and the conversation-mode Agent-call
    path (parses the sub-agent's returned text) — both must interpret
    the same "last JSON line" convention identically, so
    ``phase_2_diagnose`` and a conversation-mode caller never disagree
    about what a given diagnoser response means.

    Mirrors the defensive-default parsing ``phase_2_3_ideate_and_modify``
    already does (Red-team finding #1, llm.py) — a malformed or
    missing JSON line degrades to a safe empty diagnosis rather than
    raising, so one bad diagnoser response can't crash the loop.
    """
    for line in reversed(text.split("\n")):
        line = line.strip()
        if line.startswith("{") and "failure_patterns" in line:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            return {
                "failure_patterns": parsed.get("failure_patterns") or [],
                "recommended_focus": str(parsed.get("recommended_focus", "")),
                "layer_suggestion": str(parsed.get("layer_suggestion", "")),
                "evidence_refs": parsed.get("evidence_refs") or [],
            }

    return {"failure_patterns": [], "recommended_focus": "",
            "layer_suggestion": "", "evidence_refs": []}
