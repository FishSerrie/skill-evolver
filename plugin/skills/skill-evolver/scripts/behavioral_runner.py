#!/usr/bin/env python3
"""Behavioral Runner — real skill execution for evaluation.

Phase A of the multi-agent evolution architecture plan (see the
skill-evolver architecture design produced 2026-07-09). Root problem
this fixes: ``LocalEvaluator`` (``evaluators.py``) always evaluates
assertions against ``_load_skill_corpus()`` — the concatenated static
text of SKILL.md + references/*.md + agents/*.md. Every assertion type
(contains / regex / path_hit / fact_coverage / ...) therefore measures
"does the documentation contain the right words", never "does the
skill actually behave correctly when run". This module supplies the
missing piece: a real transcript, produced by actually invoking an
isolated agent against the case prompt with the skill loaded.

Two runner paths, mirroring the existing conversation/CLI split in
``llm.py`` (see ``phase_2_3_ideate_and_modify`` vs the CLI ``claude -p``
fallback):

  * **Conversation mode** (default, used when the evolve loop is
    driven by Claude directly in a conversation): a Python script
    cannot call the Agent/Task tool itself. ``build_behavioral_task_spec``
    prepares the ``(prompt, opts)`` pair; the executing Claude must
    issue the actual Agent tool call and then normalize the result
    with ``build_transcript_from_text``.
  * **CLI ``--run`` mode**: ``run_case_behaviorally`` shells out via
    ``llm.py``'s existing ``LLM_BACKENDS`` registry (independent OS
    subprocess — stronger isolation than a Task sub-agent's context
    isolation, no shared memory at all).

Fidelity levels (how faithfully "the skill is loaded" is simulated):

  * ``assume_loaded`` (default, implemented here): the prompt points
    the agent directly at the skill's file path and asks it to read
    SKILL.md and respond as if the skill were loaded. Cheap, but does
    not exercise the skill's own trigger/description matching.
  * ``scratch_install``: place the candidate skill in a throwaway
    ``~/.claude/skills/`` directory so an agent decides FOR ITSELF
    whether to trigger it. Higher fidelity, higher cost. Not
    implemented in this phase — see the architecture plan's Phase A
    scope note. Callers get ``NotImplementedError`` with this pointer
    rather than a silently-wrong result.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def build_behavioral_prompt(skill_path: Path, case: dict) -> str:
    """Build the prompt text used to elicit real skill behavior for a case.

    Shared by both the conversation-mode Task spec and the CLI
    subprocess path so the two runners test the same thing.
    """
    prompt = case.get("prompt", "")
    return (
        f"A Claude Code skill is installed at {skill_path} — its "
        f"SKILL.md (and any references/*.md or agents/*.md files it "
        f"points you to) describes how to behave. Read SKILL.md first, "
        f"then respond to the following user request exactly as you "
        f"would with this skill loaded and its instructions in effect. "
        f"Do not mention that you are being evaluated or that this is a "
        f"test.\n\nUser request: {prompt}"
    )


def build_behavioral_task_spec(skill_path: Path, case: dict,
                               subagent_type: str = "general-purpose") -> dict:
    """Build an Agent/Task tool call spec for the conversation-mode path.

    IMPORTANT: this function does NOT invoke the Agent tool — a Python
    script has no way to do that. It only prepares the inputs. The
    Claude driving the evolve loop in conversation must:

      1. Read this dict.
      2. Issue the actual Agent tool call with
         ``prompt=spec["prompt"]``, ``description=spec["description"]``
         (and ``subagent_type`` if the harness supports it).
      3. Take the sub-agent's returned text and pass it to
         :func:`build_transcript_from_text` to get a case-JSON-ready
         ``transcript`` block.

    The sub-agent spawned this way has no access to the main
    conversation's history (the Agent tool's own isolation guarantee)
    — it only ever sees the prompt text built here. That is the
    isolation this spec relies on; this function does not add a
    second layer on top of it.
    """
    return {
        "case_id": case.get("id", "?"),
        "prompt": build_behavioral_prompt(skill_path, case),
        "subagent_type": subagent_type,
        "description": f"Run behavioral case {case.get('id', '?')} for {skill_path.name}",
        "fidelity": "assume_loaded",
        "isolation": "subagent_context",
    }


def build_transcript_from_text(output_text: str, *, runner_backend: str,
                               isolation: str, fidelity: str,
                               tokens: int = 0, duration_ms: int = 0,
                               tool_calls: list | None = None,
                               exit_status: str = "ok") -> dict:
    """Normalize a raw agent response into the case JSON ``transcript`` shape.

    See ``references/memory_schema.md`` for the field reference. Used
    by both runner paths so ``BehavioralEvaluator`` never has to know
    which path produced a given transcript.
    """
    return {
        "mode": "behavioral",
        "runner_backend": runner_backend,
        "isolation": isolation,
        "fidelity": fidelity,
        "output_text": output_text,
        "tool_calls": tool_calls or [],
        "tokens": tokens,
        "duration_ms": duration_ms,
        "exit_status": exit_status,
    }


def run_case_behaviorally(skill_path: Path, case: dict, *,
                          backend: str | None = None,
                          model: str | None = None,
                          timeout: int = 120,
                          fidelity: str = "assume_loaded") -> dict:
    """CLI-mode runner: spawn an isolated subprocess to actually run the case.

    Uses ``llm.py``'s existing ``_call_llm`` — an independent OS
    process, no shared memory with the caller. ``cwd=skill_path`` lets
    the subprocess-invoked Claude/codex/opencode CLI Read the skill's
    own files directly, mirroring how
    ``phase_2_3_ideate_and_modify`` already relies on Claude reading
    SKILL.md from a given path.

    Known limitation: ``_call_llm`` only returns the final text
    response — it does not surface intermediate tool calls or token
    usage from the subprocess. ``tool_calls`` is always ``[]`` and
    ``tokens`` is always ``0`` for this path (honest zero, not a
    fabricated estimate). Getting real tool-call/token capture would
    require switching to ``--output-format json`` and parsing usage,
    which is a larger change to ``_call_llm``'s contract shared by
    other callers (``phase_2_3_ideate_and_modify``,
    ``run_l2_eval_via_claude``, ``auto_construct_gt``) — left as
    follow-up work, not done here.
    """
    if fidelity != "assume_loaded":
        raise NotImplementedError(
            f"fidelity={fidelity!r} is not implemented. Only "
            f"'assume_loaded' (point the agent at the skill directly, "
            f"no scratch install) is supported by run_case_behaviorally "
            f"in this phase. 'scratch_install' (placing the skill in a "
            f"throwaway ~/.claude/skills/ dir so triggering itself is "
            f"tested) is future work — see the skill-evolver "
            f"architecture plan, Phase A scope note."
        )

    from llm import _call_llm, is_llm_error  # local import: keeps this module import-light

    prompt = build_behavioral_prompt(skill_path, case)
    t0 = time.time()
    output_text = _call_llm(prompt, model=model, timeout=timeout,
                            backend=backend, cwd=str(skill_path))
    duration_ms = int((time.time() - t0) * 1000)

    # Asked rather than pattern-matched here. This line used to test
    # `output_text.startswith("[ERROR:")` with its own literal and no
    # leading-whitespace tolerance, so `"\n[ERROR: claude timed out]"`
    # read as a successful run — a transport failure recorded as the
    # candidate's answer, which the gate then scores as a very bad
    # candidate rather than as nothing having been measured. It happened
    # not to fire today only because every `_call_llm` return path strips;
    # that made this function's correctness depend on another module's
    # implementation detail instead of on its contract.
    exit_status = "error" if is_llm_error(output_text) else "ok"


    return build_transcript_from_text(
        output_text,
        runner_backend=backend or "auto",
        isolation="subprocess",
        fidelity=fidelity,
        tokens=0,
        duration_ms=duration_ms,
        tool_calls=[],
        exit_status=exit_status,
    )


# ─────────────────────────────────────────────
# Rotation sampling — architecture plan Module A, "轮转采样的边界情况".
# Picks which GT cases get a real behavioral run this dev-eval round,
# since running every case through a full agent call every iteration
# is the most expensive line item in the whole plan (see
# docs/private/multi-agent-evolution-upgrade/architecture.md §2.5).
# ─────────────────────────────────────────────

def rotation_state_path(workspace: Path) -> Path:
    """Public — evaluator_backends.BehavioralEvaluator computes this from
    the workspace it infers (or is given) and passes the result into
    get_rotation_sample(); no other module should hardcode this path."""
    return workspace / "evolve" / "behavioral_rotation.json"


def _load_rotation_state(path: Path) -> dict:
    """Read the rotation cache. Any failure (missing file, corrupt
    JSON, unreadable) degrades to "start fresh" rather than raising —
    this file is a performance cache, not a correctness dependency
    (see architecture plan: losing it just means a few cases get
    resampled sooner than the rotation schedule would have picked)."""
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_rotation_state(path: Path, state: dict) -> None:
    """Best-effort write. Swallows OSError for the same reason
    _load_rotation_state swallows read failures — this is a cache."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def get_rotation_sample(cases: list[dict], sample_size: int,
                        rotation_state_path: Path, split: str = "dev") -> list[dict]:
    """Return the subset of ``cases`` to run behaviorally this round.

    Rules (architecture plan §2 Module A, "轮转采样的边界情况"):
      * ``len(cases) <= sample_size``: run all of them, every round —
        no rotation needed, no state file touched.
      * Otherwise: rotate through ``cases`` in the order given,
        ``sample_size`` at a time, advancing a per-``split`` offset
        persisted at ``rotation_state_path``. Wraps around when the
        window runs past the end of the list.
      * Missing/corrupt state file: treated as offset 0 (see
        ``_load_rotation_state``), not an error.
      * No file locking: the evolve loop is single-process sequential
        (Module A's assumption — Module C's parallel population mode
        keeps this true by writing shared state from the main process
        only after all slots finish, see architecture plan §2 Module C).
    """
    n = len(cases)
    if n == 0:
        return []
    if n <= sample_size:
        return list(cases)

    state = _load_rotation_state(rotation_state_path)
    split_state = state.get(split, {})
    offset = split_state.get("offset", 0) if isinstance(split_state, dict) else 0
    if not isinstance(offset, int) or offset < 0 or offset >= n:
        offset = 0

    end = offset + sample_size
    if end <= n:
        sample = cases[offset:end]
    else:
        sample = cases[offset:n] + cases[0:end - n]

    state[split] = {"offset": end % n}
    _save_rotation_state(rotation_state_path, state)

    return sample

