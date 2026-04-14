#!/usr/bin/env python3
"""BinaryLLMJudge — atomic YES/NO LLM calls with reasoning capture.

Core principle: LLM only classifies, never scores. Each call asks
exactly one binary question and returns one verdict plus a 1-2
sentence rationale. Programs aggregate the binary results into
scores. This enforces deterministic aggregation — the same
classification always produces the same aggregate, with zero LLM
algebraic drift.

The rationale capture (``judge_with_reasoning``) is what makes the
per-case evaluation trace actually diagnose-able — it's the
Meta-Harness paper §3 "model outputs" trace component for any
semantic assertion. Without it, a failing path_hit or fact_coverage
check just tells the proposer "the LLM said no", not *why*.

This module depends only on:
  - ``llm._call_llm`` (lazy-imported to avoid a module-import cycle
    with ``evaluators`` → ``llm`` → ``evaluators``)
  - stdlib (os, subprocess, sys, time)

It has NO runtime dependency on ``evaluators.py``; extracting it
into its own file removes ~190 lines from evaluators.py without any
functional change. ``evaluators.py`` re-exports ``BinaryLLMJudge``
for back-compat so every existing ``from evaluators import
BinaryLLMJudge`` keeps working.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time


class BinaryLLMJudge:
    """Makes atomic binary (YES/NO) calls to an LLM.

    Core principle: LLM only classifies, never scores.
    Each call asks exactly one question with a YES or NO answer.
    Programs aggregate the binary results into scores.

    Uses the pluggable backend system from scripts/llm.py — supports
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
        """Fallback LLM caller when llm.py is not importable.
        Auto-detects available CLI (claude > codex > opencode)."""
        for cli in ["claude", "codex", "opencode"]:
            cmd = [cli]
            output_path = None
            prompt_input = None
            if cli == "claude":
                cmd.extend(["-p", prompt, "--output-format", "text"])
            elif cli == "codex":
                tmp = tempfile.NamedTemporaryFile(
                    prefix="skill-evolver-codex-",
                    suffix=".txt",
                    delete=False,
                )
                output_path = tmp.name
                tmp.close()
                cmd.extend(["exec", "--skip-git-repo-check",
                            "-o", output_path, "-"])
                prompt_input = prompt
            else:
                cmd.extend(["run", prompt])
            if model:
                cmd.extend(["--model", model])
            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=timeout, env=env, input=prompt_input)
                if result.returncode != 0:
                    continue
                if output_path and os.path.exists(output_path):
                    with open(output_path, "r", encoding="utf-8") as fh:
                        text = fh.read().strip()
                    if text:
                        return text
                return result.stdout.strip()
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                continue
            finally:
                if output_path:
                    try:
                        os.unlink(output_path)
                    except OSError:
                        pass
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
