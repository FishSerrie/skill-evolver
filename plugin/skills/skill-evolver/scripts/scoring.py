"""Turn judgment outcomes into numbers.

Single responsibility: given the sets a grader produced, compute metrics.
This module is the ONLY place in the engine where a score is calculated —
graders classify, this module scores. That split exists because all three
grader families need the same arithmetic; letting each compute its own
would mean three implementations drifting apart, and because an LLM must
never be in a position to hand back a number directly.

Pure functions only: no LLM calls, no file IO, no imports beyond stdlib.
That makes every branch here reachable from a unit test without spawning
a process, which matters because the credibility of the whole optimization
loop rests on these few formulas being right.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "Outcome",
    "ConservationError",
    "DEFAULT_PARTIAL_WEIGHT",
    "compute_prf",
    "check_conservation",
]

# A partially covered expectation scores less than a full one but more
# than a miss. Zero would be simpler, but it erases the gradient the
# optimizer steers by: a candidate that half-covers ten points would look
# identical to one that covers none, so the loop could not tell progress
# from stagnation. 0.5 is a default, not a law — callers override it when
# their domain treats partial credit differently.
DEFAULT_PARTIAL_WEIGHT = 0.5


class ConservationError(ValueError):
    """Raised when an outcome's set sizes do not account for everything.

    The grader is told to place every expectation somewhere. If the parts do
    not add up to the whole, the classification cannot be trusted — most
    importantly, an LLM classifier inflating ``matched`` has to take those
    items from somewhere, so the sum stops balancing.

    This is why the check exists as an arithmetic invariant rather than an
    instruction in a prompt: asking a model not to over-report is advice,
    whereas a conservation equation makes dropping an expectation
    structurally impossible to hide.

    **What it does not catch:** a classifier that places every expectation
    but places them *wrongly* — claiming four of four matched when only one
    was covered balances perfectly. No arithmetic can detect that, because
    nothing is missing from the accounting; only comparing against an
    independent opinion can, which is what ``graders.RubricGrader``'s
    commit-first mode is for. Conservation is a floor, not a ceiling.
    """


@dataclass(frozen=True)
class Outcome:
    """What a grader classified, before any arithmetic is applied.

    Deliberately holds counts and item lists rather than a score: the
    grader's job ends here.``matched``/``partial``/``missed`` partition
    the expected items; ``extra`` holds produced items that matched no
    expectation (they lower precision without affecting recall).

    Item lists are kept because the feedback string is built from them —
    knowing that expectation 3 was missed is what makes a diagnosis
    actionable, whereas knowing that recall was 0.67 is not.
    """

    matched: list = field(default_factory=list)
    partial: list = field(default_factory=list)
    missed: list = field(default_factory=list)
    extra: list = field(default_factory=list)
    # Total expectations the grader was given. Passed in rather than
    # derived so that conservation can be checked against the caller's
    # own count — deriving it from the parts would make the check
    # tautological and unable to catch anything.
    expected_total: int | None = None
    # Total items the candidate produced. Same reasoning.
    produced_total: int | None = None

    @property
    def expected_parts_total(self) -> int:
        return len(self.matched) + len(self.partial) + len(self.missed)


def check_conservation(outcome: Outcome) -> None:
    """Verify the outcome's set sizes account for everything. Raises on failure.

    Two arithmetic checks:

    1. The expected-side parts must sum to ``expected_total``. This is what
       catches a classifier that drops expectations from its partition
       instead of placing them.
    2. The produced side cannot exceed what was produced.

    ``expected_total``/``produced_total`` of ``None`` mean "the caller did
    not supply a reference count", and the corresponding check is skipped
    rather than assumed — silently substituting the derived total would
    turn a real invariant into a no-op.

    **Identity is the caller's business, not this module's.** An earlier
    version also rejected two buckets holding equal-looking items, compared
    by their string form. That was wrong twice over: a ground-truth file may
    legitimately list the same expectation twice, and a produced item may
    legitimately equal an expected one — both voided the whole case as
    "inconsistent" when nothing was. Whoever builds the ``Outcome`` knows
    whether its items are positions or values and is the only layer able to
    tell a real double-count from a coincidence; see
    ``graders.PointCoverageGrader``, which rejects a repeated point *index*.

    What this leaves is a purely arithmetic guarantee, which is the durable
    part: a classifier inflating ``matched`` has to take those items from
    somewhere, so the sum stops balancing. That is why the check lives here
    as an equation rather than as a request in a prompt.
    """
    if outcome.expected_total is not None:
        parts = outcome.expected_parts_total
        if parts != outcome.expected_total:
            raise ConservationError(
                f"expected-side sets do not balance: "
                f"matched={len(outcome.matched)} + partial={len(outcome.partial)} "
                f"+ missed={len(outcome.missed)} = {parts}, "
                f"but expected_total={outcome.expected_total}"
            )

    if outcome.produced_total is not None:
        # Produced items land either in a match bucket or in `extra`.
        # `partial` counts as produced too: something was output for it.
        produced_accounted = (
            len(outcome.matched) + len(outcome.partial) + len(outcome.extra)
        )
        if produced_accounted > outcome.produced_total:
            raise ConservationError(
                f"produced-side sets exceed what was produced: "
                f"matched+partial+extra={produced_accounted} > "
                f"produced_total={outcome.produced_total}"
            )


def compute_prf(
    outcome: Outcome,
    partial_weight: float = DEFAULT_PARTIAL_WEIGHT,
    verify: bool = True,
    ndigits: int = 4,
) -> dict[str, float]:
    """Compute precision / recall / f1 from a classified outcome.

    Returns each metric separately rather than a single blended number,
    because they diagnose different failures: low recall means
    expectations were missed, low precision means content was produced
    that nothing asked for. Collapsing them loses exactly the signal a
    diagnosis step needs to prescribe a fix.

    Args:
        outcome: the grader's classification.
        partial_weight: credit for a partially covered expectation.
        verify: run :func:`check_conservation` first. Defaults to True so
            that the safe behaviour is the one you get by forgetting the
            argument; callers who have already verified can skip it.
        ndigits: rounding, applied only at the end so intermediate
            division does not accumulate error.

    Raises:
        ConservationError: when ``verify`` is set and the sets do not
            partition correctly. Propagating rather than returning a zero
            score is intentional — a broken classification is not the
            same as a bad candidate, and scoring it as 0.0 would
            misattribute an evaluator fault to the thing being evaluated.
        ValueError: if ``partial_weight`` is outside [0, 1], which would
            let a metric exceed 1.0 or go negative and silently break
            every threshold comparison downstream.
    """
    if not 0.0 <= partial_weight <= 1.0:
        raise ValueError(
            f"partial_weight must be in [0, 1], got {partial_weight}"
        )

    if verify:
        check_conservation(outcome)

    credit = len(outcome.matched) + partial_weight * len(outcome.partial)

    expected_total = (
        outcome.expected_total
        if outcome.expected_total is not None
        else outcome.expected_parts_total
    )
    produced_total = (
        outcome.produced_total
        if outcome.produced_total is not None
        else len(outcome.matched) + len(outcome.partial) + len(outcome.extra)
    )

    # An empty denominator means the question "what fraction was right?"
    # has no answer.0.0 is returned rather than raising because an empty
    # expectation set or an empty response is a legitimate state of the
    # thing being measured, not a fault in the measurement.
    recall = credit / expected_total if expected_total > 0 else 0.0
    precision = credit / produced_total if produced_total > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    return {
        "precision": round(precision, ndigits),
        "recall": round(recall, ndigits),
        "f1": round(f1, ndigits),
    }
