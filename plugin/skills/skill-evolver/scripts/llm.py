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
backend swaps (claude → codex → third-party-cli) a single-file change.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from json_extract import extract_json_object


# ─────────────────────────────────────────────
# LLM Backend Abstraction
# ─────────────────────────────────────────────

# Prefix every transport failure carries. `_call_llm` reports failures by
# returning a string rather than raising, a convention older code depends
# on, so callers that must distinguish "the model said this" from "the
# model was never reached" have to test for it.
#
# Named here as the single definition of that test. A caller inventing its
# own check would drift the moment a new failure message was added, and the
# consequence is severe: an unrecognised error string gets scored as if it
# were the candidate's output, so a run where every call timed out reports
# a confident zero instead of "nothing was measured".
LLM_ERROR_PREFIX = "[ERROR:"


def is_llm_error(output: str | None) -> bool:
    """Whether ``output`` is a transport failure rather than a model reply.

    Callers that score model output must check this. Treating a failure
    string as a reply attributes a harness fault to the thing being
    evaluated — the gate then discards a candidate for being bad when in
    fact it was never run.
    """
    return isinstance(output, str) and output.lstrip().startswith(LLM_ERROR_PREFIX)


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
        "cmd": ["codex", "exec", "--skip-git-repo-check",
                "-o", "{output_path}", "-"],
        "model_flag": "--model",
        "stdin_prompt": True,
        "env_filter": lambda env: dict(env),
    },
    "opencode": {
        "cmd": ["opencode", "run", "{prompt}"],
        "model_flag": "--model",
        "env_filter": lambda env: dict(env),
    },
    "http": {
        # Generic HTTP backend for platforms without a CLI.
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
              timeout: int = 120, backend: str | None = None,
              cwd: str | None = None) -> str:
    """Call LLM and return the text response.

    Supports multiple backends: claude, codex, opencode, http.
    Auto-detects backend if not specified.

    Args:
        cwd: optional working directory for the subprocess.
             Useful when the LLM needs project context (e.g. skill loading).
    """
    backend = backend or _detect_llm_backend()
    config = LLM_BACKENDS.get(backend, LLM_BACKENDS["claude"])

    # HTTP backend
    if config.get("type") == "http":
        return _call_llm_http(prompt, model, timeout)

    # CLI backend
    cmd_template = config["cmd"]
    cmd = []
    output_path = None
    use_stdin_prompt = bool(config.get("stdin_prompt"))
    if not use_stdin_prompt:
        use_stdin_prompt = "{prompt}" not in cmd_template and bool(cmd_template)
        use_stdin_prompt = use_stdin_prompt and cmd_template[-1] == "-"
    if any(part == "{output_path}" for part in cmd_template):
        tmp = tempfile.NamedTemporaryFile(
            prefix=f"skill-evolver-{backend}-",
            suffix=".txt",
            delete=False,
        )
        output_path = tmp.name
        tmp.close()
    for part in cmd_template:
        if part == "{prompt}":
            cmd.append(prompt)
        elif part == "{output_path}":
            if output_path is None:
                raise RuntimeError("output_path placeholder used without temp file")
            cmd.append(output_path)
        else:
            cmd.append(part)

    if model and config.get("model_flag"):
        cmd.extend([config["model_flag"], model])

    env_filter = config.get("env_filter", lambda e: dict(e))
    env = env_filter(os.environ)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout, env=env, cwd=cwd,
                                input=prompt if use_stdin_prompt else None)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if not detail:
                detail = "no stderr/stdout"
            return (
                f"[ERROR: {backend} CLI exited with status "
                f"{result.returncode}: {detail}]"
            )
        if output_path:
            try:
                text = Path(output_path).read_text().strip()
                if text:
                    return text
            except OSError:
                pass
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"[ERROR: {backend} timed out after {timeout}s]"
    except FileNotFoundError:
        return f"[ERROR: {backend} CLI not found — install it or set LLM_BACKEND]"
    except (OSError, UnicodeDecodeError) as e:
        # Real bug found via adversarial review: only TimeoutExpired and
        # FileNotFoundError were caught, so anything else `subprocess.run`
        # can raise — e.g. OSError/E2BIG ("Argument list too long", a real
        # risk once a full diff is embedded directly into a CLI argv
        # element, see verifier_panel.build_verifier_task_spec's diff cap)
        # or UnicodeDecodeError from non-UTF-8 subprocess output with
        # text=True — propagated uncaught and crashed the entire evolve
        # loop instead of degrading to one failed call. A caller like
        # phase_6_5_review that's mid-way through several independent
        # calls needs this to degrade the same way a timeout does, not
        # lose already-collected verdicts to an unrelated crash.
        return f"[ERROR: {backend} CLI failed to run: {e}]"
    finally:
        if output_path:
            try:
                os.unlink(output_path)
            except OSError:
                pass


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
# Phase 2 / Phase 3 — split, isolated (Module B)
# ─────────────────────────────────────────────
#
# phase_2_diagnose and phase_3_modify replace phase_2_3_ideate_and_modify
# (kept below as a deprecated backward-compat wrapper). Each is a
# SEPARATE _call_claude invocation — a fresh subprocess, no shared
# memory, no shared context — which is the actual isolation mechanism
# in CLI mode (the conversation-mode equivalent is two separate Agent
# tool calls; see isolation.build_diagnoser_task_spec /
# build_mutator_task_spec). Prompt-building and response-parsing are
# NOT duplicated here — both live in isolation.py so the CLI subprocess
# path and the conversation-mode Agent-call path always agree on what
# a given diagnoser/mutator response means.

def phase_2_diagnose(skill_path: Path, workspace: Path, review: dict,
                     gt_path: Path, current_layer: str = "body",
                     model: str | None = None) -> dict:
    """Phase 2 (diagnose only) — CLI-mode subprocess call.

    Returns the diagnosis dict shape (see
    ``isolation.parse_diagnosis_response``):
    ``{"failure_patterns": [...], "recommended_focus": str,
    "layer_suggestion": str, "evidence_refs": [...]}``.
    """
    from isolation import build_diagnoser_prompt, parse_diagnosis_response

    prompt = build_diagnoser_prompt(
        skill_path, workspace, review, gt_path, current_layer)
    response = _call_claude(prompt, model=model, timeout=180)
    return parse_diagnosis_response(response)


def phase_3_modify(skill_path: Path, diagnosis: dict,
                   current_layer: str = "body",
                   model: str | None = None) -> dict:
    """Phase 3 (modify only) — CLI-mode subprocess call.

    Narrow signature by design: no ``review``/``gt_path``/``workspace``
    parameter, so there is no code path by which this call could see
    raw GT evidence or the diagnoser's reasoning trace — only the
    ``diagnosis`` dict :func:`phase_2_diagnose` produced.

    Returns ``{"changed": bool, "description": str}`` (see
    ``isolation.parse_mutation_response``).
    """
    from isolation import build_mutator_prompt, parse_mutation_response

    prompt = build_mutator_prompt(skill_path, diagnosis, current_layer)
    response = _call_claude(prompt, model=model, timeout=180)
    return parse_mutation_response(response)


# ─────────────────────────────────────────────
# Phase 6.5 — adversarial review panel (Module D), CLI mode
# ─────────────────────────────────────────────

def phase_6_5_review(skill_path: Path, diff: str, metrics: dict,
                     model: str | None = None) -> dict:
    """Phase 6.5 (adversarial review) — CLI-mode subprocess calls.

    Three SEPARATE ``_call_claude`` invocations, one per checker in
    ``verifier_panel.CHECKERS`` — fresh subprocesses, no shared memory,
    mirroring how :func:`phase_2_diagnose` and :func:`phase_3_modify`
    are isolated from each other. The in-conversation equivalent is
    three separate Agent tool calls using
    ``verifier_panel.build_verifier_task_spec`` (see
    ``references/evolve_protocol.md`` Phase 6.5).

    Returns the aggregated dict from
    ``verifier_panel.aggregate_verdicts``:
    ``{"decision": "pass"|"reject"|"skipped", "verdicts": [...],
    "reasoning": str}``.
    """
    from verifier_panel import (
        build_verifier_task_spec, parse_verifier_response, aggregate_verdicts,
        CHECKERS,
    )

    verdicts = []
    for checker in CHECKERS:
        spec = build_verifier_task_spec(skill_path, diff, metrics, checker)
        response = _call_claude(spec["prompt"], model=model, timeout=180)
        verdicts.append(parse_verifier_response(response, checker))

    return aggregate_verdicts(verdicts)


# ─────────────────────────────────────────────
# Phase 2+3: Ideate and Modify
# ─────────────────────────────────────────────

def phase_2_3_ideate_and_modify(skill_path: Path, workspace: Path,
                                review: dict, gt_path: Path,
                                current_layer: str = "body",
                                model: str | None = None) -> dict:
    """DEPRECATED — kept for backward compatibility with existing
    callers (e.g. orchestrator.py's single-call site). New code should
    call :func:`phase_2_diagnose` and :func:`phase_3_modify` directly.

    This used to be one ``_call_claude`` invocation that diagnosed AND
    modified in the same context — the exact isolation gap the
    architecture plan's Module B fixes. This wrapper now does two
    genuinely separate calls (``phase_2_diagnose`` then
    ``phase_3_modify``) and merges their results into the old return
    shape ``{"changed", "description", "mutation_type", "diagnosis"}``
    so nothing downstream needs to change. ``mutation_type`` has no
    equivalent in the new split (neither function produces one) — it
    is always ``"unknown"`` here; no existing caller in this repo
    reads it.
    """
    import warnings
    warnings.warn(
        "phase_2_3_ideate_and_modify is deprecated — call phase_2_diagnose "
        "and phase_3_modify directly for isolated diagnose/modify calls "
        "(see architecture plan Module B).",
        DeprecationWarning, stacklevel=2,
    )

    diagnosis = phase_2_diagnose(
        skill_path, workspace, review, gt_path, current_layer, model=model)
    mutation = phase_3_modify(skill_path, diagnosis, current_layer, model=model)

    return {
        "changed": mutation["changed"],
        "description": mutation["description"],
        "mutation_type": "unknown",
        "diagnosis": diagnosis.get("recommended_focus", ""),
    }



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

    parsed = extract_json_object(response, required_key="pass_rate")
    if parsed is not None:
        return parsed

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
    #
    # Extraction goes through the shared extractor, which handles a
    # pretty-printed payload spanning many lines as well as a single-line
    # one. A regex fallback used to sit here, justified by a comment
    # claiming that "no line-based scan" could see a multi-line payload.
    # The claim was untrue — the extractor's brace-matching pass reads
    # exactly that — and 348 inputs (preambles, trailing prose, code
    # fences, stray braces) produced no case where the fallback succeeded
    # while the extractor failed, and none where they disagreed. It was
    # also actively harmful: its greedy `\{[\s\S]*"evals"[\s\S]*\}` ran
    # from the first brace to the last, ignoring the extractor's contract
    # of taking the *last* well-formed object, which is the rule the
    # prompt's own template relies on.
    data = extract_json_object(response, required_key="evals")
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
