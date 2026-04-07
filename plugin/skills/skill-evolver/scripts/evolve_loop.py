#!/usr/bin/env python3
"""Evolve Loop — 8-phase orchestrator for skill evolution.

Usage:
    # FULL AUTO LOOP (the real thing)
    python evolve_loop.py <skill-path> --gt <gt-json> --run [--max-iterations 20]

    # Setup only
    python evolve_loop.py <skill-path> --gt <gt-json>

    # Cleanup
    python evolve_loop.py <skill-path> --cleanup

This script runs the complete 8-phase evolve cycle. Phase 2 (Ideate) and
Phase 3 (Modify) use `claude -p` subprocess to invoke LLM reasoning.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import find_creator_path, find_workspace, find_evolve_dir, validate_frontmatter, parse_skill_md
from aggregate_results import parse_results_tsv, calculate_summary
from evaluators import get_evaluator, parse_evaluator_from_plan, Evaluator


# ─────────────────────────────────────────────
# Phase 0: Setup (fully automated)
# ─────────────────────────────────────────────

def phase_0_setup(skill_path: Path, gt_path: Path,
                  workspace: Path | None = None) -> dict:
    """Create workspace, initialize memory, generate evolve_plan template.

    On first use, auto-detects creator tools (skill-creator, claw-creator, etc.)
    and configures the evaluation pipeline accordingly.

    Returns: {"workspace", "evolve_dir", "plan_path", "baseline_needed", "creator_config"}
    """
    from setup_workspace import setup_workspace  # noqa: sibling import
    from common import setup_creator_config

    ws = workspace or find_workspace(skill_path)
    result = setup_workspace(skill_path, ws)

    evolve_dir = Path(result["evolve_dir"])
    plan_path = evolve_dir / "evolve_plan.md"
    results_tsv = evolve_dir / "results.tsv"

    # First-use creator detection and configuration
    creator_config = setup_creator_config(ws, skill_path)

    # Check if baseline already exists
    baseline_needed = True
    if results_tsv.exists():
        content = results_tsv.read_text()
        if "baseline" in content:
            baseline_needed = False

    return {
        "workspace": str(ws),
        "evolve_dir": str(evolve_dir),
        "plan_path": str(plan_path),
        "baseline_needed": baseline_needed,
        "gt_path": str(gt_path),
        "skill_path": str(skill_path),
        "creator_config": creator_config,
    }


# ─────────────────────────────────────────────
# Phase 1: Review (fully automated)
# ─────────────────────────────────────────────

def phase_1_review(workspace: Path) -> dict:
    """Read memory and analyze current state.

    Returns: {"iterations", "keeps", "discards", "stuck", "recent_failures",
              "successful_patterns", "current_best_metric", "git_log"}
    """

    evolve_dir = workspace / "evolve"
    rows = parse_results_tsv(workspace)
    summary = calculate_summary(rows)

    # Read experiments.jsonl for detailed patterns
    experiments_path = evolve_dir / "experiments.jsonl"
    recent_experiments = []
    if experiments_path.exists():
        lines = experiments_path.read_text().strip().split("\n")
        for line in lines[-10:]:  # last 10
            if line.strip():
                try:
                    recent_experiments.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    # Extract patterns
    successful_patterns = [
        e.get("mutation_type") for e in recent_experiments
        if e.get("status") == "keep"
    ]
    recent_failures = [
        {"intent": e.get("intent"), "reason": e.get("failure_reason")}
        for e in recent_experiments
        if e.get("status") in ("discard", "crash")
    ][-5:]  # last 5 failures

    # Try to get git log
    git_log = ""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-15"],
            capture_output=True, text=True, timeout=5,
            cwd=str(workspace.parent),  # skill parent dir
        )
        if result.returncode == 0:
            git_log = result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Meta-Harness: read execution traces from recent failed iterations
    # Enables active diagnosis in Phase 2 (grep traces, not guess)
    recent_traces = {}
    if rows:
        # Find the most recent iteration with traces
        for row in reversed(rows):
            iter_num = row.get("iteration", 0)
            trace_dir = evolve_dir / f"iteration-E{iter_num}" / "traces"
            if trace_dir.exists():
                for trace_file in sorted(trace_dir.glob("case_*.md"))[:10]:
                    recent_traces[trace_file.stem] = trace_file.read_text()[:2000]
                break  # only the most recent iteration's traces

    # Collect past diagnoses (counterfactual insights from prior iterations)
    past_diagnoses = [
        e.get("diagnosis") for e in recent_experiments
        if e.get("diagnosis")
    ][-5:]

    return {
        "iterations": summary["total_iterations"],
        "keeps": summary["keep_count"],
        "discards": summary["discard_count"],
        "crashes": summary["crash_count"],
        "stuck": summary.get("is_stuck", False),
        "current_best_metric": summary.get("best_metric"),
        "best_iteration": summary.get("best_iteration"),
        "latest_metric": summary.get("latest_metric"),
        "trajectory": summary.get("trajectory", []),
        "recent_failures": recent_failures,
        "successful_patterns": successful_patterns,
        "git_log": git_log,
        "recent_traces": recent_traces,
        "past_diagnoses": past_diagnoses,
    }


# ─────────────────────────────────────────────
# Phase 2: Ideate (Claude reasoning required)
# This function prepares the context; Claude makes the decision.
# ─────────────────────────────────────────────

def phase_2_prepare_ideation(workspace: Path, review: dict,
                             evolve_plan: dict | None = None) -> dict:
    """Prepare context for Claude to decide what to change.

    Returns context data. Claude reads this + evolve_plan.md to decide.
    """
    # Determine current layer from plan or history
    current_layer = "body"  # default
    if evolve_plan and "optimization_priority" in evolve_plan:
        current_layer = evolve_plan["optimization_priority"][0] if evolve_plan["optimization_priority"] else "body"

    # Priority suggestions based on review
    suggestions = []
    if review.get("stuck"):
        suggestions.append("STUCK detected — try radical strategy (different layer or approach)")
    if review.get("recent_failures"):
        last_failure = review["recent_failures"][-1]
        suggestions.append(f"Last failure: {last_failure.get('intent', '?')} — avoid repeating")
    if review.get("successful_patterns"):
        suggestions.append(f"Successful patterns: {', '.join(set(review['successful_patterns'][-3:]))}")

    return {
        "current_layer": current_layer,
        "suggestions": suggestions,
        "recent_failures": review.get("recent_failures", []),
        "successful_patterns": review.get("successful_patterns", []),
        "current_best": review.get("current_best_metric"),
        "prompt_for_claude": (
            "Based on the above context, decide:\n"
            "1. What to change (one atomic modification)\n"
            "2. mutation_type (body_rewrite / body_simplify / rule_reorder / template_change)\n"
            "3. One-sentence description of the change\n"
            "Then execute Phase 3: make the change."
        ),
    }


# ─────────────────────────────────────────────
# Phase 4: Commit (fully automated)
# ─────────────────────────────────────────────

def phase_4_commit(skill_path: Path, layer: str, description: str) -> dict:
    """Git add + commit the changes.

    Returns: {"success", "commit_hash", "files_changed"}
    """
    try:
        # Stage changes
        subprocess.run(["git", "add", "-A"], cwd=str(skill_path),
                       capture_output=True, timeout=10)

        # Check if there are changes
        status = subprocess.run(["git", "status", "--porcelain"],
                                cwd=str(skill_path), capture_output=True,
                                text=True, timeout=10)
        if not status.stdout.strip():
            return {"success": False, "commit_hash": None,
                    "files_changed": [], "error": "No changes to commit"}

        # Commit
        msg = f"experiment({layer}): {description}"
        result = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(skill_path), capture_output=True, text=True, timeout=10,
        )

        if result.returncode != 0:
            return {"success": False, "commit_hash": None,
                    "files_changed": [], "error": result.stderr.strip()}

        # Get commit hash
        hash_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(skill_path), capture_output=True, text=True, timeout=5,
        )
        commit_hash = hash_result.stdout.strip()

        # Get changed files
        diff_result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1"],
            cwd=str(skill_path), capture_output=True, text=True, timeout=5,
        )
        files = [f.strip() for f in diff_result.stdout.strip().split("\n") if f.strip()]

        return {"success": True, "commit_hash": commit_hash,
                "files_changed": files, "error": None}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "commit_hash": None,
                "files_changed": [], "error": str(e)}


# ─────────────────────────────────────────────
# Phase 5: Verify — L1 gate (automated)
# L2 eval requires Claude orchestration (see run_l2_eval.py)
# ─────────────────────────────────────────────

def phase_5_l1_gate(skill_path: Path, gt_path: Path | None = None) -> dict:
    """Run L1 quick gate. Returns {"pass", "checks", "errors"}."""
    from run_l1_gate import run_l1_gate
    return run_l1_gate(skill_path, gt_path)


# ─────────────────────────────────────────────
# Phase 6: Gate Decision (fully automated)
# ─────────────────────────────────────────────

def phase_6_gate_decision(current_metrics: dict, baseline_metrics: dict,
                          thresholds: dict | None = None) -> dict:
    """Multi-gate decision. Returns {"decision", "reasons"}.

    decision: "keep" | "discard" | "revert"
    """
    th = thresholds or {}
    min_delta = th.get("min_delta", 0.02)
    trigger_tolerance = th.get("trigger_tolerance", 0.05)
    max_token_increase = th.get("max_token_increase", 0.20)
    max_latency_increase = th.get("max_latency_increase", 0.20)
    regression_tolerance = th.get("regression_tolerance", 0.05)
    noise_threshold = th.get("noise_threshold", 0.01)

    reasons = []

    # Hard failures
    if current_metrics.get("status") in ("crash", "timeout"):
        return {"decision": "revert", "reasons": ["crash or timeout"]}

    if not current_metrics.get("l1_pass", True):
        return {"decision": "discard", "reasons": ["L1 gate failed"]}

    # Multi-gate AND logic
    cur_pr = current_metrics.get("pass_rate", 0)
    base_pr = baseline_metrics.get("pass_rate", 0)

    quality_ok = cur_pr >= base_pr + min_delta
    if quality_ok:
        reasons.append(f"quality: {cur_pr:.3f} >= {base_pr:.3f} + {min_delta}")
    else:
        reasons.append(f"quality FAIL: {cur_pr:.3f} < {base_pr:.3f} + {min_delta}")

    cur_trigger = current_metrics.get("trigger_f1", 1.0)
    base_trigger = baseline_metrics.get("trigger_f1", 1.0)
    trigger_ok = cur_trigger >= base_trigger * (1 - trigger_tolerance)
    if not trigger_ok:
        reasons.append(f"trigger FAIL: {cur_trigger:.3f} < {base_trigger:.3f} * {1 - trigger_tolerance}")

    cur_tokens = current_metrics.get("tokens_mean", 0)
    base_tokens = baseline_metrics.get("tokens_mean", 1)
    cost_ok = base_tokens == 0 or cur_tokens <= base_tokens * (1 + max_token_increase)
    if not cost_ok:
        reasons.append(f"cost FAIL: {cur_tokens} > {base_tokens} * {1 + max_token_increase}")

    cur_dur = current_metrics.get("duration_mean", 0)
    base_dur = baseline_metrics.get("duration_mean", 1)
    latency_ok = base_dur == 0 or cur_dur <= base_dur * (1 + max_latency_increase)
    if not latency_ok:
        reasons.append(f"latency FAIL: {cur_dur:.1f} > {base_dur:.1f} * {1 + max_latency_increase}")

    cur_reg = current_metrics.get("regression_pass", 1.0)
    base_reg = baseline_metrics.get("regression_pass", 1.0)
    regression_ok = cur_reg >= base_reg * (1 - regression_tolerance)
    if not regression_ok:
        reasons.append(f"regression FAIL: {cur_reg:.3f} < {base_reg:.3f} * {1 - regression_tolerance}")

    if quality_ok and trigger_ok and cost_ok and latency_ok and regression_ok:
        return {"decision": "keep", "reasons": reasons}

    # Noise check
    if abs(cur_pr - base_pr) < noise_threshold:
        reasons.append(f"change within noise ({noise_threshold})")

    return {"decision": "discard", "reasons": reasons}


# ─────────────────────────────────────────────
# Phase 7: Log (fully automated)
# ─────────────────────────────────────────────

def phase_7_log(workspace: Path, iteration: int, commit: str,
                metric: float, delta: float, trigger_f1: float,
                tokens: int, guard: str, status: str,
                layer: str, description: str,
                experiment: dict | None = None,
                traces: dict | None = None) -> None:
    """Append to results.tsv, experiments.jsonl, and write execution traces.

    Traces (Meta-Harness pattern): full evaluation output per test case,
    stored as individual files for active diagnosis in Phase 1/2.
    """
    evolve_dir = workspace / "evolve"

    # results.tsv
    tsv_path = evolve_dir / "results.tsv"
    line = (f"{iteration}\t{commit}\t{metric:.1f}\t{delta:+.1f}\t"
            f"{trigger_f1:.2f}\t{tokens}\t{guard}\t{status}\t"
            f"{layer}\t{description}\n")
    with open(tsv_path, "a") as f:
        f.write(line)

    # experiments.jsonl
    if experiment:
        jsonl_path = evolve_dir / "experiments.jsonl"
        with open(jsonl_path, "a") as f:
            f.write(json.dumps(experiment, ensure_ascii=False) + "\n")

    # Execution traces (Meta-Harness: full output per case for diagnosis)
    if traces:
        trace_dir = evolve_dir / f"iteration-E{iteration}" / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        for case_id, trace_content in traces.items():
            trace_file = trace_dir / f"case_{case_id}.md"
            trace_file.write_text(str(trace_content))


# ─────────────────────────────────────────────
# Phase 8: Loop Control (fully automated)
# ─────────────────────────────────────────────

def phase_8_loop_control(workspace: Path, max_iterations: int,
                         consecutive_discard_limit: int = 5,
                         layer_promotion_k: int = 5) -> dict:
    """Determine whether to continue, promote layer, or stop.

    Returns: {"continue", "reason", "promote_layer", "next_layer"}
    """

    rows = parse_results_tsv(workspace)
    n = len(rows)

    if n >= max_iterations:
        return {"continue": False, "reason": f"max_iterations ({max_iterations}) reached",
                "promote_layer": False, "next_layer": None}

    if not rows:
        return {"continue": True, "reason": "no iterations yet",
                "promote_layer": False, "next_layer": None}

    # Check consecutive discards in current layer
    current_layer = rows[-1].get("layer", "body")
    layer_rows = [r for r in rows if r.get("layer") == current_layer]
    recent_statuses = [r.get("status", "") for r in layer_rows[-layer_promotion_k:]]

    if (len(recent_statuses) >= layer_promotion_k and
            all(s in ("discard", "crash", "revert") for s in recent_statuses)):
        # Layer promotion
        layer_order = ["description", "body", "script"]
        try:
            idx = layer_order.index(current_layer)
            if idx < len(layer_order) - 1:
                next_layer = layer_order[idx + 1]
                return {"continue": True, "reason": f"promoting from {current_layer} to {next_layer}",
                        "promote_layer": True, "next_layer": next_layer}
            else:
                return {"continue": False, "reason": "all layers exhausted",
                        "promote_layer": False, "next_layer": None}
        except ValueError:
            pass

    # Check overall consecutive discards
    all_statuses = [r.get("status", "") for r in rows[-consecutive_discard_limit:]]
    if (len(all_statuses) >= consecutive_discard_limit and
            all(s in ("discard", "crash", "revert") for s in all_statuses)):
        return {"continue": True, "reason": "STUCK — switch to radical strategy",
                "promote_layer": False, "next_layer": None}

    return {"continue": True, "reason": "normal",
            "promote_layer": False, "next_layer": None}


# ─────────────────────────────────────────────
# Git helpers
# ─────────────────────────────────────────────

def git_revert_last(skill_path: Path) -> dict:
    """Revert the last commit (for discard/revert actions)."""
    try:
        result = subprocess.run(
            ["git", "revert", "HEAD", "--no-edit"],
            cwd=str(skill_path), capture_output=True, text=True, timeout=10,
        )
        return {"success": result.returncode == 0, "output": result.stdout + result.stderr}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "output": str(e)}


def save_best_version(skill_path: Path, workspace: Path, iteration: int) -> str:
    """Copy current skill to best_versions/."""
    import shutil
    dest = workspace / "evolve" / "best_versions" / f"iteration-{iteration}"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(skill_path, dest, ignore=shutil.ignore_patterns('.git'))
    return str(dest)


# ─────────────────────────────────────────────
# Claude -p integration (Phase 2+3 and L2 eval)
# ─────────────────────────────────────────────

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

    # Meta-Harness: include trace evidence for active diagnosis
    trace_context = ""
    recent_traces = review.get("recent_traces", {})
    if recent_traces:
        trace_lines = []
        for name, content in list(recent_traces.items())[:5]:
            trace_lines.append(f"--- {name} ---\n{content}")
        trace_context = "\n".join(trace_lines)

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

{"## Execution Traces (from most recent eval)" + chr(10) + trace_context if trace_context else ""}

{"## Past Diagnoses (insights from prior iterations)" + chr(10) + diagnosis_context if diagnosis_context else ""}

MANDATORY PROTOCOL (Meta-Harness active diagnosis):
1. If traces are provided, READ THEM FIRST — identify the specific assertion
   that failed and WHY it failed based on the trace evidence
2. State your diagnosis: "Case X failed because [specific reason from trace]"
3. Then propose ONE atomic change that directly addresses the diagnosed cause
4. Do NOT guess — if no trace evidence points to a clear cause, say so

Read the SKILL.md at {skill_path / 'SKILL.md'}, then make your change.

After making the change, output EXACTLY this JSON on the last line:
{{"changed": true, "description": "one sentence describing what you changed", "mutation_type": "body_rewrite", "diagnosis": "Case X failed because Y, so I changed Z"}}

If you find nothing to improve, output:
{{"changed": false, "description": "no improvement found", "mutation_type": "none", "diagnosis": ""}}
"""

    response = _call_claude(prompt, model=model, timeout=180)

    # Parse the JSON from the last line
    for line in reversed(response.split("\n")):
        line = line.strip()
        if line.startswith("{") and "changed" in line:
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass

    return {"changed": False, "description": "could not parse claude response",
            "mutation_type": "none"}


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
# Full auto loop
# ─────────────────────────────────────────────

def run_evolve_loop(skill_path: Path, gt_path: Path, workspace: Path,
                    max_iterations: int = 20, model: str | None = None,
                    verbose: bool = True,
                    evaluator: Evaluator | None = None) -> dict:
    """Run the complete 8-phase evolve loop.

    This is the REAL auto loop. Phase 2+3 use claude -p for LLM reasoning.
    Evaluation uses the pluggable Evaluator interface.

    Args:
        evaluator: Pluggable evaluator instance. If None, auto-detects from
                   evolve_plan.md config or defaults to CreatorEvaluator.
    """
    # Initialize evaluator
    if evaluator is None:
        plan_path = workspace / "evolve" / "evolve_plan.md"
        eval_config = parse_evaluator_from_plan(plan_path)
        if model:
            eval_config["model"] = model
        evaluator = get_evaluator(eval_config)

    def log(msg):
        if verbose:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] {msg}", file=sys.stderr, flush=True)

    log("=" * 60)
    log("EVOLVE LOOP START")
    log(f"Skill: {skill_path}")
    log(f"GT: {gt_path}")
    log(f"Max iterations: {max_iterations}")
    log(f"Evaluator: {evaluator.info()}")
    log("=" * 60)

    # Phase 0: Setup
    log("Phase 0: Setup")
    setup = phase_0_setup(skill_path, gt_path, workspace)
    evolve_dir = Path(setup["evolve_dir"])

    l1 = evaluator.quick_gate(skill_path, gt_path)
    if not l1["pass"]:
        log(f"ABORT: L1 gate failed — {l1['errors']}")
        return {"success": False, "error": "L1 gate failed"}

    # Baseline eval
    log("Phase 0: Baseline eval")
    baseline = evaluator.full_eval(skill_path, gt_path)
    baseline_rate = baseline["pass_rate"]
    log(f"Baseline: {baseline['total_passed']}/{baseline['total_assertions']} = {baseline_rate:.0%}")

    phase_7_log(workspace, 0, "baseline", baseline_rate * 100, 0.0,
                1.0, 0, "pass", "baseline", "-", "initial baseline")
    save_best_version(skill_path, workspace, 0)

    best_rate = baseline_rate
    current_layer = "body"

    for iteration in range(1, max_iterations + 1):
        log("")
        log(f"{'=' * 40}")
        log(f"ITERATION {iteration}/{max_iterations}")
        log(f"{'=' * 40}")
        t0 = time.time()

        # Phase 1: Review
        log("Phase 1: Review")
        review = phase_1_review(workspace)
        log(f"  {review['iterations']} iters, {review['keeps']} keeps, stuck={review['stuck']}")

        # Phase 2+3: Ideate and Modify (via claude -p)
        log("Phase 2+3: Ideate and Modify (calling claude -p)")
        result_23 = phase_2_3_ideate_and_modify(
            skill_path, workspace, review, gt_path, current_layer, model)
        log(f"  Result: changed={result_23['changed']}, {result_23['description']}")

        if not result_23["changed"]:
            log("  No changes — stopping")
            phase_7_log(workspace, iteration, "-", best_rate * 100, 0.0,
                        1.0, 0, "pass", "exhausted", current_layer, "no improvement found")
            break

        # Phase 4: Commit
        log("Phase 4: Commit")
        commit = phase_4_commit(skill_path, current_layer, result_23["description"])
        if not commit["success"]:
            log(f"  Commit failed: {commit.get('error')}")
            continue
        log(f"  Committed: {commit['commit_hash']}")

        # Phase 5: Verify
        log("Phase 5: Verify")
        l1 = evaluator.quick_gate(skill_path, gt_path)
        log(f"  L1: {'PASS' if l1['pass'] else 'FAIL'}")
        if not l1["pass"]:
            git_revert_last(skill_path)
            phase_7_log(workspace, iteration, commit["commit_hash"], 0, -(best_rate*100),
                        1.0, 0, "fail", "discard", current_layer,
                        f"L1 fail: {result_23['description']}")
            continue

        # L2 eval (uses pluggable evaluator)
        log("  L2 eval...")
        new_eval = evaluator.full_eval(skill_path, gt_path)
        new_rate = new_eval["pass_rate"]
        delta = new_rate - best_rate
        log(f"  L2: {new_eval.get('total_passed', '?')}/{new_eval.get('total_assertions', '?')} = {new_rate:.0%} (delta: {delta:+.0%})")

        # Phase 6: Gate (with real metrics from evaluator)
        log("Phase 6: Gate")
        gate = phase_6_gate_decision(
            {"pass_rate": new_rate, "l1_pass": True, "trigger_f1": 1.0,
             "tokens_mean": new_eval.get("tokens", 0),
             "duration_mean": new_eval.get("duration", 0.0),
             "regression_pass": 1.0},
            {"pass_rate": best_rate, "trigger_f1": 1.0,
             "tokens_mean": baseline.get("tokens", 0),
             "duration_mean": baseline.get("duration", 0.0),
             "regression_pass": 1.0},
            {"min_delta": 0.01, "noise_threshold": 0.005}
        )
        decision = gate["decision"]
        log(f"  Decision: {decision}")

        if decision == "keep":
            best_rate = new_rate
            save_best_version(skill_path, workspace, iteration)
            log(f"  KEEP — new best: {best_rate:.0%}")
        else:
            git_revert_last(skill_path)
            log(f"  {decision.upper()} — reverted")

        # Phase 7: Log (with traces for Meta-Harness active diagnosis)
        elapsed = time.time() - t0
        phase_7_log(workspace, iteration, commit["commit_hash"],
                    new_rate * 100, delta * 100,
                    1.0, new_eval.get("tokens", 0), "pass", decision,
                    current_layer, result_23["description"],
                    experiment={
                        "iteration": iteration,
                        "mutation_type": result_23["mutation_type"],
                        "mutation_layer": current_layer,
                        "intent": result_23["description"],
                        "status": decision,
                        "elapsed_seconds": round(elapsed, 1),
                        "tokens": new_eval.get("tokens", 0),
                        "duration": new_eval.get("duration", 0.0),
                        "diagnosis": result_23.get("diagnosis", ""),
                    },
                    traces=new_eval.get("traces"))
        log(f"  Logged ({elapsed:.1f}s)")

        # Phase 8: Loop control
        ctrl = phase_8_loop_control(workspace, max_iterations)
        log(f"Phase 8: {ctrl['reason']}")
        if not ctrl["continue"]:
            break
        if ctrl.get("promote_layer"):
            current_layer = ctrl["next_layer"]
            log(f"  PROMOTE → {current_layer}")

    # Final
    log("")
    log("=" * 60)
    log("EVOLVE COMPLETE")
    log("=" * 60)

    holdout = evaluator.full_eval(skill_path, gt_path, split="holdout")
    final_rows = parse_results_tsv(workspace)
    final_summary = calculate_summary(final_rows)

    log(f"Baseline: {baseline_rate:.0%} → Best: {best_rate:.0%}")
    log(f"Keeps: {final_summary['keep_count']} | Discards: {final_summary['discard_count']}")
    log(f"Holdout: {holdout['pass_rate']:.0%}")

    cleanup_best_versions(workspace, keep_n=3)

    # Try to launch Creator's eval viewer if available
    viewer_launched = _try_launch_eval_viewer(workspace, skill_path)
    if viewer_launched:
        log("Eval viewer launched — open the URL above to review results")

    return {
        "baseline_rate": baseline_rate,
        "best_rate": best_rate,
        "holdout_rate": holdout["pass_rate"],
        "iterations": final_summary["total_iterations"],
        "keeps": final_summary["keep_count"],
        "discards": final_summary["discard_count"],
        "viewer_launched": viewer_launched,
    }


# ─────────────────────────────────────────────
# Eval Viewer Integration
# ─────────────────────────────────────────────

def _try_launch_eval_viewer(workspace: Path, skill_path: Path) -> bool:
    """Try to launch Creator's eval viewer (generate_review.py) if available.

    Generates a static HTML review of the evolution results.
    Returns True if viewer was launched successfully.
    """
    creator_path = find_creator_path()
    if not creator_path:
        return False

    viewer_script = creator_path / "eval-viewer" / "generate_review.py"
    if not viewer_script.exists():
        return False

    # Parse skill name for the viewer
    try:
        name, _, _ = parse_skill_md(skill_path)
    except (ValueError, FileNotFoundError):
        name = skill_path.name

    # Find the latest benchmark file
    evolve_dir = workspace / "evolve"
    benchmark_path = None
    for d in sorted(evolve_dir.iterdir(), reverse=True):
        if d.is_dir() and d.name.startswith("iteration-E"):
            bp = d / "benchmark.json"
            if bp.exists():
                benchmark_path = bp
                break

    try:
        cmd = [
            sys.executable, str(viewer_script),
            str(workspace),
            "--skill-name", name,
            "--static", str(workspace / "evolve" / "review.html"),
        ]
        if benchmark_path:
            cmd.extend(["--benchmark", str(benchmark_path)])

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print(f"Review saved: {workspace / 'evolve' / 'review.html'}",
                  file=sys.stderr)
            return True
    except (subprocess.TimeoutExpired, OSError):
        pass

    return False


# ─────────────────────────────────────────────
# Cleanup helpers
# ─────────────────────────────────────────────

def cleanup_git_history(skill_path: Path, workspace: Path) -> dict:
    """Squash all experiment/revert commits into one summary commit.

    Call this AFTER evolve completes. Squashes everything since the
    commit tagged 'evolve-start' (or the first experiment commit) into
    a single summary commit to prevent git bloat.
    """

    rows = parse_results_tsv(workspace)
    summary = calculate_summary(rows)

    # Find the first experiment commit
    try:
        log = subprocess.run(
            ["git", "log", "--oneline", "--all"],
            cwd=str(skill_path), capture_output=True, text=True, timeout=10,
        )
        lines = log.stdout.strip().split("\n")
        # Find last non-experiment commit
        base_hash = None
        for line in lines:
            if "experiment(" not in line and "Revert" not in line:
                base_hash = line.split()[0]
                break
    except (subprocess.TimeoutExpired, OSError, IndexError):
        return {"success": False, "error": "Cannot read git log"}

    if not base_hash:
        return {"success": False, "error": "No base commit found"}

    # Squash
    best = summary.get("best_metric", "?")
    baseline_row = rows[0] if rows else {}
    baseline_metric = baseline_row.get("metric", "?")
    keeps = summary.get("keep_count", 0)
    total = summary.get("total_iterations", 0)
    msg = (f"evolve: {baseline_metric}% → {best}%, "
           f"{keeps} keeps in {total} iterations")

    try:
        subprocess.run(["git", "reset", "--soft", base_hash],
                       cwd=str(skill_path), capture_output=True, timeout=10)
        subprocess.run(["git", "commit", "-m", msg],
                       cwd=str(skill_path), capture_output=True, timeout=10)
        return {"success": True, "message": msg, "squashed_to": base_hash}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": str(e)}


def cleanup_best_versions(workspace: Path, keep_n: int = 3) -> list[str]:
    """Remove old best_versions, keeping only the most recent N."""
    import shutil
    bv_dir = workspace / "evolve" / "best_versions"
    if not bv_dir.exists():
        return []
    dirs = sorted(bv_dir.iterdir(), key=lambda d: d.name)
    removed = []
    while len(dirs) > keep_n:
        old = dirs.pop(0)
        if old.is_dir():
            shutil.rmtree(old)
            removed.append(str(old))
    return removed


def cleanup_eval_outputs(workspace: Path, keep_recent: int = 5) -> list[str]:
    """Remove old iteration-EN/ dirs, keeping recent N and all 'keep' iterations."""
    import shutil

    evolve_dir = workspace / "evolve"
    rows = parse_results_tsv(workspace)

    # Find which iterations were 'keep'
    keep_iters = {r.get("iteration") for r in rows if r.get("status") == "keep"}

    # List all iteration-E* dirs
    iter_dirs = sorted(
        [d for d in evolve_dir.iterdir() if d.is_dir() and d.name.startswith("iteration-E")],
        key=lambda d: d.name,
    )

    # Determine which to keep
    recent_dirs = set(d.name for d in iter_dirs[-keep_recent:])
    keep_dir_names = set()
    for ki in keep_iters:
        keep_dir_names.add(f"iteration-E{ki}")

    removed = []
    for d in iter_dirs:
        if d.name not in recent_dirs and d.name not in keep_dir_names:
            shutil.rmtree(d)
            removed.append(str(d))
    return removed


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

    # Parse JSON from response
    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("{") and '"evals"' in line:
            try:
                data = json.loads(line)
                output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                return {"count": len(data.get("evals", []))}
            except json.JSONDecodeError:
                pass

    # Try to find JSON block in the full response
    json_match = re.search(r'\{[\s\S]*"evals"[\s\S]*\}', response)
    if json_match:
        try:
            data = json.loads(json_match.group())
            output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            return {"count": len(data.get("evals", []))}
        except json.JSONDecodeError:
            pass

    return None


# ─────────────────────────────────────────────
# Main (reference CLI)
# ─────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Evolve loop orchestrator")
    parser.add_argument("skill_path", type=Path, help="Path to target skill")
    parser.add_argument("--gt", type=Path, default=None, help="Path to GT JSON")
    parser.add_argument("--max-iterations", type=int, default=20)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--model", default=None, help="Model for LLM CLI")
    parser.add_argument("--evaluator", default=None,
                        choices=["local", "creator", "script", "pytest"],
                        help="Evaluator engine (default: auto-detect from evolve_plan.md)")
    parser.add_argument("--evaluator-script", default=None,
                        help="Path to eval script (for --evaluator script)")
    parser.add_argument("--evaluator-test-cmd", default=None,
                        help="Test command (for --evaluator pytest)")
    parser.add_argument("--run", action="store_true",
                        help="Run the full auto evolve loop")
    parser.add_argument("--info", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--cleanup-versions", action="store_true")
    parser.add_argument("--verbose", action="store_true", default=True)
    args = parser.parse_args()

    ws = args.workspace or find_workspace(args.skill_path)

    if args.info:
        evaluators_info = {k: v.__name__ for k, v in
                          __import__("evaluators").EVALUATOR_REGISTRY.items()}
        print(json.dumps({
            "phases": {
                "phase_0": "Setup (auto)", "phase_1": "Review (auto)",
                "phase_2_3": "Ideate+Modify (LLM)", "phase_4": "Commit (auto)",
                "phase_5": "Verify (pluggable evaluator)", "phase_6": "Gate (auto)",
                "phase_7": "Log (auto)", "phase_8": "Loop control (auto)",
            },
            "evaluators": evaluators_info,
        }, indent=2))
        return

    if args.cleanup:
        print(json.dumps({"cleaned": cleanup_eval_outputs(ws)}, indent=2))
        return

    if args.cleanup_versions:
        print(json.dumps({"cleaned": cleanup_best_versions(ws)}, indent=2))
        return

    if not args.gt:
        # Auto-discover GT data
        candidates = [
            ws / "evals" / "evals.json",
            args.skill_path / "evals.json",
            args.skill_path.parent / "evals.json",
        ]
        for candidate in candidates:
            if candidate.exists():
                args.gt = candidate
                print(f"Auto-discovered GT data: {candidate}", file=sys.stderr)
                break
        if not args.gt:
            # Auto-construct GT using LLM to analyze the skill
            gt_target = ws / "evals" / "evals.json"
            gt_target.parent.mkdir(parents=True, exist_ok=True)
            print("No GT data found. Auto-constructing from SKILL.md...",
                  file=sys.stderr)
            gt_result = auto_construct_gt(args.skill_path, gt_target,
                                          model=args.model)
            if gt_result:
                args.gt = gt_target
                print(f"Generated {gt_result['count']} test cases → {gt_target}",
                      file=sys.stderr)
            else:
                print("Error: GT auto-construction failed. Provide --gt manually.",
                      file=sys.stderr)
                sys.exit(1)

    # Build evaluator from CLI args or evolve_plan.md
    eval_config = {}
    if args.evaluator:
        eval_config["evaluator"] = args.evaluator
    if args.evaluator_script:
        eval_config["evaluator_script"] = args.evaluator_script
    if args.evaluator_test_cmd:
        eval_config["evaluator_test_cmd"] = args.evaluator_test_cmd
    if args.model:
        eval_config["model"] = args.model

    evaluator_instance = None
    if eval_config.get("evaluator"):
        evaluator_instance = get_evaluator(eval_config)

    if args.run:
        # THE REAL LOOP
        result = run_evolve_loop(
            args.skill_path, args.gt, ws,
            max_iterations=args.max_iterations,
            model=args.model, verbose=args.verbose,
            evaluator=evaluator_instance,
        )
        print(json.dumps(result, indent=2))
    else:
        # Setup only
        setup = phase_0_setup(args.skill_path, args.gt, ws)
        print(json.dumps(setup, indent=2))
        print("\nTo run the full loop, add --run:", file=sys.stderr)
        print(f"  python evolve_loop.py {args.skill_path} --gt {args.gt} --run",
              file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        sys.exit(130)
    except FileNotFoundError as e:
        print(f"Error: File not found — {e}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in GT data — {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Run with PYTHONTRACEBACK=1 for full traceback.", file=sys.stderr)
        if os.environ.get("PYTHONTRACEBACK"):
            raise
        sys.exit(1)
