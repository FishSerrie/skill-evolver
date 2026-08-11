"""How "good" is decided.

Single responsibility: turn a case plus a candidate's output into a
:class:`judgment.Judgment`. A grader classifies — it partitions
expectations into matched / partially matched / missed, and identifies
produced content nothing asked for. It does **not** compute scores.

Why that split is absolute
--------------------------
Arithmetic lives in :mod:`scoring`, and this module contains no division
at all — a property you can grep for, which is the point: three grader
families need the same formulas, so letting each compute its own would
produce three implementations that drift. More importantly, a model must
never be in a position to hand back a number. Asked to classify, a model
that inflates ``matched`` has to take those items from somewhere, and the
conservation equation in :mod:`scoring` then fails. Asked to score, the
same model just returns a larger float and nothing can tell.

Why a template method here, and not inheritance between graders
---------------------------------------------------------------
The three families share a skeleton exactly: get the sets, hand them to
scoring, assemble a Judgment, attach feedback. That shared skeleton is
what :class:`BaseGrader` owns, and a subclass supplies only the step that
genuinely differs.

They do **not** inherit from each other. Coverage-by-model is not a
special case of programmatic checking — their notions of "an expectation"
are unrelated — so making one the parent of another would break Liskov
substitution to save a few lines.

Capabilities that are not the grader's job are injected rather than
inherited: a model-backed grader receives a judge object, so the same
grader can run against any backend and can be tested with a stub that
makes no network call.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import scoring
from json_extract import extract_json_object
from judgment import Judgment
from scoring import ConservationError, Outcome

__all__ = [
    "BaseGrader",
    "ProgrammaticGrader",
    "PointCoverageGrader",
    "RubricGrader",
    "MalformedExpectation",
    "ProtectionUnavailable",
    "CHECKS",
    "register_check",
]


class ProtectionUnavailable(RuntimeError):
    """A safeguard the grader promised could not be applied to this case.

    Raised so the case ends up errored rather than graded. Grading anyway
    would be worse than not grading: with commit-first off, the same
    wrong-but-fluent candidate scores 1.0 where it should score 0.0, so a
    lapsed safeguard does not weaken the measurement — it inverts it. And it
    would do so silently, since nothing in a normal-looking result says the
    protection lapsed.

    Errored cases are excluded from the mean rather than counted as zero, so
    the honest report is "this case could not be measured" instead of a
    number produced under conditions the caller did not ask for.
    """


# ─────────────────────────────────────────────
# The template
# ─────────────────────────────────────────────

class BaseGrader(ABC):
    """Fixed skeleton for producing a judgment; subclasses classify only.

    Subclasses implement :meth:`_classify` and optionally
    :meth:`_describe`. Everything else — scoring, conservation checking,
    error handling, cost accounting — is identical across graders and
    therefore lives here exactly once.
    """

    #: Metric the gate ranks on. Overridable, because a grader whose
    #: notion of "extra" is meaningless has no useful precision and
    #: should rank on recall instead of an f1 that silently halves.
    primary_metric = "f1"

    def __init__(
        self,
        pass_threshold: float = 1.0,
        partial_weight: float = scoring.DEFAULT_PARTIAL_WEIGHT,
    ):
        """
        Args:
            pass_threshold: primary-metric value at which a case counts as
                satisfied. Defaults to 1.0 — "passed" should mean fully
                correct unless a caller deliberately relaxes it, since a
                lower default would quietly redefine success for every
                grader that forgot to set it.
            partial_weight: credit for a partially covered expectation,
                passed through to :mod:`scoring`.
        """
        if not 0.0 <= pass_threshold <= 1.0:
            raise ValueError(
                f"pass_threshold must be in [0, 1], got {pass_threshold}"
            )
        self.pass_threshold = pass_threshold
        self.partial_weight = partial_weight

    def grade(self, case: Mapping, output: str) -> Judgment:
        """Judge one candidate output against one case.

        Never raises. A grader that throws would abort the whole
        optimization run over a single malformed case, so failures are
        captured as an errored :class:`Judgment` instead — which the loop
        excludes from the mean rather than scoring as zero, because an
        evaluator fault is not evidence about the candidate.
        """
        case_id = str(case.get("id", "?"))
        try:
            outcome = self._classify(case, output)
            metrics = scoring.compute_prf(
                outcome, partial_weight=self.partial_weight
            )
        except ConservationError as exc:
            # Deliberately distinguished from other failures: this is the
            # signal that a classifier's numbers do not add up, which is
            # what catches a model inflating its own match count. Caught
            # around both calls because either the grader or scoring may
            # be the one to notice, and the diagnosis is the same.
            return Judgment.failed(
                case_id,
                error=f"conservation: {exc}",
                feedback=(
                    "The classification was internally inconsistent, so this "
                    "case was not scored. This indicates a problem with the "
                    "evaluation, not with the candidate."
                ),
                # The classifier was paid for its bad answer. Reported so a
                # run where every case failed does not show a zero bill.
                cost=self._cost(),
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            return Judgment.failed(
                case_id,
                error=f"{type(exc).__name__}: {exc}",
                feedback="The evaluation itself failed; this case was skipped.",
                cost=self._cost(),
            )

        score = metrics[self.primary_metric]
        return Judgment(
            case_id=case_id,
            metrics=metrics,
            primary=self.primary_metric,
            passed=score >= self.pass_threshold,
            feedback=self._describe(outcome),
            evidence=self._evidence(outcome),
            cost=self._cost(),
        )

    @abstractmethod
    def _classify(self, case: Mapping, output: str) -> Outcome:
        """Partition the case's expectations against ``output``.

        The one step that differs between graders. Must return sets, never
        a number.
        """

    def _describe(self, outcome: Outcome) -> str:
        """Natural-language diagnosis of what went wrong.

        The highest-leverage output of a grader: a score says a candidate
        got worse, feedback says why, and only the latter can be acted on
        by the step that proposes the next change. Research on
        reflection-based optimizers puts feedback quality ahead of search
        strategy in determining results.

        Deliberately quotes the actual items rather than counting them.
        "3 of 8 expectations missed" tells the next step nothing it can
        use; naming which three does.
        """
        parts = []
        if outcome.missed:
            parts.append(f"Missed: {_render(outcome.missed)}")
        if outcome.partial:
            parts.append(f"Only partially covered: {_render(outcome.partial)}")
        if outcome.extra:
            parts.append(
                f"Produced content nothing asked for: {_render(outcome.extra)}"
            )
        if not parts:
            return "All expectations were met and nothing extraneous was produced."
        return " ".join(parts)

    def _evidence(self, outcome: Outcome) -> dict:
        """Structured detail behind the score. Not itself scored."""
        return {
            "matched": list(outcome.matched),
            "partial": list(outcome.partial),
            "missed": list(outcome.missed),
            "extra": list(outcome.extra),
            "expected_total": outcome.expected_total,
            "produced_total": outcome.produced_total,
        }

    def _cost(self) -> dict:
        """Resources this grader consumed. Zero unless a subclass spends any."""
        return {}


class _JudgeBackedGrader(BaseGrader):
    """Common ground for graders that reach a model through a judge.

    Holds only what is genuinely identical: the injected judge, the call
    counter, and how cost is reported. Deliberately does not implement
    ``_classify`` — that is the whole difference between them, and offering
    a default would invite a subclass to inherit the wrong one.

    Sits between :class:`BaseGrader` and the concrete graders rather than
    one of them inheriting from the other: classifying point coverage and
    checking a list of rules are unrelated notions of "an expectation", so
    making one the parent of the other would break substitutability for the
    sake of a few lines.

    Two distinct channels to the model, because they are genuinely
    different questions:

    - :meth:`_ask_binary` for "does this hold, yes or no".
    - :meth:`_ask_text` when the answer *is* the output — a classification
      to be parsed, or an independently produced answer.

    Conflating them is not a style question. A binary-question method has to
    split the verdict off the end of the reply, so routing raw text through
    it silently destroys the last line — precisely where a model that
    followed instructions put its structured answer.

    Cost is reported **per case**, as the difference between the judge's
    counters before and after. The counters themselves are cumulative, so
    reporting their current value on every case makes the run's total the
    sum of a growing series — roughly (N+1)/2 times the real spend. The gate
    compares that total against a budget, so the inflation rejects
    candidates for a cost they never incurred.
    """

    #: Judge attributes to report, and the key each becomes in ``cost``.
    #: ``duration_ms`` rather than seconds because that is the unit
    #: ``judgment.aggregate`` sums and the gate reads; two units for one
    #: quantity means one of them silently reads as zero.
    _COST_SOURCES = (("total_tokens", "tokens", 1),
                     ("total_duration", "duration_ms", 1000))

    #: Set by subclasses that need raw replies, so the requirement is
    #: checked once at construction instead of failing mid-run.
    needs_raw_channel = False

    def __init__(self, judge, max_output_chars: int = 8000, **kwargs):
        """
        Args:
            judge: object exposing ``judge_with_reasoning(question,
                context) -> (bool, str)`` and, for graders that need raw
                replies, ``complete(prompt) -> str``. Injected rather than
                constructed here so the same grader works against any
                backend and can be tested with a stub — a grader that built
                its own client could not be tested without one.
            max_output_chars: cap on candidate text sent to the model.

        Raises:
            TypeError: when the grader needs a raw-text channel and the
                judge has none. Checked here rather than at the first call
                so that a wiring mistake is reported before any case is
                graded — as a mid-run failure it was indistinguishable from
                the model misbehaving, and it could not be told apart from
                an unrelated ``TypeError`` raised inside the judge.
        """
        super().__init__(**kwargs)
        if self.needs_raw_channel and not callable(
            getattr(judge, "complete", None)
        ):
            raise TypeError(
                f"{type(judge).__name__} has no complete(); "
                f"{type(self).__name__} needs the model's reply intact, and "
                f"the binary-question channel strips the last line"
            )
        self.judge = judge
        self.max_output_chars = max_output_chars
        self._calls = 0
        self._spent: dict[str, float] = {}

    def _ask_text(self, prompt: str) -> str:
        """Send ``prompt`` and return the reply intact.

        Uses the raw channel, whose presence was established at
        construction. The binary channel would drop the last line of every
        reply — where a model that followed instructions puts its answer.
        """
        with self._accounted():
            return self.judge.complete(prompt) or ""

    def _ask_binary(self, question: str) -> tuple[bool, str]:
        """Ask one yes/no question; return the verdict and its rationale."""
        with self._accounted():
            return self.judge.judge_with_reasoning(question, "")

    @contextmanager
    def _accounted(self):
        """Attribute one call's spend to the case being graded.

        Records the judge's counters before and after so that ``cost``
        reflects this case rather than the run so far. Wrapping the call
        keeps the arithmetic in one place — a counter incremented at each
        call site is a counter that will eventually be forgotten at one of
        them.
        """
        before = self._counters()
        self._calls += 1
        try:
            yield
        finally:
            after = self._counters()
            for key, value in after.items():
                self._spent[key] = self._spent.get(key, 0) + (
                    value - before.get(key, 0)
                )

    def _counters(self) -> dict[str, float]:
        """The judge's cumulative counters, in the units ``cost`` reports.

        Read by ``getattr`` rather than required: a stub judge in a test has
        no counters, and demanding them would make the grader untestable
        without reimplementing the real judge's bookkeeping.
        """
        counters: dict[str, float] = {}
        for attr, key, scale in self._COST_SOURCES:
            value = getattr(self.judge, attr, None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                counters[key] = value * scale
        return counters

    def _cost(self) -> dict:
        """This case's spend, then reset for the next one.

        Returns a delta rather than the judge's running total. Reporting the
        total on every case makes the run's sum a growing series — about
        (N+1)/2 times what was actually spent — and the gate compares that
        against a token budget, so the inflation rejects candidates for a
        cost they never incurred.

        Resetting here rather than in ``grade`` keeps the accounting local:
        the object that accumulates is the object that clears.
        """
        cost: dict[str, float] = {"classifier_calls": self._calls}
        cost.update(
            {key: round(value, 4) if isinstance(value, float) else value
             for key, value in self._spent.items()}
        )
        self._calls = 0
        self._spent = {}
        return cost


def _render(items: Sequence, limit: int = 8) -> str:
    """Compact, readable rendering of a bucket for feedback text.

    Truncated because feedback is fed back into a model's context: an
    unbounded list of every missed expectation would crowd out the
    instruction being optimized, which is the very failure mode
    (context collapse) the single-atomic-change discipline exists to
    avoid.
    """
    shown = [_render_one(item) for item in items[:limit]]
    text = "; ".join(shown)
    if len(items) > limit:
        text += f"; … and {len(items) - limit} more"
    return text


def _shorten(text: str, width: int = 160) -> str:
    """Collapse whitespace and cap length.

    The single place that decides how long a piece of feedback text may
    be. Both the item renderer and the assertion-reason renderer go
    through it, so there is one answer to "how much is too much" rather
    than two that drift.
    """
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= width else collapsed[:width] + "…"


def _render_one(item: Any, width: int = 120) -> str:
    if isinstance(item, Mapping):
        for key in ("text", "point", "value", "description", "name"):
            if key in item:
                item = item[key]
                break
        else:
            item = json.dumps(item, ensure_ascii=False, sort_keys=True)
    return _shorten(item, width)


# ─────────────────────────────────────────────
# Programmatic checks
# ─────────────────────────────────────────────
#
# A registry rather than a chain of ``if kind == ...`` branches inside the
# grader: adding a check type means registering a function, with no edit
# to the grader itself. Each check answers one question — does the output
# satisfy this expectation — and returns a verdict plus a short reason.
#
# Signature: (expectation: Mapping, output: str, case: Mapping)
#            -> tuple[bool, str]

CheckFn = Callable[[Mapping, str, Mapping], "tuple[bool, str]"]

CHECKS: dict[str, CheckFn] = {}


def register_check(kind: str) -> Callable[[CheckFn], CheckFn]:
    """Register a check under ``kind``. Refuses to silently replace one.

    Overwriting would let a stray definition change what an existing GT
    file means, with every score shifting and nothing reporting why.
    """
    def decorator(fn: CheckFn) -> CheckFn:
        if kind in CHECKS:
            raise ValueError(f"check {kind!r} is already registered")
        CHECKS[kind] = fn
        return fn
    return decorator


class MalformedExpectation(ValueError):
    """An expectation the grader cannot act on.
    Raised for a *dataset* problem rather than a candidate problem, so it
    surfaces as an errored case naming the offending expectation. The
    alternative — coercing whatever was written into a string and matching
    on that — produces a verdict that looks authoritative and means nothing:
    an empty needle makes ``contains`` pass for free while making
    ``not_contains`` impossible, and a dict silently becomes a
    ``"{'a': 1}"`` literal that no output will ever hold. Both score
    confidently, so the GT author gets no signal that the row is broken.
    """


def _text_value(expectation: Mapping, *, allow_empty: bool = False) -> str:
    """The expectation's ``value`` as text, or raise if it cannot be one.

    One definition for the four textual checks, so a malformed row is
    reported the same way whichever check reads it.

    ``bool`` is rejected alongside dicts and lists: ``True`` would become
    the literal ``"True"`` and quietly match any output containing that
    word, which is a coincidence rather than a check.
    """
    if "value" not in expectation:
        raise MalformedExpectation("expectation has no 'value'")
    value = expectation["value"]
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        # A number written unquoted is a plausible way to express "the
        # output should contain 42", so it is honoured.
        text = str(value)
    else:
        raise MalformedExpectation(
            f"'value' must be text or a number, got "
            f"{type(value).__name__}: {value!r}"
        )
    if not text.strip() and not allow_empty:
        raise MalformedExpectation("'value' is empty")
    return text


@register_check("contains")
def _check_contains(expectation: Mapping, output: str, case: Mapping):
    needle = _text_value(expectation)
    if expectation.get("case_sensitive", False):
        hit = needle in output
    else:
        hit = needle.casefold() in output.casefold()
    return hit, f"expected to contain {needle!r}"


@register_check("not_contains")
def _check_not_contains(expectation: Mapping, output: str, case: Mapping):
    needle = _text_value(expectation)
    if expectation.get("case_sensitive", False):
        hit = needle not in output
    else:
        hit = needle.casefold() not in output.casefold()
    return hit, f"expected NOT to contain {needle!r}"


@register_check("regex")
def _check_regex(expectation: Mapping, output: str, case: Mapping):
    pattern = _text_value(expectation)
    try:
        hit = re.search(pattern, output, re.MULTILINE) is not None
    except re.error as exc:
        # A malformed pattern is the GT author's mistake, not the
        # candidate's. Reporting it as a miss with the reason attached
        # beats raising, which would lose every other expectation in
        # this case.
        return False, f"invalid regex {pattern!r}: {exc}"
    return hit, f"expected to match /{pattern}/"


@register_check("exact")
def _check_exact(expectation: Mapping, output: str, case: Mapping):
    # An empty expected answer is meaningful here — "the output should be
    # blank" is a real requirement, unlike an empty substring.
    want = _text_value(expectation, allow_empty=True)
    if expectation.get("strip", True):
        hit = output.strip() == want.strip()
    else:
        hit = output == want
    return hit, "expected an exact match"


@register_check("json_schema")
def _check_json_schema(expectation: Mapping, output: str, case: Mapping):
    """Validate the output against a JSON schema.

    Reuses the engine's existing schema checker rather than adding a
    second one — and with it, the failure *path*, so a diagnosis can name
    the offending field instead of just declaring the shape wrong.
    """
    from trace_enrichment import basic_schema_check_with_path

    schema = expectation.get("value")
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except json.JSONDecodeError as exc:
            return False, f"invalid schema JSON: {exc}"
    if not isinstance(schema, Mapping):
        return False, "json_schema expectation has no schema object"

    payload = extract_json_object(output)
    if payload is None:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return False, "output is not valid JSON"

    ok, where = basic_schema_check_with_path(payload, dict(schema), "")
    return ok, "schema satisfied" if ok else f"schema violation at {where}"


@register_check("script")
def _check_script(expectation: Mapping, output: str, case: Mapping):
    """Delegate the verdict to an external script.

    The output is passed on stdin rather than as an argument: an argument
    would be truncated by the platform's limit on long candidates, and
    silently — the check would start failing for reasons unrelated to
    quality.

    Exit code 0 means satisfied. That convention is inherited from every
    other tool a user already knows, so a check script needs no special
    knowledge to write.
    """
    script = expectation.get("value")
    if not script:
        return False, "script expectation has no script path"
    path = Path(str(script))
    if not path.is_file():
        return False, f"script not found: {path}"

    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            input=output,
            capture_output=True,
            text=True,
            timeout=int(expectation.get("timeout", 30)),
        )
    except subprocess.TimeoutExpired:
        return False, f"script timed out: {path}"
    except OSError as exc:
        return False, f"script could not run: {exc}"

    detail = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode == 0, detail[:200] or f"exit {proc.returncode}"


class ProgrammaticGrader(BaseGrader):
    """Grades against expectations a program can decide on its own.

    Most reliable of the three families, because no model is involved in
    the verdict at all: the same output always produces the same
    classification. Use it wherever the expectation can be stated
    mechanically.

    ``extra`` is always empty here — a program checking a list of
    assertions has no way to notice content that nothing asked about — so
    precision would be a constant 1.0 and an f1 built on it would be
    misleading. Ranking is therefore on recall, which is the only
    dimension this grader actually measures.
    """

    primary_metric = "recall"

    def __init__(self, expectations_key: str = "expectations", **kwargs):
        """
        Args:
            expectations_key: which case field holds the list of
                expectations. Configurable because the field name belongs
                to whoever wrote the data, not to this module — hard-coding
                it would put a caller's schema inside the engine.
        """
        super().__init__(**kwargs)
        self.expectations_key = expectations_key

    def _classify(self, case: Mapping, output: str) -> Outcome:
        expectations = case.get(self.expectations_key) or []
        if not isinstance(expectations, Sequence) or isinstance(expectations, str):
            raise TypeError(
                f"case {case.get('id', '?')!r}: {self.expectations_key} must be "
                f"a list, got {type(expectations).__name__}"
            )

        matched: list = []
        missed: list = []
        for expectation in expectations:
            if not isinstance(expectation, Mapping):
                raise TypeError(
                    f"case {case.get('id', '?')!r}: each expectation must be a "
                    f"mapping, got {type(expectation).__name__}"
                )
            kind = str(expectation.get("type", "contains"))
            check = CHECKS.get(kind)
            if check is None:
                raise KeyError(
                    f"unknown expectation type {kind!r}; "
                    f"registered: {sorted(CHECKS)}"
                )
            ok, reason = check(expectation, output, case)
            record = {**expectation, "reason": reason}
            (matched if ok else missed).append(record)

        return Outcome(
            matched=matched,
            missed=missed,
            expected_total=len(expectations),
            # Every satisfied expectation is something the output
            # produced; nothing here can observe unrequested content.
            produced_total=len(matched),
        )

    def _describe(self, outcome: Outcome) -> str:
        if not outcome.missed:
            return f"All {len(outcome.matched)} expectations were satisfied."
        lines = [
            f"{len(outcome.missed)} of {outcome.expected_total} expectations "
            f"failed:"
        ]
        for item in outcome.missed[:8]:
            # The reason embeds the expectation's own value, which a GT
            # author may have made very long. Shortened here for the same
            # reason the item lists are: feedback re-enters a model's
            # context, and one verbose expectation must not crowd out the
            # instruction being optimized.
            lines.append(f"- {_shorten(item.get('reason') or _render_one(item))}")
        if len(outcome.missed) > 8:
            lines.append(f"- … and {len(outcome.missed) - 8} more")
        return "\n".join(lines)


# ─────────────────────────────────────────────
# Coverage of expected points, decided by a model
# ─────────────────────────────────────────────

_COVERAGE_PROMPT = """\
You are classifying which expected points a response covers. You do NOT \
assign scores — a program computes those from your classification.

Expected points (numbered):
{points}

Response to classify:
{output}

For each expected point, decide exactly one:
  - "matched"  — the response conveys this point correctly and completely
  - "partial"  — the response gestures at this point but is incomplete \
or imprecise
  - "missed"   — the response does not convey this point

Also list any substantive claim the response makes that corresponds to \
none of the expected points, as "extra".

Reply with ONE line of JSON and nothing else, on the last line:
{{"matched": [<point numbers>], "partial": [<point numbers>], \
"missed": [<point numbers>], "extra": ["<short quote>", ...]}}

Every point number from 1 to {total} must appear in exactly one of \
matched, partial, or missed.\
"""


class PointCoverageGrader(_JudgeBackedGrader):
    """Grades free-form text against expectations pre-split into points.

    Used where the answer cannot be matched mechanically but the expected
    content is enumerable — the model decides coverage, the program
    computes the score.

    The model is asked to *partition* the points, which is what makes
    over-reporting detectable: to inflate ``matched`` it must remove the
    same items from ``missed``, and the conservation equation in
    :mod:`scoring` then fails to balance. A model asked for a score
    instead could simply return a bigger number, and no amount of
    prompting would reveal it.
    """

    # The classification is structured output, so the raw channel is
    # required; asserted at construction rather than on first use.
    needs_raw_channel = True

    def __init__(self, judge, points_key: str = "points", **kwargs):
        """
        Args:
            judge: see :class:`_JudgeBackedGrader`.
            points_key: which case field holds the expected points.
        """
        super().__init__(judge, **kwargs)
        self.points_key = points_key

    def _classify(self, case: Mapping, output: str) -> Outcome:
        points = _as_points(case.get(self.points_key))
        if not points:
            raise ValueError(
                f"case {case.get('id', '?')!r} has no points under "
                f"{self.points_key!r}; PointCoverageGrader needs enumerated "
                f"expectations"
            )

        raw = self._ask(points, output)
        parsed = extract_json_object(raw, required_key="matched")
        if parsed is None:
            raise ValueError(
                "the classifier did not return the expected JSON object"
            )

        total = len(points)
        buckets = {
            name: _valid_indices(parsed.get(name), total)
            for name in ("matched", "partial", "missed")
        }

        # A point claimed twice is a contradiction, reported rather than
        # resolved — whether the two claims are in different buckets
        # ("matched and missed") or the same one (a repeated index, which
        # would inflate the count of that bucket).
        #
        # Deduplicating instead — keeping the first occurrence and
        # dropping the rest — was the earlier approach and it defeated the
        # whole safeguard: dropping the duplicate made the partition sum
        # correctly again, so a classifier could claim a point as both
        # matched and missed and be scored as if it had answered honestly.
        # There is no defensible way to pick which of two contradictory
        # claims was meant.
        placements: dict[int, str] = {}
        conflicts: list[str] = []
        for name in ("matched", "partial", "missed"):
            for index in buckets[name]:
                if index in placements:
                    where = placements[index]
                    conflicts.append(
                        f"point {index} claimed twice in {name!r}"
                        if where == name
                        else f"point {index} claimed as both {where!r} and {name!r}"
                    )
                else:
                    placements[index] = name
        if conflicts:
            raise ConservationError(
                "the classification contradicts itself: "
                + "; ".join(conflicts[:5])
            )

        extra = [str(x) for x in _as_list(parsed.get("extra")) if str(x).strip()]

        outcome = Outcome(
            matched=[points[i - 1] for i in buckets["matched"]],
            partial=[points[i - 1] for i in buckets["partial"]],
            missed=[points[i - 1] for i in buckets["missed"]],
            extra=extra,
            expected_total=total,
            produced_total=(
                len(buckets["matched"]) + len(buckets["partial"]) + len(extra)
            ),
        )
        # Verified here rather than left to compute_prf so the error names
        # the classifier as the cause; by the time scoring sees it, the
        # provenance is gone.
        scoring.check_conservation(outcome)
        return outcome

    def _ask(self, points: Sequence, output: str) -> str:
        """Send one classification request and return the raw reply.

        Goes through the raw-text channel, not the binary-question one. The
        latter treats the last line as a YES/NO verdict and strips it, which
        would discard the JSON of every model that followed the prompt's
        instruction to put it there — turning correct classifications into
        "the classifier did not return the expected JSON object" on every
        single case.
        """
        numbered = "\n".join(
            f"{i}. {_render_one(p, width=400)}" for i, p in enumerate(points, 1)
        )
        return self._ask_text(_COVERAGE_PROMPT.format(
            points=numbered,
            output=output[: self.max_output_chars],
            total=len(points),
        ))


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping):
        return [value]
    if isinstance(value, Sequence):
        return list(value)
    return [value]


def _as_points(value) -> list:
    """Normalise a case's points field into a list.

    Delegates the string case to :func:`datasets.parse_points`, which owns
    the question "how many expectations does this value hold". This function
    once answered it independently, and the two disagreed on six of nine
    inputs — two of them changing the count itself, which silently altered
    recall's denominator depending on which path a value took. That they
    agreed at all was decided by call order rather than by design.

    A real list still passes through here rather than round-tripping, since
    a case built in memory may legitimately hold structured points (a dict
    with a description, for example) that rendering to text would flatten.
    """
    if value is None:
        return []
    if isinstance(value, str):
        from datasets import parse_points  # local import: avoids a cycle

        return parse_points(value)
    return _as_list(value)


class InvalidClassification(ValueError):
    """The classifier named a point number that does not exist.

    Separate from a merely low score: a classifier that invents point
    numbers has not answered the question asked, so its judgement about
    the numbers it got right is not evidence either.
    """


def _valid_indices(value, total: int) -> list[int]:
    """The 1-based point numbers in ``value``, in order.

    Raises rather than discarding anything unusable, and that choice is
    load-bearing. The previous version dropped out-of-range entries and
    relied on the conservation check in :mod:`scoring` to notice the
    shortfall — its docstring said so explicitly. That reasoning is wrong,
    and measurably so: discarding only leaves the totals short when the
    discarded entries were *extra*. A classifier that reports every real
    point **and** an invented one loses only the invented one, so the
    remaining indices fill the partition exactly, the equation balances,
    and the case scores 1.0.

        total=1, matched=[1, 2]        -> kept [1]     -> balances -> 1.0
        total=2, matched=[1, 2, 3, 4]  -> kept [1, 2]  -> balances -> 1.0

    Both were verified against the real gate. Conservation is a floor, not
    a ceiling: it catches a partition that fails to account for every
    expectation, not one that accounts for more than exist.

    So an impossible index is treated as what it is — evidence the
    classifier was not reading the list it was given — and the case is
    reported as unmeasurable. That is a shape-level rule rather than a
    catalogue of ways to be wrong: any index outside ``1..total``, in any
    bucket, fails, without anyone having to anticipate 0-based confusion,
    off-by-one, or hallucinated counts individually.

    ``int()`` is deliberately not used for the conversion. It *truncates*,
    so ``1.5`` would silently become "point 1 was matched" — a claim the
    model never made.

    Raises:
        InvalidClassification: on any entry that is not an exact integer in
            ``1..total``.
    """
    kept: list[int] = []
    for item in _as_list(value):
        index = _as_exact_int(item)
        if index is None:
            raise InvalidClassification(
                f"{item!r} is not a point number; expected an integer "
                f"in 1..{total}"
            )
        if not 1 <= index <= total:
            raise InvalidClassification(
                f"point {index} does not exist; there are {total}"
            )
        kept.append(index)
    return kept



def _as_exact_int(item) -> int | None:
    """Return ``item`` as an int when it denotes one exactly, else ``None``.

    Accepts ints, and strings or floats whose value is integral — quoting a
    number is a routine serialisation difference, not a meaningless value.
    Rejects fractions, since there is no non-fabricating way to read ``1.5``
    as a point number, and rejects ``bool``: ``True`` would otherwise pass
    as point 1 through Python's numeric tower, turning a type error in the
    reply into a plausible-looking classification.
    """
    if isinstance(item, bool):
        return None
    if isinstance(item, int):
        return item
    if isinstance(item, float):
        return int(item) if item.is_integer() else None
    if isinstance(item, str):
        try:
            return int(item.strip())
        except ValueError:
            pass
        try:
            number = float(item.strip())
        except ValueError:
            return None
        return int(number) if number.is_integer() else None
    return None


# ─────────────────────────────────────────────
# Rules with no reference answer
# ─────────────────────────────────────────────

_SOLVE_PROMPT = """\
{instruction}

Task:
{task}

Answer the task directly and completely. Do not comment on the task or on \
how it should be answered — produce the answer itself.\
"""

_RUBRIC_PROMPT = """\
You are checking one specific requirement. Answer only about that \
requirement, and answer only YES or NO.

Requirement: {criterion}

{reference_block}Response to check:
{output}

Does the response satisfy the requirement? Reply with your reasoning in one \
or two short sentences, then YES or NO alone on the last line.\
"""

_REFERENCE_BLOCK = """\
For comparison, here is an independently produced answer to the same task. \
It is not authoritative and may itself be imperfect — use it only as a \
reference point for what the task called for:
{reference}

"""


class RubricGrader(_JudgeBackedGrader):
    """Grades against stated rules when there is no reference answer.

    Each rule is checked as its own YES/NO question, and the program counts
    the answers. That is deliberate on two counts: a binary question has
    lower variance than a rating, and a model that only ever answers YES or
    NO cannot hand back a score to be inflated.

    **What this grader is for.** Some tasks have no single right answer but
    do have stated requirements — "cites its source", "does not speculate
    beyond the material", "answers in the requested format". Those are
    checkable one at a time even though the whole answer is not.

    **Why commit-first exists.** A judge shown only a candidate rates how
    *plausible* it looks, not whether it is right, and that is exploitable:
    in the study this follows, self-play drove judge approval from 0.72 to
    0.94 while real accuracy stayed at 0.20. Stronger judges, judges from a
    different model family, and strict three-judge ensembles all failed to
    close the gap — the ensemble was worse. The one intervention that
    worked was making the judge answer the task itself *before* seeing the
    candidate: false acceptance fell from 0.719 to 0.012.

    So with ``commit_first=True`` the judge is asked to solve the task
    first, and only then to check the candidate with its own answer
    available for comparison. The reference is explicitly marked as
    non-authoritative, because the goal is to give the judge a concrete
    sense of what the task required, not to substitute one opinion for a
    ground truth.

    The isolation is structural, not requested: :func:`_solve` takes the
    task and nothing else. There is no parameter through which the
    candidate could reach it, so no prompt wording is relied on to keep the
    two steps apart.

    ``extra`` is always empty and ranking is on recall, for the same reason
    as the assertion-based grader: checking a list of rules cannot detect
    content that no rule asked about, so precision would be a constant.
    """

    primary_metric = "recall"

    def __init__(
        self,
        judge,
        rubric_key: str = "rubric",
        task_key: str = "input",
        commit_first: bool = True,
        instruction: str = "",
        **kwargs,
    ):
        """
        Args:
            judge: see :class:`_JudgeBackedGrader`.
            rubric_key: which case field holds the list of criteria.
            task_key: which case field holds the task the criteria are
                about. Needed for commit-first, which has to know what to
                answer.
            commit_first: solve before judging. Defaults to True because
                the unprotected mode is the one with a measured failure,
                and a safeguard that must be switched on is a safeguard
                that will be forgotten.
            instruction: optional framing prepended when solving, e.g.
                domain context the task assumes.

        Raises:
            TypeError: when ``commit_first`` is set and the judge has no raw
                channel. Checked here rather than per case because the
                alternative is worse than a crash: the protection would
                switch itself off silently, and this grader's whole claim to
                trustworthiness rests on it being active.
        """
        # Only demanded when solving is actually going to happen, so a
        # binary-only judge remains usable with the protection off.
        self.needs_raw_channel = bool(commit_first)
        super().__init__(judge, **kwargs)
        self.rubric_key = rubric_key
        self.task_key = task_key
        self.commit_first = commit_first
        self.instruction = instruction

    def _classify(self, case: Mapping, output: str) -> Outcome:
        criteria = _as_criteria(case.get(self.rubric_key))
        if not criteria:
            raise ValueError(
                f"case {case.get('id', '?')!r} has no criteria under "
                f"{self.rubric_key!r}; RubricGrader needs stated rules"
            )

        reference = ""
        if self.commit_first:
            # Only the task is passed. The candidate is not in scope here,
            # which is what makes the isolation structural.
            task = _text_of(case.get(self.task_key, ""))
            if not task:
                raise ProtectionUnavailable(
                    f"case {case.get('id', '?')!r} has no task under "
                    f"{self.task_key!r}, so the judge cannot answer "
                    f"independently and commit-first cannot apply"
                )
            reference, why = self._solve(task)
            if not reference:
                raise ProtectionUnavailable(
                    f"the judge produced no independent answer ({why}), so "
                    f"this case would have been graded without commit-first"
                )

        satisfied: list = []
        violated: list = []
        for criterion in criteria:
            verdict, reasoning = self._check(criterion, output, reference)
            record = {"criterion": criterion, "reasoning": reasoning}
            (satisfied if verdict else violated).append(record)

        return Outcome(
            matched=satisfied,
            missed=violated,
            expected_total=len(criteria),
            # A rule check cannot see content no rule asked about.
            produced_total=len(satisfied),
        )

    def _solve(self, task: str) -> tuple[str, str]:
        """Answer the task independently, before any candidate is seen.

        The signature is the safeguard: with only a task in scope, there is
        no path by which the candidate could influence this answer. Relying
        on prompt wording instead would leave the protection to whatever
        the model chose to ignore.

        Returns ``(answer, reason)``. On failure the answer is empty and the
        reason names what went wrong, so the caller's error message says
        whether the judge timed out or raised something unexpected. Without
        it, six different exceptions produced the same diagnosis, and a real
        bug inside the judge — an ``AttributeError``, say — read as
        "protection unavailable", sending the reader to check the judge's
        type rather than the bug.

        An earlier version degraded to grading without the reference, on the
        reasoning that a weaker measurement beats none. That was wrong, and
        measurably so: with the protection off, the same wrong-but-fluent
        candidate scores 1.0 instead of 0.0. A timeout would therefore not
        weaken the check but invert it — and silently, since nothing in the
        result said the protection had lapsed. Under rate limiting timeouts
        become common, so the failure would cluster exactly in the long
        unattended runs.

        The caller guarantees a non-empty task, so there is no empty-input
        branch here — an empty task is rejected before this is reached, as
        solving nothing would produce noise for a reference.
        """
        try:
            answer = self._ask_text(
                _SOLVE_PROMPT.format(instruction=self.instruction, task=task)
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            return "", f"{type(exc).__name__}: {exc}"
        answer = answer.strip()
        return answer, "" if answer else "the reply was empty"

    def _check(self, criterion: str, output: str, reference: str):
        """Ask one binary question about one criterion."""
        block = (
            _REFERENCE_BLOCK.format(reference=reference[: self.max_output_chars])
            if reference
            else ""
        )
        return self._ask_binary(_RUBRIC_PROMPT.format(
            criterion=criterion,
            reference_block=block,
            output=output[: self.max_output_chars],
        ))

    def _describe(self, outcome: Outcome) -> str:
        if not outcome.missed:
            return f"All {len(outcome.matched)} requirements were satisfied."
        lines = [
            f"{len(outcome.missed)} of {outcome.expected_total} requirements "
            f"were not satisfied:"
        ]
        for item in outcome.missed[:8]:
            criterion = _shorten(item.get("criterion", ""), 100)
            reasoning = _shorten(item.get("reasoning", ""), 140)
            lines.append(f"- {criterion}" + (f" — {reasoning}" if reasoning else ""))
        if len(outcome.missed) > 8:
            lines.append(f"- … and {len(outcome.missed) - 8} more")
        return "\n".join(lines)

    def _evidence(self, outcome: Outcome) -> dict:
        """The usual detail, plus whether commit-first actually applied.

        Recorded because a score obtained with the protection means
        something different from one obtained without it — the same
        wrong-but-fluent candidate scores 0.0 with and 1.0 without. A reader
        of the logs must be able to tell which regime produced a number,
        and without this field the two are indistinguishable.

        The value is always ``True`` when ``commit_first`` is set, since a
        case that could not solve independently is now errored rather than
        graded. It is written anyway: an assertion that the invariant held
        for this specific case is worth more in an audit than the reader's
        recollection of what the code promises.
        """
        evidence = super()._evidence(outcome)
        evidence["commit_first_applied"] = bool(self.commit_first)
        return evidence


def _as_criteria(value) -> list[str]:
    """Normalise a rubric field into a list of criterion strings.

    Shares :func:`_as_points`' handling of JSON-in-a-cell, then renders
    each entry to text — a criterion may arrive as a dict with a
    description, and the judge needs one sentence to ask about.
    """
    return [
        text for text in (_render_one(item, width=400) for item in _as_points(value))
        if text
    ]


def _text_of(value) -> str:
    """Render a case field to plain text for use in a prompt."""
    return _render_one(value, width=100000)
