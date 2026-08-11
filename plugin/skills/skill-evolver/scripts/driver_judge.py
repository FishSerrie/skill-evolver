"""A judge whose answers come from the agent driving the run.

Why this is not a backend
-------------------------
`BinaryLLMJudge` reaches a model by spawning a CLI or calling an HTTP
endpoint. Whoever installs this engine as a Skill has neither: they have
their own agent's model and nothing else. Adding another backend would not
help — the problem is not which transport, it is depending on one.

This class implements the same two-method interface the graders require,
but answers from a supplied mapping instead of from a model. The agent
driving the loop fills that mapping with its own completions, so the
grading logic — the conservation check, the partition arithmetic, the
commit-first ordering — runs unchanged with no transport at all.

How a round works
-----------------
Grading cannot be done in one pass, because a Python method cannot block
and wait for the driver to answer a question it has not been asked yet. So
the same prompts are rendered twice, and the first pass exists only to
collect them:

    1. `collect` mode — grade every case with a judge in this mode. Each
       question is recorded and answered with a placeholder. The scores
       from this pass are meaningless and must be discarded; the questions
       are the output.
    2. The driver answers each recorded question with its own model.
    3. `replay` mode — grade the same cases again with the answers supplied.
       These scores are the real ones.

Rendering twice rather than caching a plan keeps one definition of every
prompt: the graders build them, exactly as they do when a model is
reachable. A separate "render the questions" path would be a second
implementation free to drift from the one that scores.

What this cannot enforce
------------------------
`RubricGrader`'s commit-first protection depends on the judge answering
the task *before* seeing the candidate. In this file that ordering is
preserved — the solve question is recorded and answered before the check
questions are — but the isolation is no longer structural. When a model is
reachable, `_solve(self, task)` cannot see the candidate because it is not
in scope. Here the driver holds both.

So the driver must issue each question as an independent sub-agent call
with an empty context, and must not answer from its own knowledge of the
candidate. That is a discipline in the protocol, not a property of the
code, and `isolation` on the returned evidence records which regime was in
force so a log can be read honestly later.
"""

from __future__ import annotations

import hashlib
from typing import Mapping

__all__ = ["DriverJudge", "PendingAnswer", "question_key"]


class PendingAnswer(RuntimeError):
    """A question was asked that the driver has not answered.

    Raised in replay mode only. In collect mode a placeholder is returned
    instead, because the point of that pass is to reach every question —
    stopping at the first one would surface them a single call at a time.
    """


def question_key(kind: str, case_id: str, prompt: str, index: int = 0) -> str:
    """A stable identifier for one question.

    Keyed on a digest of the prompt as well as the case, so that a question
    whose text changed cannot be answered by a stale reply. Two rounds over
    the same candidate produce identical keys; a round over a rewritten
    candidate produces different ones, which is what stops the loop from
    scoring round N with round N-1's answers.
    """
    digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{case_id}:{index}:{digest}"


class DriverJudge:
    """Answers grader questions from a mapping the driver supplies.

    Interface-compatible with `BinaryLLMJudge` — `complete` and
    `judge_with_reasoning` — so every grader works with it unmodified.
    """

    def __init__(self, mode: str = "collect",
                 answers: Mapping[str, str] | None = None):
        """
        Args:
            mode: ``"collect"`` to record questions and return placeholders,
                ``"replay"`` to answer from ``answers``.
            answers: ``{question_key: reply_text}``, required in replay mode.

        Raises:
            ValueError: on an unknown mode. A typo would otherwise silently
                behave as one of them — and which one it happened to be
                decides whether a run produces real scores or placeholders.
        """
        if mode not in ("collect", "replay"):
            raise ValueError(
                f"mode must be 'collect' or 'replay', got {mode!r}")
        self.mode = mode
        self.answers = dict(answers or {})
        #: Questions seen this pass, in order, as ``{key: prompt}``.
        self.asked: dict[str, str] = {}
        self.missing: list[str] = []
        # Named to match what the graders read off a judge for cost
        # accounting. Zero because the driver's spend is not visible from
        # here; reporting a made-up number would be worse than reporting
        # none, since the budget gate compares these against a baseline.
        self.total_tokens = 0
        self.total_duration = 0.0
        self._case_id = "?"
        self._counts: dict[str, int] = {}

    # ── driver-facing ────────────────────────

    def for_case(self, case_id: str) -> "DriverJudge":
        """Tell the judge which case the next questions belong to.

        Returns self so it can be used inline. Without this every question
        would be keyed to ``"?"`` and cases would overwrite each other's
        answers — the same silent-loss shape as two cases sharing an id.
        """
        self._case_id = str(case_id)
        return self

    def pending(self) -> list[dict]:
        """The questions collected this pass, for the driver to answer.

        Each entry is ``{key, kind, case_id, prompt}``. Answer them and pass
        ``{key: reply}`` to a replay-mode judge.
        """
        return [
            {"key": key, "kind": key.split(":", 1)[0],
             "case_id": key.split(":")[1], "prompt": prompt}
            for key, prompt in self.asked.items()
        ]

    # ── grader-facing ────────────────────────

    def complete(self, prompt: str, **_kwargs) -> str:
        """Raw-channel reply. Used where the answer *is* the output."""
        return self._answer("text", prompt)

    def judge_with_reasoning(self, question: str, context: str = "",
                             **_kwargs) -> tuple[bool, str]:
        """Binary-channel reply: a verdict plus its rationale.

        The reply is read the same way `BinaryLLMJudge` reads a model's:
        the last line carries YES or NO and the lines above it are the
        reasoning. Keeping that convention means the driver answers these
        questions exactly as a model would, and the prompt the grader wrote
        already asks for that shape.
        """
        full = f"{question}\n\n{context}".strip() if context else question
        reply = self._answer("binary", full)
        return _read_verdict(reply)

    # ── internals ────────────────────────────

    def _answer(self, kind: str, prompt: str) -> str:
        index = self._counts.get(f"{kind}:{self._case_id}", 0)
        self._counts[f"{kind}:{self._case_id}"] = index + 1
        key = question_key(kind, self._case_id, prompt, index)
        self.asked[key] = prompt

        if self.mode == "collect":
            # A placeholder shaped like a valid reply, so that collecting
            # reaches every question instead of stopping at the first
            # parse failure. These scores are discarded.
            return _PLACEHOLDER[kind]

        if key not in self.answers:
            self.missing.append(key)
            raise PendingAnswer(
                f"no answer supplied for {key}. Collect the questions with a "
                f"collect-mode judge, answer them, then replay."
            )
        return self.answers[key]


#: Replies that parse cleanly but assert nothing, used during collection.
#: The binary one says NO rather than YES: if a collection pass were ever
#: mistaken for a real one, a floor of zero is a visible failure, whereas a
#: ceiling of one looks like success.
_PLACEHOLDER = {
    "text": '{"matched": [], "partial": [], "missed": [], "extra": []}',
    "binary": "collecting questions; not a real verdict\nNO",
}


def _read_verdict(reply: str) -> tuple[bool, str]:
    """Split a reply into (verdict, rationale).

    The verdict is on the last non-blank line; everything before it is the
    reasoning. Mirrors `BinaryLLMJudge.judge_with_reasoning` so that a
    driver-supplied reply and a model-supplied one are read identically —
    two readings of the same convention would eventually disagree, and the
    disagreement would look like a judging difference rather than a parsing
    one.
    """
    lines = [line for line in (reply or "").strip().split("\n") if line.strip()]
    if not lines:
        return False, "empty reply"
    verdict_line = lines[-1].strip().upper()
    rationale = "\n".join(lines[:-1]).strip() or lines[-1].strip()
    return verdict_line.startswith("YES"), rationale
