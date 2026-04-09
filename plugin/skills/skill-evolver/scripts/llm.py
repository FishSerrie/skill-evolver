#!/usr/bin/env python3
"""LLM backend + LLM-driven phases, extracted from evolve_loop.py.

Contents:

  * ``LLM_BACKENDS`` registry — CLI + HTTP backend definitions
  * ``_detect_llm_backend`` — auto-detection logic
  * ``_call_llm`` / ``_call_llm_http`` / ``_call_claude`` — the call
    layer used by evaluators.py (lazy-imported) and the ideate/eval
    phases below
  * ``phase_2_3_ideate_and_modify`` — the Meta-Harness active diagnosis
    prompt wrapping ``_call_llm``
  * ``run_l2_eval_via_claude`` / ``_local_eval`` — L2 behavior eval
    paths (LLM-based with local fallback)
  * ``auto_construct_gt`` — bootstrap GT generator for fresh skills

Split rationale: these are all the places that actually invoke or
delegate to an external LLM. Keeping them in one module makes
backend swaps (claude → codex → openclaw) a single-file change.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


# ─────────────────────────────────────────────
# LLM Backend Abstraction
# ─────────────────────────────────────────────

# Supported LLM backends for Phase 2+3 (Ideate + Modify)
# The backend is auto-detected or configured via LLM_BACKEND env var.
#
# Backend registry: name → (command_template, env_filter)
LLM_BACKENDS = {
    "claude": {
        "cmd": ["claude", "-p", "{prompt}", "--output-format", "text"],
        "model_flag": "--model",
        "env_filter": lambda env: {k: v for k, v in env.items() if k != "CLAUDECODE"},
    },
    "codex": {
        "cmd": ["codex", "-q", "{prompt}"],
        "model_flag": "--model",
        "env_filter": lambda env: dict(env),
    },
    "opencode": {
        "cmd": ["opencode", "run", "{prompt}"],
        "model_flag": "--model",
        "env_filter": lambda env: dict(env),
    },
    "http": {
        # For platforms without a CLI (e.g., OpenClaw).
        # Uses EVOLVER_LLM_URL env var to POST to an HTTP endpoint.
        # Request: {"prompt": "...", "model": "..."}
        # Response: {"text": "..."}
        "type": "http",
    },
}


def _detect_llm_backend() -> str:
    """Auto-detect available LLM backend.

    Priority: LLM_BACKEND env var > claude > codex > opencode > http
    """
    override = os.environ.get("LLM_BACKEND", "").lower()
    if override and override in LLM_BACKENDS:
        return override

    # Try to find CLI tools
    for name in ["claude", "codex", "opencode"]:
        try:
            subprocess.run([name, "--version"], capture_output=True, timeout=5)
            return name
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue

    # Check for HTTP endpoint
    if os.environ.get("EVOLVER_LLM_URL"):
        return "http"

    return "claude"  # default, will fail gracefully if not installed


def _call_llm(prompt: str, model: str | None = None,
              timeout: int = 120, backend: str | None = None) -> str:
    """Call LLM and return the text response.

    Supports multiple backends: claude, codex, opencode, http.
    Auto-detects backend if not specified.
    """
    backend = backend or _detect_llm_backend()
    config = LLM_BACKENDS.get(backend, LLM_BACKENDS["claude"])

    # HTTP backend
    if config.get("type") == "http":
        return _call_llm_http(prompt, model, timeout)

    # CLI backend
    cmd_template = config["cmd"]
    cmd = []
    for part in cmd_template:
        if part == "{prompt}":
            cmd.append(prompt)
        else:
            cmd.append(part)

    if model and config.get("model_flag"):
        cmd.extend([config["model_flag"], model])

    env_filter = config.get("env_filter", lambda e: dict(e))
    env = env_filter(os.environ)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout, env=env)
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"[ERROR: {backend} timed out after {timeout}s]"
    except FileNotFoundError:
        return f"[ERROR: {backend} CLI not found — install it or set LLM_BACKEND]"


def _call_llm_http(prompt: str, model: str | None = None,
                   timeout: int = 120) -> str:
    """Call LLM via HTTP endpoint (for platforms without CLI)."""
    import urllib.request
    import urllib.error

    url = os.environ.get("EVOLVER_LLM_URL", "")
    if not url:
        return "[ERROR: EVOLVER_LLM_URL not set for http backend]"

    payload = json.dumps({"prompt": prompt, "model": model or ""}).encode()
    headers = {"Content-Type": "application/json"}

    api_key = os.environ.get("EVOLVER_LLM_API_KEY", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data.get("text", data.get("content", data.get("output", "")))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        return f"[ERROR: HTTP LLM call failed: {e}]"


# Keep backward compat alias
_call_claude = _call_llm


# ─────────────────────────────────────────────
# Phase 2+3: Ideate and Modify
# ─────────────────────────────────────────────

def phase_2_3_ideate_and_modify(skill_path: Path, workspace: Path,
                                review: dict, gt_path: Path,
                                current_layer: str = "body",
                                model: str | None = None) -> dict:
    """Phase 2+3: Use claude -p to analyze failures and make an atomic change.

    Returns: {"changed": bool, "description": str, "mutation_type": str}
    """
    skill_content = (skill_path / "SKILL.md").read_text()

    # Build context for Claude
    recent_failures = json.dumps(review.get("recent_failures", []), ensure_ascii=False)
    successful = json.dumps(review.get("successful_patterns", []), ensure_ascii=False)

    # Meta-Harness §2 filesystem access: give Claude file paths, not
    # preloaded content. Claude uses the Read/Grep tools (both in CLI
    # `claude -p` mode and in-conversation mode) to selectively pull
    # case JSON content. This matches paper §2:
    #   "the proposer retrieves via standard operations such as grep
    #    and cat rather than ingesting them as a single prompt"
    cases_dir = review.get("cases_dir")
    failed_case_paths = review.get("failed_case_paths", [])
    suggested_greps = review.get("suggested_greps", [])
    last_meta_json = review.get("last_meta_json")

    path_context_lines = []
    if last_meta_json:
        path_context_lines.append(
            f"- Last iteration metadata: {last_meta_json}")
    if cases_dir:
        path_context_lines.append(
            f"- Per-case JSONs (grep-friendly): {cases_dir}/case_*.json")
    if failed_case_paths:
        path_context_lines.append(
            f"- Failing cases (read these first): {', '.join(failed_case_paths[:10])}")
    if suggested_greps:
        path_context_lines.append("- Suggested greps:")
        for g in suggested_greps:
            path_context_lines.append(f"    {g}")
    path_context = "\n".join(path_context_lines)

    diagnosis_context = ""
    past_diagnoses = review.get("past_diagnoses", [])
    if past_diagnoses:
        diagnosis_context = "\n".join(f"- {d}" for d in past_diagnoses)

    prompt = f"""You are optimizing a skill's SKILL.md. Make ONE atomic improvement.

Current SKILL.md ({len(skill_content)} chars) is at: {skill_path / 'SKILL.md'}

Current layer: {current_layer}
Recent failures: {recent_failures}
Successful patterns: {successful}
Current best metric: {review.get('current_best_metric', 'unknown')}
Is stuck: {review.get('stuck', False)}

{"## Trace files (read selectively with the Read and Grep tools — do NOT try to read all of them)" + chr(10) + path_context if path_context else ""}

## Per-case JSON schema (what to look for inside each case_{{id}}.json)

Each case JSON produced by LocalEvaluator.full_eval carries the
Meta-Harness paper §3 four trace components in structured form. When
you Read a case file, look for these fields by assertion type —
they're the difference between guessing and diagnosing.

  case.skill_loaded  (state updates trace component)
    - path, size_bytes, skill_md_lines
    - description_chars            ← front-matter description length
    - references_loaded: [str]     ← what the evaluator corpus-loaded
    - agents_loaded: [str]

  case.summary.failed_indexes: [int]  ← the exact assertions that failed

  assertion (common fields)
    - index, type, value, description, pass

  assertion.type == "contains" (or "regex")
    PASS: match.file / match.line / match.excerpt
          → tells you WHERE the needle hit; useful for cross-checking
            that the match landed in the intended section
    FAIL: nearest_match: {{matched_text, missing_suffix|missing_prefix,
                           match_ratio, file, line, excerpt}} or None
          → if nearest_match is populated, the needle is CLOSE to
            appearing — usually a whitespace/punctuation diff. If None,
            the content is missing entirely.

  assertion.type == "not_contains"
    FAIL: found_at: {{file, line, excerpt}}
          → tells you WHERE the forbidden string actually lives so
            you can delete it precisely

  assertion.type == "script_check"  (tool calls trace component)
    BOTH: exit_code, stdout, stderr, duration_ms, resolved_path
          → exit_code + stdout/stderr are THE reason a script_check
            failed. DO NOT re-run the script — read these fields
            from the case JSON and diagnose from there.

  assertion.type == "path_hit"  (model outputs trace component)
    BOTH: judge_reasoning: str
          → the LLM judge's 1-2 sentence rationale. If it says "found
            at line 42", go Read that line in the skill source.

  assertion.type == "fact_coverage" preset mode  (model outputs ×N)
    BOTH: judge_verdicts: [{{fact, verdict, reasoning}}]
          passed_facts, total_facts
          → find the facts whose verdict is false; the reasoning
            tells you why each one was marked missing

  assertion.type == "fact_coverage" online mode
    BOTH: keyword_hits: [str], keyword_total

{"## Past Diagnoses (insights from prior iterations)" + chr(10) + diagnosis_context if diagnosis_context else ""}

MANDATORY PROTOCOL (Meta-Harness §2 active diagnosis):
1. If failed_case_paths are listed, READ THEM FIRST using the Read tool —
   each case_{{id}}.json has a "summary.failed_indexes" array pointing
   at which assertions failed. Use it to jump straight to the failing
   assertion record without scanning the whole file.
2. Inside each failing assertion, look for the type-specific rich
   fields (above). Don't just read "pass": false — read the exact
   trace evidence. A contains failure with nearest_match.match_ratio
   of 0.9 means something very different from nearest_match == None.
3. For cross-iteration patterns, use the suggested greps with the Grep
   tool — e.g. grep for "pass": false across iteration-E*/cases/*.json
   to see if the same case has been failing repeatedly.
4. State your diagnosis in the format:
     "Case X assertion Y (type=Z) failed because [specific field
      evidence from the case JSON, e.g. 'stderr shows
      ModuleNotFoundError: foo' or 'nearest_match found at
      SKILL.md:87 with match_ratio 0.9 — missing the leading slash']"
5. Then propose ONE atomic change that directly addresses the
   diagnosed cause.
6. Do NOT guess — if no case JSON evidence points to a clear cause,
   say so and either fall back to exploring less-tried mutation types
   or ask for a GT probe expansion.

Read the SKILL.md at {skill_path / 'SKILL.md'}, then make your change.

After making the change, output EXACTLY this JSON on the last line:
{{"changed": true, "description": "one sentence describing what you changed", "mutation_type": "body_rewrite", "diagnosis": "Case X assertion Y failed because [trace evidence]; I changed Z"}}

If you find nothing to improve, output:
{{"changed": false, "description": "no improvement found", "mutation_type": "none", "diagnosis": ""}}
"""

    response = _call_claude(prompt, model=model, timeout=180)

    # Parse the JSON from the last line and VALIDATE shape.
    # Red-team finding #1 (iter 30): the prior code returned the parsed
    # dict as-is, so a malformed LLM response like `{"changed": true}`
    # (missing `description` / `mutation_type`) would crash the
    # orchestrator's `result_23['description']` access with a KeyError.
    # Instead, normalize the dict with safe defaults so every caller
    # sees a well-formed shape even when the LLM output is partial.
    for line in reversed(response.split("\n")):
        line = line.strip()
        if line.startswith("{") and "changed" in line:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            return {
                "changed": bool(parsed.get("changed", False)),
                "description": str(parsed.get("description", "llm did not provide description")),
                "mutation_type": str(parsed.get("mutation_type", "unknown")),
                "diagnosis": str(parsed.get("diagnosis", "")),
            }

    return {"changed": False, "description": "could not parse claude response",
            "mutation_type": "none", "diagnosis": ""}


# ─────────────────────────────────────────────
# L2 Eval (claude-p driven with local fallback)
# ─────────────────────────────────────────────

def run_l2_eval_via_claude(skill_path: Path, gt_path: Path,
                           workspace: Path, model: str | None = None) -> dict:
    """Phase 5 L2: Use claude -p to evaluate skill against GT cases.

    Returns: {"pass_rate": float, "total_passed": int, "total_assertions": int, ...}
    """
    gt_data = json.loads(gt_path.read_text())
    dev_cases = [c for c in gt_data.get("evals", []) if c.get("split", "dev") == "dev"]

    prompt = f"""You are a grader. Evaluate the skill at {skill_path / 'SKILL.md'} against these test cases.

Read the SKILL.md first, then for each case, check every assertion (contains/not_contains/regex) against the SKILL.md content.

Test cases:
{json.dumps(dev_cases, indent=2, ensure_ascii=False)}

Output EXACTLY this JSON format on the last line (no other text after it):
{{"pass_rate": 0.95, "total_passed": 19, "total_assertions": 20, "failed": [{{"case_id": 1, "assertion": "description of failed assertion"}}]}}
"""

    response = _call_claude(prompt, model=model, timeout=120)

    for line in reversed(response.split("\n")):
        line = line.strip()
        if line.startswith("{") and "pass_rate" in line:
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass

    # Fallback: do it locally
    return _local_eval(skill_path, gt_path)


def _local_eval(skill_path: Path, gt_path: Path) -> dict:
    """Fallback local eval when claude -p is unavailable."""
    skill_content = (skill_path / "SKILL.md").read_text()
    gt_data = json.loads(gt_path.read_text())
    dev_cases = [c for c in gt_data.get("evals", []) if c.get("split", "dev") == "dev"]

    total_p = total_t = 0
    failed = []
    for c in dev_cases:
        for a in c.get("assertions", []):
            total_t += 1
            ok = False
            if a["type"] == "contains":
                ok = a["value"].lower() in skill_content.lower()
            elif a["type"] == "not_contains":
                ok = a["value"].lower() not in skill_content.lower()
            elif a["type"] == "regex":
                ok = bool(re.search(a["value"], skill_content))
            if ok:
                total_p += 1
            else:
                failed.append({"case_id": c["id"], "assertion": a.get("description", a["value"])})

    return {
        "pass_rate": total_p / total_t if total_t else 0,
        "total_passed": total_p,
        "total_assertions": total_t,
        "failed": failed,
    }


# ─────────────────────────────────────────────
# GT Auto-Construction
# ─────────────────────────────────────────────

def auto_construct_gt(skill_path: Path, output_path: Path,
                      model: str | None = None) -> dict | None:
    """Auto-construct GT data by analyzing the skill's SKILL.md.

    Uses LLM to read the skill and generate realistic test cases
    with assertions. Saves to output_path as evals.json.

    This follows the Creator's test case construction methodology:
    understand skill → write realistic test prompts → draft assertions.

    Returns: {"count": int} on success, None on failure.
    """
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return None

    skill_content = skill_md.read_text()
    if len(skill_content.strip()) < 50:
        return None  # SKILL.md too short to auto-construct GT from

    prompt = f"""You are generating ground-truth test data for evaluating a skill.

Read this SKILL.md and generate 8 test cases (6 dev + 2 holdout):

{skill_content[:6000]}

For each test case, create realistic user prompts that would trigger this skill,
and assertions that check whether the SKILL.md content properly addresses them.

Use these assertion types:
- "contains": SKILL.md must contain this text (case-insensitive)
- "not_contains": SKILL.md must NOT contain this text
- "regex": SKILL.md must match this regex pattern

Output EXACTLY this JSON format (no other text):
{{
  "evals": [
    {{
      "id": 1,
      "prompt": "realistic user prompt",
      "assertions": [
        {{"type": "contains", "value": "expected text", "description": "what this checks"}}
      ],
      "split": "dev",
      "metadata": {{"note": "why this case matters"}}
    }}
  ]
}}

Requirements:
- 6 cases with "split": "dev", 2 cases with "split": "holdout"
- Each case should test a different aspect of the skill
- Include at least one not_contains assertion (negative test)
- Make prompts realistic (how a real user would trigger this skill)
- Assertions should check that SKILL.md has the right instructions
"""

    response = _call_llm(prompt, model=model, timeout=180)

    # Parse JSON from response, then VALIDATE shape before writing.
    # Red-team finding #3 (iter 30): the prior code wrote whatever the
    # LLM returned directly to evals.json. A malformed response like
    # `{"evals": [{"id": 1, "prompt": "test"}]}` (missing `assertions`,
    # no `split`) would pass through, poisoning the baseline eval with
    # zero-assertion cases that artificially inflate pass_rate to 1.0.
    data = None
    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("{") and '"evals"' in line:
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                pass
    if data is None:
        json_match = re.search(r'\{[\s\S]*"evals"[\s\S]*\}', response)
        if json_match:
            try:
                data = json.loads(json_match.group())
            except json.JSONDecodeError:
                pass
    if data is None:
        return None

    # Schema validation — every case must have a non-empty assertions
    # list plus prompt + split. Reject the whole batch on any violation
    # (safer than partial writes; the caller can retry or fall back).
    valid = _validate_gt_schema(data)
    if not valid:
        return None

    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return {"count": len(data.get("evals", []))}


def _validate_gt_schema(data: object) -> bool:
    """Return True if ``data`` matches the GT schema strictly enough to
    be safely written to ``evals.json``.

    Checks every case has: int-convertible ``id``, non-empty string
    ``prompt``, non-empty list ``assertions`` where each assertion has
    a string ``type``, and a ``split`` string. Extra keys are ignored.
    Zero-assertion cases are rejected because they inflate ``pass_rate``
    to 1.0 (the ``if total_t else 0`` guard in LocalEvaluator treats a
    no-op case as trivially passing).
    """
    if not isinstance(data, dict):
        return False
    evals = data.get("evals")
    if not isinstance(evals, list) or not evals:
        return False
    valid_splits = {"dev", "holdout", "regression"}
    for case in evals:
        if not isinstance(case, dict):
            return False
        if "id" not in case:
            return False
        prompt = case.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return False
        assertions = case.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            return False
        for a in assertions:
            if not isinstance(a, dict):
                return False
            atype = a.get("type")
            if not isinstance(atype, str) or not atype:
                return False
        split = case.get("split", "dev")
        if split not in valid_splits:
            return False
    return True
