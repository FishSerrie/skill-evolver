"""Tests for judgment — the grading/loop contract.

Imports only the module under test plus stdlib.
"""

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "plugin" / "skills" / "skill-evolver" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from judgment import Judgment, aggregate  # noqa: E402


class ConstructionTests(unittest.TestCase):
    def test_minimal_judgment(self):
        j = Judgment(case_id="1")
        self.assertEqual(j.case_id, "1")
        self.assertEqual(dict(j.metrics), {})
        self.assertFalse(j.passed)
        self.assertIsNone(j.error)

    def test_score_returns_primary_metric(self):
        j = Judgment(case_id="1", metrics={"precision": 0.8, "recall": 0.6},
                     primary="recall")
        self.assertEqual(j.score, 0.6)

    def test_metrics_present_without_primary_is_rejected(self):
        """Fail where the offending grader is still on the stack, not later
        inside the gate where the cause is invisible."""
        with self.assertRaises(ValueError):
            Judgment(case_id="1", metrics={"f1": 0.5})

    def test_primary_not_in_metrics_is_rejected(self):
        with self.assertRaises(ValueError):
            Judgment(case_id="1", metrics={"f1": 0.5}, primary="recall")

    def test_error_judgment_needs_no_primary(self):
        j = Judgment(case_id="1", error="transport failure")
        self.assertEqual(j.score, 0.0)

    def test_failed_factory_sets_error_and_feedback(self):
        j = Judgment.failed("7", "conservation violated")
        self.assertEqual(j.case_id, "7")
        self.assertEqual(j.error, "conservation violated")
        self.assertIn("conservation violated", j.feedback)

    def test_failed_factory_accepts_custom_feedback(self):
        j = Judgment.failed("7", "boom", feedback="judge returned no JSON")
        self.assertEqual(j.feedback, "judge returned no JSON")


class ImmutabilityTests(unittest.TestCase):
    """A judgment is an audit record; if scores can change after the fact,
    the gate's decisions stop being explainable."""

    def test_attribute_cannot_be_rebound(self):
        j = Judgment(case_id="1", metrics={"f1": 0.5}, primary="f1")
        with self.assertRaises(Exception):
            j.passed = True

    def test_metrics_dict_cannot_be_mutated(self):
        """frozen=True alone would leave the dict itself writable."""
        j = Judgment(case_id="1", metrics={"f1": 0.5}, primary="f1")
        with self.assertRaises(TypeError):
            j.metrics["f1"] = 1.0

    def test_evidence_cannot_be_mutated(self):
        j = Judgment(case_id="1", evidence={"missed": ["a"]})
        with self.assertRaises(TypeError):
            j.evidence["missed"] = []

    def test_cost_cannot_be_mutated(self):
        j = Judgment(case_id="1", cost={"tokens": 10})
        with self.assertRaises(TypeError):
            j.cost["tokens"] = 0

    def test_mutating_the_source_dict_does_not_affect_the_judgment(self):
        src = {"f1": 0.5}
        j = Judgment(case_id="1", metrics=src, primary="f1")
        src["f1"] = 1.0
        self.assertEqual(j.metrics["f1"], 0.5)


class AggregateTests(unittest.TestCase):
    def test_empty_input_yields_zeros(self):
        """A run that judged nothing is a real state, not an exception."""
        a = aggregate([])
        self.assertEqual(a["total"], 0)
        self.assertEqual(a["pass_rate"], 0.0)
        self.assertEqual(a["metrics"], {})

    def test_means_over_scored_cases(self):
        js = [
            Judgment(case_id="1", metrics={"f1": 1.0}, primary="f1", passed=True),
            Judgment(case_id="2", metrics={"f1": 0.0}, primary="f1"),
        ]
        a = aggregate(js)
        self.assertEqual(a["metrics"]["f1"], 0.5)
        self.assertEqual(a["pass_rate"], 0.5)

    def test_errored_cases_are_excluded_not_averaged_as_zero(self):
        """The central rule: harness faults must not be scored as candidate
        failures, or a flaky evaluator looks like a bad prompt."""
        js = [
            Judgment(case_id="1", metrics={"f1": 1.0}, primary="f1", passed=True),
            Judgment.failed("2", "judge timed out"),
        ]
        a = aggregate(js)
        self.assertEqual(a["metrics"]["f1"], 1.0)# not 0.5
        self.assertEqual(a["scored"], 1)
        self.assertEqual(a["errored"], 1)
        self.assertEqual(a["total"], 2)

    def test_error_case_ids_are_reported(self):
        a = aggregate([Judgment.failed("x", "boom")])
        self.assertEqual(a["error_case_ids"], ["x"])

    def test_all_errored_yields_zero_not_division_error(self):
        a = aggregate([Judgment.failed("1", "a"), Judgment.failed("2", "b")])
        self.assertEqual(a["scored"], 0)
        self.assertEqual(a["pass_rate"], 0.0)
        self.assertEqual(a["metrics"], {})

    def test_multiple_metrics_averaged_independently(self):
        js = [
            Judgment(case_id="1", metrics={"precision": 1.0, "recall": 0.0},
                     primary="precision"),
            Judgment(case_id="2", metrics={"precision": 0.0, "recall": 1.0},
                     primary="precision"),
        ]
        a = aggregate(js)
        self.assertEqual(a["metrics"]["precision"], 0.5)
        self.assertEqual(a["metrics"]["recall"], 0.5)

    def test_metric_missing_from_some_cases_averages_over_present_only(self):
        """Graders may emit different metric sets; a metric absent from one
        case must not be read as zero for it."""
        js = [
            Judgment(case_id="1", metrics={"f1": 1.0, "extra": 1.0}, primary="f1"),
            Judgment(case_id="2", metrics={"f1": 1.0}, primary="f1"),
        ]
        a = aggregate(js)
        self.assertEqual(a["metrics"]["f1"], 1.0)
        self.assertEqual(a["metrics"]["extra"], 1.0)

    def test_cost_sums_across_all_cases_including_errored(self):
        """An errored case still consumed budget; hiding that understates
        the true cost of a run."""
        js = [
            Judgment(case_id="1", metrics={"f1": 1.0}, primary="f1",
                     cost={"tokens": 100, "duration_ms": 50}),
            Judgment(case_id="2", error="boom", cost={"tokens": 20,
                                                      "duration_ms": 5}),
        ]
        a = aggregate(js)
        self.assertEqual(a["cost"]["tokens"], 120)
        self.assertEqual(a["cost"]["duration_ms"], 55)

    def test_missing_cost_keys_default_to_zero(self):
        a = aggregate([Judgment(case_id="1", metrics={"f1": 1.0}, primary="f1")])
        self.assertEqual(a["cost"]["tokens"], 0)


if __name__ == "__main__":
    unittest.main()
