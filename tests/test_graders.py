"""Tests for graders — how "good" is decided.

The acceptance criterion for this step is that the conservation check
catches a classifier over-reporting its own match count, so those are
adversarial tests rather than happy-path ones: a stub judge returns
deliberately dishonest classifications and the grader must refuse to
score them.

No network calls. The model-backed grader takes its judge by injection,
which is exactly what makes that possible.
"""

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).parent.parent / "plugin" / "skills" / "skill-evolver" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from graders import (  # noqa: E402
    CHECKS,
    BaseGrader,
    PointCoverageGrader,
    ProgrammaticGrader,
    register_check,
)
from judgment import Judgment  # noqa: E402
from scoring import Outcome  # noqa: E402


class StubJudge:
    """Returns canned replies. Records what it was asked.

    Mimics BinaryLLMJudge on **both** channels, and — importantly — mimics
    its *behaviour*, not just its signatures. The real
    ``judge_with_reasoning`` splits the last line off as a YES/NO verdict
    and returns only what preceded it; a stub that returned the whole reply
    would hide any caller wrongly routing structured output through it.
    That is exactly the bug this class previously concealed: a model obeying
    the prompt's "JSON on the last line" had its answer thrown away on every
    single case, and the tests all passed.
    """

    def __init__(self, replies, total_tokens=0, total_duration=0.0):
        self.replies = list(replies) if isinstance(replies, list) else [replies]
        self.prompts = []
        self.total_tokens = total_tokens
        self.total_duration = total_duration

    def _next_reply(self):
        return self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]

    def complete(self, prompt):
        """The raw-text channel: reply returned intact."""
        self.prompts.append(prompt)
        return self._next_reply()

    def judge_with_reasoning(self, question, context):
        """The binary channel: last line consumed as the verdict.

        Reproduces the real implementation's line-stripping so that routing
        structured output through here fails in a test exactly as it would
        in production.
        """
        self.prompts.append(question)
        reply = self._next_reply()
        lines = [ln for ln in str(reply).split("\n") if ln.strip()]
        if not lines:
            return False, ""
        verdict = "NO" not in lines[-1].upper()
        reasoning = "\n".join(lines[:-1]).strip()
        if not reasoning and len(lines) == 1:
            reasoning = lines[0].strip()
        return verdict, reasoning


def coverage_reply(matched=(), partial=(), missed=(), extra=()):
    return json.dumps({
        "matched": list(matched),
        "partial": list(partial),
        "missed": list(missed),
        "extra": list(extra),
    })


# ─────────────────────────────────────────────
# The template
# ─────────────────────────────────────────────

class TemplateTests(unittest.TestCase):
    def test_base_grader_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            BaseGrader()

    def test_only_classify_is_abstract(self):
        """The skeleton is shared; only the differing step is required.

        If _describe were abstract too, every grader would have to restate
        a diagnosis format that is identical for most of them.
        """
        self.assertEqual(BaseGrader.__abstractmethods__, frozenset({"_classify"}))

    def test_graders_do_not_inherit_from_each_other(self):
        """Coverage-by-model is not a special case of programmatic checking.

        Making one the parent of the other would break Liskov substitution
        to save a few lines.
        """
        self.assertFalse(issubclass(PointCoverageGrader, ProgrammaticGrader))
        self.assertFalse(issubclass(ProgrammaticGrader, PointCoverageGrader))

    def test_module_performs_no_division(self):
        """I2, checked on the parse tree rather than by grepping text.

        Three graders each computing their own P/R/F1 would be three
        implementations free to drift apart, so the arithmetic lives in
        scoring alone. Asserted via AST because a textual search for "/"
        cannot tell a division from a path separator, a URL, or a regex —
        and a check that reports false positives gets deleted, which
        leaves the real invariant unguarded.
        """
        tree = ast.parse((SCRIPTS / "graders.py").read_text())
        divisions = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.BinOp)
            and isinstance(node.op, (ast.Div, ast.FloorDiv))
        ]
        self.assertEqual(
            [node.lineno for node in divisions], [],
            "graders.py must not compute scores; delegate to scoring",
        )

    def test_module_does_not_reimplement_metrics(self):
        """The other half of I2: no local P/R/F1 formulas under other names."""
        tree = ast.parse((SCRIPTS / "graders.py").read_text())
        defined = {
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in ("precision", "recall", "f1", "compute_prf", "score"):
            self.assertNotIn(name, defined)

    def test_pass_threshold_is_validated(self):
        for bad in (-0.1, 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ProgrammaticGrader(pass_threshold=bad)

    def test_grade_returns_a_judgment_never_raises(self):
        """A malformed case must not abort the whole run."""
        grader = ProgrammaticGrader()
        judgment = grader.grade({"id": "x", "expectations": "not-a-list"}, "out")
        self.assertIsInstance(judgment, Judgment)
        self.assertIsNotNone(judgment.error)

    def test_evaluation_failure_is_not_a_zero_score(self):
        """An errored judgment must be excluded, not averaged in as 0.

        Scoring a harness fault as zero blames the candidate for the
        harness.
        """
        grader = ProgrammaticGrader()
        judgment = grader.grade({"id": "x", "expectations": 42}, "out")
        self.assertIsNotNone(judgment.error)
        self.assertEqual(judgment.metrics, {})
        self.assertFalse(judgment.passed)


# ─────────────────────────────────────────────
# ProgrammaticGrader
# ─────────────────────────────────────────────

class ProgrammaticGraderTests(unittest.TestCase):
    def setUp(self):
        self.grader = ProgrammaticGrader()

    def grade(self, expectations, output, **kw):
        grader = ProgrammaticGrader(**kw) if kw else self.grader
        return grader.grade({"id": "c1", "expectations": expectations}, output)

    def test_all_satisfied_scores_one(self):
        j = self.grade(
            [{"type": "contains", "value": "alpha"},
             {"type": "contains", "value": "beta"}],
            "alpha and beta",
        )
        self.assertEqual(j.metrics["recall"], 1.0)
        self.assertTrue(j.passed)
        self.assertIsNone(j.error)

    def test_partial_satisfaction_scores_proportionally(self):
        j = self.grade(
            [{"type": "contains", "value": "alpha"},
             {"type": "contains", "value": "gamma"}],
            "alpha only",
        )
        self.assertEqual(j.metrics["recall"], 0.5)
        self.assertFalse(j.passed)

    def test_ranks_on_recall_not_f1(self):
        """This grader cannot observe unrequested content.

        Precision would be a constant 1.0 and an f1 built on it would
        halve a perfectly good recall for no reason.
        """
        self.assertEqual(ProgrammaticGrader.primary_metric, "recall")
        j = self.grade([{"type": "contains", "value": "alpha"}], "alpha")
        self.assertEqual(j.primary, "recall")
        self.assertEqual(j.score, 1.0)

    def test_contains_is_case_insensitive_by_default(self):
        j = self.grade([{"type": "contains", "value": "ALPHA"}], "alpha")
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_contains_can_be_case_sensitive(self):
        j = self.grade(
            [{"type": "contains", "value": "ALPHA", "case_sensitive": True}],
            "alpha",
        )
        self.assertEqual(j.metrics["recall"], 0.0)

    def test_not_contains(self):
        ok = self.grade([{"type": "not_contains", "value": "forbidden"}], "clean")
        bad = self.grade(
            [{"type": "not_contains", "value": "forbidden"}], "has forbidden word"
        )
        self.assertEqual(ok.metrics["recall"], 1.0)
        self.assertEqual(bad.metrics["recall"], 0.0)

    def test_not_contains_can_be_case_sensitive(self):
        j = self.grade(
            [{"type": "not_contains", "value": "FORBIDDEN",
              "case_sensitive": True}],
            "has forbidden word",
        )
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_regex(self):
        j = self.grade([{"type": "regex", "value": r"\d{3}-\d{4}"}], "call 555-1234")
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_invalid_regex_is_a_miss_with_a_reason_not_a_crash(self):
        """A malformed pattern is the GT author's mistake.

        Raising would discard every other expectation in the case.
        """
        j = self.grade(
            [{"type": "regex", "value": "["},
             {"type": "contains", "value": "ok"}],
            "ok",
        )
        self.assertIsNone(j.error)
        self.assertEqual(j.metrics["recall"], 0.5)
        self.assertIn("invalid regex", j.feedback)

    def test_exact_strips_by_default(self):
        j = self.grade([{"type": "exact", "value": "answer"}], "  answer\n")
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_exact_can_require_byte_equality(self):
        j = self.grade(
            [{"type": "exact", "value": "answer", "strip": False}], " answer"
        )
        self.assertEqual(j.metrics["recall"], 0.0)

    def test_default_type_is_contains(self):
        j = self.grade([{"value": "alpha"}], "alpha")
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_unknown_type_errors_and_names_the_valid_ones(self):
        j = self.grade([{"type": "telepathy", "value": "x"}], "out")
        self.assertIsNotNone(j.error)
        self.assertIn("telepathy", j.error)

    def test_non_mapping_expectation_errors(self):
        j = self.grade(["just a string"], "out")
        self.assertIsNotNone(j.error)

    def test_empty_expectations_scores_zero_without_erroring(self):
        """No expectations means the question has no answer, not a crash."""
        j = self.grade([], "anything")
        self.assertIsNone(j.error)
        self.assertEqual(j.metrics["recall"], 0.0)

    def test_expectations_key_is_configurable(self):
        """The field name belongs to whoever wrote the data (I3)."""
        grader = ProgrammaticGrader(expectations_key="assertions")
        j = grader.grade(
            {"id": "c1", "assertions": [{"type": "contains", "value": "a"}]}, "a"
        )
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_feedback_names_what_failed(self):
        """A score says worse; feedback says why. Only the latter is actionable."""
        j = self.grade(
            [{"type": "contains", "value": "must-have-this"}], "missing"
        )
        self.assertIn("must-have-this", j.feedback)

    def test_evidence_records_the_partition(self):
        j = self.grade(
            [{"type": "contains", "value": "a"}, {"type": "contains", "value": "z"}],
            "a",
        )
        self.assertEqual(len(j.evidence["matched"]), 1)
        self.assertEqual(len(j.evidence["missed"]), 1)
        self.assertEqual(j.evidence["expected_total"], 2)

    def test_threshold_can_be_relaxed(self):
        j = self.grade(
            [{"type": "contains", "value": "a"}, {"type": "contains", "value": "z"}],
            "a",
            pass_threshold=0.5,
        )
        self.assertTrue(j.passed)


class JsonSchemaCheckTests(unittest.TestCase):
    SCHEMA = {"type": "object", "required": ["name"],
              "properties": {"name": {"type": "string"}}}

    def grade(self, output, schema=None):
        return ProgrammaticGrader().grade(
            {"id": "c1", "expectations": [
                {"type": "json_schema", "value": schema or self.SCHEMA}
            ]},
            output,
        )

    def test_valid_payload_passes(self):
        self.assertEqual(self.grade('{"name": "x"}').metrics["recall"], 1.0)

    def test_schema_violation_names_the_field(self):
        j = self.grade('{"other": 1}')
        self.assertEqual(j.metrics["recall"], 0.0)
        self.assertIn("name", j.feedback)

    def test_non_json_output_is_a_miss(self):
        j = self.grade("this is prose")
        self.assertEqual(j.metrics["recall"], 0.0)
        self.assertIn("not valid JSON", j.feedback)

    def test_schema_may_be_a_json_string(self):
        """GT read from a spreadsheet arrives as text."""
        j = self.grade('{"name": "x"}', schema=json.dumps(self.SCHEMA))
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_malformed_schema_is_reported_not_raised(self):
        j = self.grade('{"name": "x"}', schema="{not json")
        self.assertIsNone(j.error)
        self.assertIn("invalid schema", j.feedback)

    def test_json_embedded_in_prose_is_found(self):
        """Reuses the engine's single JSON-extraction implementation."""
        j = self.grade('Here you go:\n{"name": "x"}')
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_schema_that_is_not_an_object_is_reported(self):
        j = self.grade('{"name": "x"}', schema="[]")
        self.assertIn("no schema object", j.feedback)


class ScriptCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def script(self, body: str) -> Path:
        p = self.tmp / "check.py"
        p.write_text(body)
        return p

    def grade(self, script_path, output, **extra):
        return ProgrammaticGrader().grade(
            {"id": "c1", "expectations": [
                {"type": "script", "value": str(script_path), **extra}
            ]},
            output,
        )

    def test_exit_zero_means_satisfied(self):
        s = self.script("import sys; sys.exit(0)")
        self.assertEqual(self.grade(s, "out").metrics["recall"], 1.0)

    def test_non_zero_exit_means_missed(self):
        s = self.script("import sys; print('nope'); sys.exit(1)")
        j = self.grade(s, "out")
        self.assertEqual(j.metrics["recall"], 0.0)
        self.assertIn("nope", j.feedback)

    def test_output_arrives_on_stdin_not_as_an_argument(self):
        """An argument would be silently truncated on a long candidate."""
        s = self.script(
            "import sys; sys.exit(0 if sys.stdin.read().strip() == 'expected' else 1)"
        )
        self.assertEqual(self.grade(s, "expected").metrics["recall"], 1.0)
        self.assertEqual(self.grade(s, "other").metrics["recall"], 0.0)

    def test_large_output_is_not_truncated(self):
        big = "x" * 200000
        s = self.script(
            "import sys; sys.exit(0 if len(sys.stdin.read()) == 200000 else 1)"
        )
        self.assertEqual(self.grade(s, big).metrics["recall"], 1.0)

    def test_missing_script_is_reported_not_raised(self):
        j = self.grade(self.tmp / "absent.py", "out")
        self.assertIsNone(j.error)
        self.assertIn("not found", j.feedback)

    def test_no_script_path_is_reported(self):
        j = ProgrammaticGrader().grade(
            {"id": "c1", "expectations": [{"type": "script"}]}, "out"
        )
        self.assertIn("no script path", j.feedback)

    def test_timeout_is_reported_not_hung(self):
        s = self.script("import time; time.sleep(5)")
        j = self.grade(s, "out", timeout=1)
        self.assertIn("timed out", j.feedback)

    def test_an_unlaunchable_script_is_reported(self):
        """An OS-level failure is still the harness's problem to report."""
        s = self.script("print('ok')")
        with mock.patch("graders.subprocess.run",
                        side_effect=OSError("exec format error")):
            j = self.grade(s, "out")
        self.assertIsNone(j.error)
        self.assertIn("could not run", j.feedback)


class CheckRegistryTests(unittest.TestCase):
    def test_registering_a_duplicate_is_refused(self):
        """A stray redefinition would change what existing GT files mean."""
        with self.assertRaises(ValueError):
            register_check("contains")(lambda e, o, c: (True, ""))

    def test_a_new_check_needs_no_edit_to_the_grader(self):
        """OCP, demonstrated rather than asserted."""
        @register_check("always_true_test_only")
        def _fn(expectation, output, case):
            return True, "always"

        try:
            j = ProgrammaticGrader().grade(
                {"id": "c1",
                 "expectations": [{"type": "always_true_test_only"}]},
                "anything",
            )
            self.assertEqual(j.metrics["recall"], 1.0)
        finally:
            CHECKS.pop("always_true_test_only", None)


# ─────────────────────────────────────────────
# PointCoverageGrader — the anti-gaming tests
# ─────────────────────────────────────────────

class PointCoverageHonestTests(unittest.TestCase):
    POINTS = ["alpha fact", "beta fact", "gamma fact"]

    def grade(self, reply, points=None, **kw):
        judge = StubJudge(reply)
        grader = PointCoverageGrader(judge, **kw)
        j = grader.grade({"id": "c1", "points": points or self.POINTS}, "response")
        return j, judge

    def test_full_coverage_scores_one(self):
        j, _ = self.grade(coverage_reply(matched=[1, 2, 3]))
        self.assertIsNone(j.error)
        self.assertEqual(j.metrics["recall"], 1.0)
        self.assertEqual(j.metrics["f1"], 1.0)

    def test_missed_points_lower_recall(self):
        j, _ = self.grade(coverage_reply(matched=[1], missed=[2, 3]))
        self.assertAlmostEqual(j.metrics["recall"], 0.3333, places=3)

    def test_partial_scores_between_matched_and_missed(self):
        """Partial credit keeps the gradient the optimizer steers by."""
        full, _ = self.grade(coverage_reply(matched=[1, 2, 3]))
        part, _ = self.grade(coverage_reply(matched=[1, 2], partial=[3]))
        none, _ = self.grade(coverage_reply(matched=[1, 2], missed=[3]))
        self.assertGreater(full.metrics["recall"], part.metrics["recall"])
        self.assertGreater(part.metrics["recall"], none.metrics["recall"])

    def test_partial_weight_is_configurable(self):
        strict, _ = self.grade(
            coverage_reply(matched=[1, 2], partial=[3]), partial_weight=0.0
        )
        lenient, _ = self.grade(
            coverage_reply(matched=[1, 2], partial=[3]), partial_weight=1.0
        )
        self.assertLess(strict.metrics["recall"], lenient.metrics["recall"])

    def test_extra_content_lowers_precision_without_touching_recall(self):
        """The two dimensions diagnose different failures."""
        clean, _ = self.grade(coverage_reply(matched=[1, 2, 3]))
        noisy, _ = self.grade(
            coverage_reply(matched=[1, 2, 3], extra=["unasked claim"])
        )
        self.assertEqual(clean.metrics["recall"], noisy.metrics["recall"])
        self.assertLess(noisy.metrics["precision"], clean.metrics["precision"])

    def test_ranks_on_f1_because_both_dimensions_are_observable(self):
        self.assertEqual(PointCoverageGrader.primary_metric, "f1")

    def test_points_may_arrive_as_a_json_string(self):
        """A spreadsheet cell holds text, not a list."""
        j, _ = self.grade(
            coverage_reply(matched=[1, 2]), points=json.dumps(["a", "b"])
        )
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_points_key_is_configurable(self):
        judge = StubJudge(coverage_reply(matched=[1]))
        grader = PointCoverageGrader(judge, points_key="gt_points")
        j = grader.grade({"id": "c1", "gt_points": ["only"]}, "out")
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_missing_points_is_an_error_not_a_zero(self):
        j, _ = self.grade(coverage_reply(matched=[1]), points=[])
        self.assertIsNotNone(j.error)

    def test_feedback_quotes_the_missed_points(self):
        j, _ = self.grade(coverage_reply(matched=[1], missed=[2, 3]))
        self.assertIn("beta fact", j.feedback)
        self.assertIn("gamma fact", j.feedback)

    def test_cost_records_the_call_count(self):
        j, _ = self.grade(coverage_reply(matched=[1, 2, 3]))
        self.assertEqual(j.cost["classifier_calls"], 1)

    def test_cost_reports_this_cases_spend_not_the_running_total(self):
        """The judge's counters are cumulative; cost must be a delta.

        Reporting the total on every case makes the run's sum a growing
        series — about (N+1)/2 times the real spend — and the gate compares
        that against a token budget, so the inflation rejects candidates for
        a cost they never incurred.
        """
        class CountingJudge:
            def __init__(self):
                self.total_tokens = 0
                self.total_duration = 0.0

            def complete(self, prompt):
                self.total_tokens += 100
                self.total_duration += 0.5
                return coverage_reply(matched=[1, 2, 3])

            def judge_with_reasoning(self, q, c):
                return True, "unused"

        grader = PointCoverageGrader(CountingJudge())
        case = {"id": "c1", "points": list(self.POINTS)}
        per_case = [grader.grade(case, "out").cost["tokens"] for _ in range(3)]
        self.assertEqual(per_case, [100, 100, 100])
        self.assertEqual(sum(per_case), 300, "must equal what was spent")

    def test_duration_is_reported_in_the_unit_the_aggregate_reads(self):
        """Two units for one quantity means one of them silently reads zero."""
        judge = StubJudge(coverage_reply(matched=[1, 2, 3]), total_duration=2.5)
        j = PointCoverageGrader(judge).grade(
            {"id": "c1", "points": self.POINTS}, "out"
        )
        self.assertIn("duration_ms", j.cost)
        self.assertNotIn("duration_s", j.cost)

    def test_the_aggregate_picks_up_the_reported_duration(self):
        """End to end: the grader's key must match what aggregate sums."""
        import judgment

        class TimedJudge:
            def __init__(self):
                self.total_duration = 0.0
                self.total_tokens = 0

            def complete(self, prompt):
                self.total_duration += 1.5
                self.total_tokens += 10
                return coverage_reply(matched=[1])

            def judge_with_reasoning(self, q, c):
                return True, "unused"

        grader = PointCoverageGrader(TimedJudge())
        judgments = [
            grader.grade({"id": f"c{i}", "points": ["a"]}, "out") for i in range(2)
        ]
        rollup = judgment.aggregate(judgments)
        self.assertEqual(rollup["cost"]["duration_ms"], 3000)
        self.assertEqual(rollup["cost"]["tokens"], 20)

    def test_prompt_asks_for_a_partition_not_a_score(self):
        """LLM classifies, program scores. Asserted on the actual prompt."""
        _, judge = self.grade(coverage_reply(matched=[1, 2, 3]))
        prompt = judge.prompts[0]
        self.assertIn("matched", prompt)
        self.assertIn("missed", prompt)
        self.assertIn("do NOT", prompt.replace("Do NOT", "do NOT"))
        for forbidden in ("score from 1", "rate ", "out of 10", "0-100"):
            self.assertNotIn(forbidden, prompt.lower())

    def test_prompt_numbers_every_point(self):
        _, judge = self.grade(coverage_reply(matched=[1, 2, 3]))
        for i, point in enumerate(self.POINTS, 1):
            self.assertIn(f"{i}. {point}", judge.prompts[0])

    def test_response_is_truncated_to_the_configured_cap(self):
        judge = StubJudge(coverage_reply(matched=[1]))
        PointCoverageGrader(judge, max_output_chars=10).grade(
            {"id": "c1", "points": ["a"]}, "x" * 500
        )
        self.assertNotIn("x" * 11, judge.prompts[0])


class PointCoverageGamingTests(unittest.TestCase):
    """The acceptance criterion: over-reporting must be caught.

    A classifier that inflates ``matched`` has to take those items from
    ``missed``, so the partition stops summing to the point count. These
    tests confirm the arithmetic actually rejects such a case rather than
    scoring it — which is the difference between a real safeguard and a
    request in a prompt that the model may ignore.
    """

    POINTS = ["alpha", "beta", "gamma", "delta"]

    def grade(self, reply, points=None):
        judge = StubJudge(reply)
        return PointCoverageGrader(judge).grade(
            {"id": "c1", "points": points or self.POINTS}, "response"
        )

    def test_dropping_points_from_the_partition_is_rejected(self):
        """Claiming 2 matched out of 4 while accounting for only 2."""
        j = self.grade(coverage_reply(matched=[1, 2]))
        self.assertIsNotNone(j.error)
        self.assertIn("conservation", j.error)

    def test_claiming_everything_matched_while_omitting_points_is_rejected(self):
        j = self.grade(coverage_reply(matched=[1]))
        self.assertIsNotNone(j.error)
        self.assertIn("conservation", j.error)

    def test_double_counting_a_point_is_rejected(self):
        """Matched and missed at once must be reported, not resolved.

        Deduplicating instead would make the partition sum correctly
        again, so a self-contradicting classification would score as if
        it had been honest. That was the first implementation and this
        test is what caught it.
        """
        j = self.grade(coverage_reply(matched=[1, 2, 3], missed=[3, 4]))
        self.assertIsNotNone(j.error)
        self.assertIn("conservation", j.error)
        self.assertIn("point 3", j.error)

    def test_a_repeated_index_within_one_bucket_is_rejected(self):
        """Same contradiction, one bucket: it would inflate that count."""
        j = self.grade(coverage_reply(matched=[1, 1, 2], missed=[3, 4]))
        self.assertIsNotNone(j.error)
        self.assertIn("conservation", j.error)

    def test_inventing_point_numbers_is_rejected(self):
        """Out-of-range indices are discarded, leaving the total short.

        Correcting them would fabricate a classification the model never
        made.
        """
        j = self.grade(coverage_reply(matched=[1, 2, 3, 99]))
        self.assertIsNotNone(j.error)

    def test_non_integer_indices_are_rejected(self):
        j = self.grade(coverage_reply(matched=[1, 2, "three", None], missed=[]))
        self.assertIsNotNone(j.error)

    def test_an_honest_full_partition_is_accepted(self):
        """The guard must not reject correct classifications."""
        j = self.grade(coverage_reply(matched=[1, 2], partial=[3], missed=[4]))
        self.assertIsNone(j.error)

    def test_conservation_failure_is_reported_as_evaluator_fault(self):
        """Not as a bad candidate: the feedback must say so.

        A zero here would teach the search that a fine candidate is bad.
        """
        j = self.grade(coverage_reply(matched=[1]))
        self.assertIn("evaluation", j.feedback.lower())
        self.assertEqual(j.metrics, {})

    def test_malformed_json_is_an_error_not_a_zero(self):
        j = self.grade("I think it covered most of them, roughly 80%")
        self.assertIsNotNone(j.error)
        self.assertEqual(j.metrics, {})

    def test_empty_reply_is_an_error(self):
        self.assertIsNotNone(self.grade("").error)

    def test_json_without_the_required_key_is_an_error(self):
        j = self.grade('{"verdict": "good", "score": 0.9}')
        self.assertIsNotNone(j.error)

    def test_truncated_json_is_an_error(self):
        self.assertIsNotNone(self.grade('{"matched": [1, 2').error)

    def test_a_score_handed_back_directly_is_ignored(self):
        """Even if the model volunteers a number, nothing reads it."""
        reply = json.dumps({
            "matched": [1, 2, 3, 4], "partial": [], "missed": [], "extra": [],
            "score": 0.1, "f1": 0.0,
        })
        j = self.grade(reply)
        self.assertEqual(j.metrics["recall"], 1.0)
        self.assertEqual(j.metrics["f1"], 1.0)

    def test_extra_cannot_inflate_recall(self):
        """Padding `extra` must never help; it can only cost precision."""
        j = self.grade(
            coverage_reply(matched=[1], missed=[2, 3, 4],
                           extra=["a", "b", "c", "d", "e"])
        )
        self.assertEqual(j.metrics["recall"], 0.25)
        self.assertLess(j.metrics["precision"], 0.25)


class FeedbackTruncationTests(unittest.TestCase):
    """Feedback is fed back into a model's context, so it must be bounded.

    An unbounded list of every missed expectation would crowd out the
    instruction being optimized — the context-collapse failure the
    single-atomic-change discipline exists to avoid.
    """

    def test_programmatic_feedback_is_truncated_with_a_count(self):
        expectations = [
            {"type": "contains", "value": f"missing-{i}"} for i in range(20)
        ]
        j = ProgrammaticGrader().grade(
            {"id": "c1", "expectations": expectations}, "nothing here"
        )
        self.assertIn("and 12 more", j.feedback)
        self.assertLess(len(j.feedback), 1200)

    def test_point_feedback_is_truncated_with_a_count(self):
        points = [f"point {i}" for i in range(1, 21)]
        judge = StubJudge(coverage_reply(matched=[1], missed=list(range(2, 21))))
        j = PointCoverageGrader(judge).grade({"id": "c1", "points": points}, "out")
        self.assertIn("and 11 more", j.feedback)

    def test_a_very_long_item_is_shortened(self):
        j = ProgrammaticGrader().grade(
            {"id": "c1",
             "expectations": [{"type": "contains", "value": "z" * 500}]},
            "nothing",
        )
        self.assertIn("…", j.feedback)
        self.assertLess(len(j.feedback), 500)

    def test_a_mapping_item_without_a_known_text_key_is_still_rendered(self):
        """Falls back to JSON rather than printing a dict repr."""
        judge = StubJudge(coverage_reply(matched=[], missed=[1]))
        j = PointCoverageGrader(judge).grade(
            {"id": "c1", "points": [{"unexpected_key": "content here"}]}, "out"
        )
        self.assertIn("content here", j.feedback)

    def test_a_dict_point_with_a_text_key_renders_that_key(self):
        judge = StubJudge(coverage_reply(matched=[], missed=[1]))
        j = PointCoverageGrader(judge).grade(
            {"id": "c1", "points": [{"text": "the actual point", "n": 1}]}, "out"
        )
        self.assertIn("the actual point", j.feedback)
        self.assertNotIn("unexpected", j.feedback)

    def test_no_failures_yields_a_positive_statement(self):
        judge = StubJudge(coverage_reply(matched=[1]))
        j = PointCoverageGrader(judge).grade({"id": "c1", "points": ["a"]}, "out")
        self.assertIn("All expectations were met", j.feedback)


class PointsNormalisationTests(unittest.TestCase):
    """Points arrive from spreadsheets, JSON, and hand-written dicts.

    Normalising in one place keeps every caller from repeating the parse
    and each picking a different fallback when it fails.
    """

    def grade(self, points, reply):
        return PointCoverageGrader(StubJudge(reply)).grade(
            {"id": "c1", "points": points}, "out"
        )

    def test_a_plain_list(self):
        self.assertIsNone(self.grade(["a", "b"], coverage_reply(matched=[1, 2])).error)

    def test_a_json_array_string(self):
        j = self.grade('["a", "b"]', coverage_reply(matched=[1, 2]))
        self.assertIsNone(j.error)

    def test_a_bare_string_is_one_point(self):
        """Not valid JSON, so it is the point itself rather than an error."""
        j = self.grade("just one point", coverage_reply(matched=[1]))
        self.assertIsNone(j.error)
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_a_json_string_that_decodes_to_a_scalar_is_one_point(self):
        j = self.grade("42", coverage_reply(matched=[1]))
        self.assertIsNone(j.error)

    def test_a_blank_string_is_no_points(self):
        self.assertIsNotNone(self.grade("   ", coverage_reply(matched=[1])).error)

    def test_none_is_no_points(self):
        self.assertIsNotNone(self.grade(None, coverage_reply(matched=[1])).error)

    def test_a_single_dict_is_one_point(self):
        j = self.grade({"text": "solo"}, coverage_reply(matched=[1]))
        self.assertIsNone(j.error)

    def test_a_tuple_is_accepted(self):
        j = self.grade(("a", "b"), coverage_reply(matched=[1, 2]))
        self.assertIsNone(j.error)

    def test_a_non_sequence_scalar_is_one_point(self):
        j = self.grade(7, coverage_reply(matched=[1]))
        self.assertIsNone(j.error)

    def test_extra_may_be_a_bare_string(self):
        """A model that writes one extra claim need not wrap it in a list."""
        reply = json.dumps({
            "matched": [1], "partial": [], "missed": [], "extra": "one claim",
        })
        j = self.grade(["a"], reply)
        self.assertIsNone(j.error)
        self.assertEqual(len(j.evidence["extra"]), 1)

    def test_blank_extras_are_dropped(self):
        reply = json.dumps({
            "matched": [1], "partial": [], "missed": [],
            "extra": ["", "   ", "real"],
        })
        j = self.grade(["a"], reply)
        self.assertEqual(j.evidence["extra"], ["real"])

    def test_missing_buckets_default_to_empty(self):
        """A reply naming only `matched` must not crash on the others."""
        j = self.grade(["a"], '{"matched": [1]}')
        self.assertIsNone(j.error)


class CostReportingTests(unittest.TestCase):
    def test_a_judge_without_counters_reports_only_call_count(self):
        class Bare:
            def complete(self, prompt):
                return coverage_reply(matched=[1])

            def judge_with_reasoning(self, q, c):
                return True, "unused"

        j = PointCoverageGrader(Bare()).grade({"id": "c1", "points": ["a"]}, "o")
        self.assertEqual(j.cost, {"classifier_calls": 1})

    def test_programmatic_grader_reports_no_cost(self):
        """No model involved, so nothing to account for."""
        j = ProgrammaticGrader().grade(
            {"id": "c1", "expectations": [{"value": "a"}]}, "a"
        )
        self.assertEqual(j.cost, {})


class RealJudgeChannelTests(unittest.TestCase):
    """Against the actual BinaryLLMJudge, not a stub.

    A stub can agree with a wrong assumption. These tests use the real judge
    with only its transport replaced, which is the smallest substitution that
    still costs nothing — and is what would have caught the channel mix-up
    immediately: the coverage prompt asks for JSON on the last line, and the
    binary-question method consumes the last line as its verdict.
    """

    def judge_returning(self, reply):
        from binary_judge import BinaryLLMJudge

        judge = BinaryLLMJudge()
        judge._call_llm = lambda prompt, model=None, timeout=120, backend=None: reply
        return judge

    def test_a_model_obeying_the_prompt_is_graded_correctly(self):
        """Reasoning, then JSON on the last line — exactly as instructed."""
        reply = (
            "The response covers all three points clearly.\n"
            '{"matched": [1, 2, 3], "partial": [], "missed": [], "extra": []}'
        )
        j = PointCoverageGrader(self.judge_returning(reply)).grade(
            {"id": "c1", "points": ["a", "b", "c"]}, "an answer"
        )
        self.assertIsNone(j.error, "the classification must survive the round trip")
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_json_only_with_no_preamble_also_works(self):
        reply = '{"matched": [1], "partial": [], "missed": [], "extra": []}'
        j = PointCoverageGrader(self.judge_returning(reply)).grade(
            {"id": "c1", "points": ["a"]}, "an answer"
        )
        self.assertIsNone(j.error)
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_the_binary_channel_still_strips_its_verdict_line(self):
        """The behaviour that made the raw channel necessary, pinned down."""
        judge = self.judge_returning("Because of X.\nYES")
        verdict, reasoning = judge.judge_with_reasoning("q?", "ctx")
        self.assertTrue(verdict)
        self.assertEqual(reasoning, "Because of X.")

    def test_the_raw_channel_returns_the_reply_intact(self):
        judge = self.judge_returning("line one\nline two")
        self.assertEqual(judge.complete("prompt"), "line one\nline two")

    def test_a_transport_failure_on_the_raw_channel_yields_empty_not_a_crash(self):
        from binary_judge import BinaryLLMJudge

        judge = BinaryLLMJudge()

        def boom(prompt, model=None, timeout=120, backend=None):
            raise RuntimeError("backend down")

        judge._call_llm = boom
        self.assertEqual(judge.complete("prompt"), "")

    def test_the_raw_channel_accounts_for_its_cost(self):
        judge = self.judge_returning("something")
        judge.complete("a" * 400)
        self.assertGreater(judge.total_tokens, 0)

    def test_a_judge_without_the_raw_channel_is_rejected_at_construction(self):
        """A wiring mistake must be reported before any case is graded.

        As a mid-run failure it was indistinguishable from the model
        misbehaving, and it could not be told apart from an unrelated
        TypeError raised inside the judge itself.
        """
        class BinaryOnly:
            def judge_with_reasoning(self, q, c):
                return True, "irrelevant"

        with self.assertRaises(TypeError) as ctx:
            PointCoverageGrader(BinaryOnly())
        self.assertIn("complete()", str(ctx.exception))
        self.assertIn("BinaryOnly", str(ctx.exception))

    def test_a_non_callable_complete_attribute_is_also_rejected(self):
        class Odd:
            complete = "not a method"

            def judge_with_reasoning(self, q, c):
                return True, "x"

        with self.assertRaises(TypeError):
            PointCoverageGrader(Odd())

    def test_the_rubric_grader_also_uses_the_raw_channel_to_solve(self):
        """Its independent answer is prose, so the last line matters.

        Routed through the binary channel, a one-line answer would survive by
        accident and a multi-line one would silently lose its final sentence.
        """
        from binary_judge import BinaryLLMJudge
        from graders import RubricGrader

        seen = []

        judge = BinaryLLMJudge()

        def transport(prompt, model=None, timeout=120, backend=None):
            seen.append(prompt)
            if "Answer the task directly" in prompt:
                return "First sentence.\nSecond sentence is the crucial one."
            return "Looks right.\nYES"

        judge._call_llm = transport
        RubricGrader(judge, commit_first=True).grade(
            {"id": "c1", "input": "what happens?", "rubric": ["States the outcome"]},
            "a candidate",
        )
        check_prompt = [p for p in seen if "Requirement:" in p][0]
        self.assertIn("Second sentence is the crucial one.", check_prompt)


class GtWithRepeatedItemsTests(unittest.TestCase):
    """Ground truth may legitimately repeat itself.

    An earlier conservation check compared items by their string form and
    rejected any two buckets holding equal-looking entries. That voided whole
    cases for reasons that were not the candidate's fault: a spreadsheet
    listing the same expectation twice, or a produced item that happens to
    equal an expected one.
    """

    def test_two_identical_points_are_graded_not_voided(self):
        judge = StubJudge(coverage_reply(matched=[1, 2]))
        j = PointCoverageGrader(judge).grade(
            {"id": "c1", "points": ["same point", "same point"]}, "x"
        )
        self.assertIsNone(j.error, j.error)
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_two_identical_assertions_are_graded_not_voided(self):
        j = ProgrammaticGrader().grade(
            {"id": "c1", "expectations": [
                {"type": "contains", "value": "a"},
                {"type": "contains", "value": "a"},
            ]},
            "a",
        )
        self.assertIsNone(j.error, j.error)
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_an_extra_equal_to_an_expected_point_is_allowed(self):
        """A produced item may coincide with an expectation."""
        judge = StubJudge(coverage_reply(matched=[1], extra=["alpha"]))
        j = PointCoverageGrader(judge).grade({"id": "c1", "points": ["alpha"]}, "x")
        self.assertIsNone(j.error, j.error)

    def test_repeated_points_still_catch_a_short_partition(self):
        """Relaxing identity must not relax the arithmetic."""
        judge = StubJudge(coverage_reply(matched=[1]))
        j = PointCoverageGrader(judge).grade(
            {"id": "c1", "points": ["same", "same", "same"]}, "x"
        )
        self.assertIsNotNone(j.error)
        self.assertIn("conservation", j.error)

    def test_a_repeated_index_is_still_rejected(self):
        """Identity by position is the grader's job and still enforced."""
        judge = StubJudge(coverage_reply(matched=[1, 1], missed=[2]))
        j = PointCoverageGrader(judge).grade(
            {"id": "c1", "points": ["a", "b"]}, "x"
        )
        self.assertIsNotNone(j.error)


class ConservationLimitsTests(unittest.TestCase):
    """What conservation cannot do, asserted so nobody relies on it.

    The design once claimed this check catches over-reporting outright. It
    does not: it catches a partition that fails to account for everything.
    A classifier that places every point but places them wrongly balances
    perfectly. Recording that here keeps the next reader from building on a
    defence that is not there — the reason commit-first exists.
    """

    def test_a_confidently_wrong_classification_is_not_caught(self):
        judge = StubJudge(coverage_reply(matched=[1, 2, 3, 4]))
        j = PointCoverageGrader(judge).grade(
            {"id": "c1", "points": ["a", "b", "c", "d"]},
            "an answer that actually only covers a",
        )
        self.assertIsNone(j.error)
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_which_is_why_the_rubric_grader_compares_independently(self):
        """Pointer to the mechanism that does address it."""
        from graders import RubricGrader

        self.assertTrue(RubricGrader(StubJudge("x")).commit_first)


class MalformedExpectationTests(unittest.TestCase):
    """A dataset problem must be reported, not scored.

    Coercing whatever was written into a string produces a verdict that
    looks authoritative and means nothing: an empty needle makes `contains`
    pass for free while making `not_contains` impossible, and a dict becomes
    a `"{'a': 1}"` literal no output will ever hold. Both score confidently,
    so the GT author gets no signal that the row is broken — and CSV export
    produces empty cells routinely.
    """

    def grade(self, expectation, output="any output True 123"):
        return ProgrammaticGrader().grade(
            {"id": "c1", "expectations": [expectation]}, output
        )

    def test_an_empty_needle_does_not_pass_for_free(self):
        j = self.grade({"type": "contains", "value": ""})
        self.assertIsNotNone(j.error)
        self.assertIn("empty", j.error)

    def test_an_empty_needle_does_not_make_not_contains_impossible(self):
        j = self.grade({"type": "not_contains", "value": ""})
        self.assertIsNotNone(j.error)

    def test_whitespace_only_is_treated_as_empty(self):
        self.assertIsNotNone(self.grade({"type": "contains", "value": "   "}).error)

    def test_an_empty_regex_is_rejected(self):
        self.assertIsNotNone(self.grade({"type": "regex", "value": ""}).error)

    def test_a_missing_value_is_reported(self):
        j = self.grade({"type": "contains"})
        self.assertIn("no 'value'", j.error)

    def test_structured_values_are_rejected_rather_than_stringified(self):
        for bad in (None, {"a": 1}, ["x"], True):
            with self.subTest(bad=bad):
                j = self.grade({"type": "contains", "value": bad})
                self.assertIsNotNone(j.error, f"{bad!r} was silently accepted")

    def test_a_bool_does_not_match_the_word_it_stringifies_to(self):
        """`True` would otherwise match any output containing "True"."""
        j = self.grade({"type": "contains", "value": True}, "the answer is True")
        self.assertIsNotNone(j.error)

    def test_a_number_is_accepted(self):
        """"the output should contain 42" is a reasonable thing to write."""
        j = self.grade({"type": "contains", "value": 123}, "code 123 here")
        self.assertIsNone(j.error)
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_an_empty_exact_answer_is_meaningful_and_allowed(self):
        """"the output should be blank" is a real requirement."""
        j = self.grade({"type": "exact", "value": ""}, "")
        self.assertIsNone(j.error)
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_one_malformed_row_does_not_abort_the_run(self):
        j = ProgrammaticGrader().grade(
            {"id": "c1", "expectations": [
                {"type": "contains", "value": "alpha"},
                {"type": "contains", "value": ""},
            ]},
            "alpha",
        )
        self.assertIsNotNone(j.error)
        self.assertEqual(j.metrics, {})


class IndexCoercionTests(unittest.TestCase):
    """An index the classifier could not have read is not a low score.

    Two rules, and the second one replaces an earlier mistake.

    A fractional index must never be truncated: `int(1.5)` is 1, so
    truncating turns "point 1 was matched" into a claim the model never
    made.

    An index outside the list must make the case unmeasurable, not merely
    be dropped. Dropping was the original design, and its docstring argued
    that the shortfall would then fail conservation. That is false whenever
    the invented index is *additional* to the real ones — the survivors
    fill the partition exactly, the equation balances, and the case scores
    1.0. Both cases below were verified against the real gate before this
    changed.
    """

    def indices(self, value, total=4):
        from graders import _valid_indices

        return _valid_indices(value, total)

    def rejects(self, value, total=4):
        from graders import InvalidClassification

        with self.assertRaises(InvalidClassification):
            self.indices(value, total)

    def test_a_fraction_is_rejected_not_truncated(self):
        """`int(1.5)` would read as point 1, which nobody claimed."""
        self.rejects([1.5, 2, 3, 4])

    def test_a_fraction_below_one_is_rejected(self):
        self.rejects([0.9, 2])

    def test_an_integral_float_is_accepted(self):
        self.assertEqual(self.indices([2.0, 3]), [2, 3])

    def test_a_quoted_integer_is_accepted(self):
        """Quoting a number is a routine serialisation difference."""
        self.assertEqual(self.indices(["1", 2]), [1, 2])

    def test_a_quoted_fraction_is_rejected(self):
        self.rejects(["1.5", 2])

    def test_a_bool_is_not_an_index(self):
        """True would otherwise pass as point 1 via the numeric tower."""
        self.rejects([True, 2])

    def test_an_index_past_the_end_is_rejected(self):
        self.rejects([1, 5], total=4)

    def test_a_zero_index_is_rejected(self):
        """0-based output is a real failure mode, not a near miss."""
        self.rejects([0], total=4)

    def test_a_fractional_claim_makes_the_case_unmeasurable(self):
        """End to end: it must error, not score."""
        judge = StubJudge(json.dumps({
            "matched": [1.5, 2, 3, 4], "partial": [], "missed": [], "extra": [],
        }))
        j = PointCoverageGrader(judge).grade(
            {"id": "c1", "points": ["a", "b", "c", "d"]}, "out"
        )
        self.assertIsNotNone(j.error)
        self.assertIn("InvalidClassification", j.error)

    def test_an_invented_point_cannot_score_full_marks(self):
        """The hole this class exists to close.

        One real point, and the classifier claims two matched. Dropping the
        invented index left `matched=[1]`, which balances against
        `expected_total=1` — so a classifier that was plainly not reading
        the list scored 1.0.
        """
        judge = StubJudge(json.dumps({
            "matched": [1, 2], "partial": [], "missed": [], "extra": [],
        }))
        j = PointCoverageGrader(judge).grade(
            {"id": "c1", "points": ["a"]}, "out"
        )
        self.assertIsNotNone(j.error)

    def test_extra_invented_points_alongside_every_real_one(self):
        """Same hole, wider: two real points, four claimed."""
        judge = StubJudge(json.dumps({
            "matched": [1, 2, 3, 4], "partial": [], "missed": [], "extra": [],
        }))
        j = PointCoverageGrader(judge).grade(
            {"id": "c1", "points": ["a", "b"]}, "out"
        )
        self.assertIsNotNone(j.error)

    def test_an_honest_partition_still_scores(self):
        """The rule must not make correct classifications unmeasurable."""
        judge = StubJudge(json.dumps({
            "matched": [1], "partial": [], "missed": [2], "extra": [],
        }))
        j = PointCoverageGrader(judge).grade(
            {"id": "c1", "points": ["a", "b"]}, "out"
        )
        self.assertIsNone(j.error)
        self.assertGreater(j.score, 0.0)


class OnePointsDefinitionTests(unittest.TestCase):
    """How many expectations a value holds must have one answer.

    Two implementations of this once disagreed on six of nine inputs, two of
    them differing in the *count* — which silently changed recall's
    denominator depending on which path a value took. They agreed at all
    only because of call order, not by design.
    """

    STRING_INPUTS = [
        '["a","","b"]', '[" ","x"]', '42', '{"k":1}', '[1,2]',
        'null', 'plain text', '', '["only"]', '[1.5, 2]',
    ]

    def test_the_loader_and_the_grader_agree_on_every_string(self):
        from datasets import CaseLoader, ColumnMap
        from graders import _as_points

        loader = CaseLoader("unused.csv", ColumnMap())
        for value in self.STRING_INPUTS:
            with self.subTest(value=value):
                self.assertEqual(loader._parse_points(value), _as_points(value))

    def test_the_grader_delegates_rather_than_reimplementing(self):
        """Asserted on behaviour: a blank entry must be dropped either way.

        The old local parser kept blanks, so a three-element array with one
        empty string counted as three expectations on one path and two on
        the other.
        """
        from graders import _as_points

        self.assertEqual(_as_points('["a","","b"]'), ["a", "b"])

    def test_structured_points_in_memory_are_not_flattened(self):
        """A case built in code may hold dicts; text rendering would lose them."""
        from graders import _as_points

        points = [{"text": "a point", "n": 1}, {"text": "another"}]
        self.assertEqual(_as_points(points), points)

    def test_the_dependency_direction_stays_one_way(self):
        """datasets must not import graders, or the delegation would cycle."""
        import ast

        tree = ast.parse((SCRIPTS / "datasets.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("graders", imported)

    def test_either_import_order_works(self):
        """A local import inside a function must not depend on load order.

        Run in a subprocess. Clearing ``sys.modules`` in-process would hand
        the rest of the suite freshly-imported classes that fail identity
        checks against the ones it already holds — a test must not leave the
        interpreter in a different state than it found it.
        """
        for order in (("graders", "datasets"), ("datasets", "graders")):
            with self.subTest(order=order):
                code = (
                    f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); "
                    f"import {order[0]}; import {order[1]}"
                )
                result = subprocess.run(
                    [sys.executable, "-c", code], capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
