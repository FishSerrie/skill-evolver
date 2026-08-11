"""The contract between grading and the optimization loop.

Single responsibility: define what a completed judgment looks like. This
module holds no logic — it neither scores (that is ``scoring``) nor decides
(that is a grader). It exists so that the loop can consume any grader's
verdict without knowing which grader produced it, which is what lets a new
judgment strategy be added without touching the engine.

Stdlib only, no IO, no model calls. Importable from anywhere without
side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

__all__ = ["Judgment", "aggregate"]


@dataclass(frozen=True)
class Judgment:
    """One case's completed evaluation.

    Frozen because a judgment is a record of what was measured. If the
    loop could mutate a score after the fact, "why did this candidate
    pass?" would become unanswerable from the logs — and the gate's
    decisions are only auditable if the numbers behind them are stable.

    Attributes:
        case_id: identifies which case this judges.
        metrics: named metrics, each computed independently. Multiple
            entries rather than one number because different metrics
            diagnose different failures; see ``scoring.compute_prf``.
        primary: which key in ``metrics`` the gate ranks on. Stored on the
            judgment rather than configured in the gate so that a grader
            producing an unusual metric set still yields something
            comparable, and so the choice travels with the data it
            describes.
        passed: whether this case is considered satisfied. Kept separate
            from the metrics because "good enough" is a policy decision
            (a threshold) rather than a measurement, and the two must not
            be conflated.
        feedback: natural-language diagnosis of what went wrong. The
            highest-leverage field here: a score says a candidate is worse,
            feedback says why, and only the latter can be acted on by the
            step that proposes the next change.
        evidence: supporting detail — which expectations matched, which
            were missed, per-assertion results, health probes. Not scored;
            read by humans and by the diagnosis step.
        cost: resource usage, e.g. ``{"tokens": int, "duration_ms": int}``.
            Kept out of ``metrics`` because cost is gated against a budget,
            not maximized like quality.
        error: set when the evaluation itself failed (a malformed
            classification, a conservation violation, a transport error).
            An errored judgment is NOT a zero score: scoring an evaluator
            fault as 0.0 would blame the candidate for the harness. The
            loop must exclude these rather than average them in.
    """

    case_id: str
    metrics: Mapping[str, float] = field(default_factory=dict)
    primary: str = ""
    passed: bool = False
    feedback: str = ""
    evidence: Mapping = field(default_factory=dict)
    cost: Mapping = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self) -> None:
        # Freeze the mappings too. Without this, `frozen=True` only stops
        # rebinding the attribute while the dict it points at stays
        # mutable — so `judgment.metrics["f1"] = 1.0` would silently
        # succeed and defeat the immutability the audit trail relies on.
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
        object.__setattr__(self, "cost", MappingProxyType(dict(self.cost)))

        if self.error is None and self.metrics:
            # A primary key that does not exist would raise later, deep
            # inside the gate, where the cause is hard to see. Fail here
            # instead, where the offending grader is still on the stack.
            if not self.primary:
                raise ValueError(
                    f"case {self.case_id}: metrics present but no primary "
                    f"metric named (candidates: {sorted(self.metrics)})"
                )
            if self.primary not in self.metrics:
                raise ValueError(
                    f"case {self.case_id}: primary metric {self.primary!r} "
                    f"is not in metrics (have: {sorted(self.metrics)})"
                )

    @property
    def score(self) -> float:
        """The metric the gate ranks on.

        Returns 0.0 for an errored judgment so that accidental arithmetic
        cannot silently credit a failed evaluation — but callers should be
        filtering on ``error`` rather than relying on this.
        """
        if self.error is not None:
            return 0.0
        return self.metrics[self.primary]

    @classmethod
    def failed(cls, case_id: str, error: str, feedback: str = "",
               cost: Mapping | None = None) -> "Judgment":
        """Construct a judgment representing an evaluation that broke.

        Provided so every caller reports harness failures the same way,
        instead of each inventing its own convention (empty metrics? zero
        score? a sentinel case_id?) — inconsistency there is what makes
        aggregate statistics quietly wrong.

        ``cost`` is accepted because a case that failed still spent money. A
        classifier paid to return nonsense was still paid, so a run where
        every case broke used to report zero tokens — and the budget gate
        then compared nothing against the baseline and passed. Real spend
        with an invisible bill is worth reporting even when the measurement
        itself is void.
        """
        return cls(case_id=case_id, error=error,
                   feedback=feedback or f"evaluation failed: {error}",
                   cost=cost or {})


def aggregate(judgments: list[Judgment]) -> dict:
    """Roll per-case judgments up into run-level numbers.

    Errored judgments are counted and excluded, never averaged in. This is
    the aggregation-level consequence of the same rule ``Judgment.error``
    encodes: if three of ten cases failed to evaluate, the honest report is
    "seven cases scored, three errored", not a mean silently dragged toward
    zero by harness faults.

    Returns per-metric means over the successfully judged cases, plus
    counts and summed cost. An empty input yields zeros rather than raising
    — a run that judged nothing is a real state the loop must handle.
    """
    scored = [j for j in judgments if j.error is None]
    errored = [j for j in judgments if j.error is not None]

    metric_names: list[str] = []
    for j in scored:
        for name in j.metrics:
            if name not in metric_names:
                metric_names.append(name)

    means = {
        name: round(
            sum(j.metrics[name] for j in scored if name in j.metrics)
            / max(1, sum(1 for j in scored if name in j.metrics)),
            4,
        )
        for name in metric_names
    }

    return {
        "metrics": means,
        "total": len(judgments),
        "scored": len(scored),
        "errored": len(errored),
        "passed": sum(1 for j in scored if j.passed),
        "pass_rate": round(sum(1 for j in scored if j.passed) / len(scored), 4)
        if scored else 0.0,
        "error_case_ids": [j.case_id for j in errored],
        "cost": {
            "tokens": sum(int(j.cost.get("tokens", 0)) for j in judgments),
            "duration_ms": sum(int(j.cost.get("duration_ms", 0)) for j in judgments),
        },
    }
