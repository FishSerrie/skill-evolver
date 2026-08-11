"""Tests for scoring — the single scoring implementation.

Imports only the module under test plus stdlib. These are pure functions,
so every branch is reachable without a subprocess or a model call; the
whole loop's credibility rests on this arithmetic, so it is covered
exhaustively rather than representatively.
"""

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "plugin" / "skills" / "skill-evolver" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from scoring import (  # noqa: E402
    DEFAULT_PARTIAL_WEIGHT,
    ConservationError,
    Outcome,
    check_conservation,
    compute_prf,
)


class PerfectAndEmptyTests(unittest.TestCase):
    def test_all_matched_scores_one(self):
        o = Outcome(matched=["a", "b"], expected_total=2, produced_total=2)
        self.assertEqual(
            compute_prf(o), {"precision": 1.0, "recall": 1.0, "f1": 1.0})

    def test_all_missed_scores_zero(self):
        o = Outcome(missed=["a", "b"], expected_total=2, produced_total=0)
        self.assertEqual(
            compute_prf(o), {"precision": 0.0, "recall": 0.0, "f1": 0.0})

    def test_empty_outcome_scores_zero_without_raising(self):
        """No expectations and no output is a legitimate state, not a fault."""
        self.assertEqual(
            compute_prf(Outcome()),
            {"precision": 0.0, "recall": 0.0, "f1": 0.0})

    def test_zero_expected_total_does_not_divide_by_zero(self):
        o = Outcome(extra=["x"], expected_total=0, produced_total=1)
        self.assertEqual(compute_prf(o)["recall"], 0.0)

    def test_zero_produced_total_does_not_divide_by_zero(self):
        o = Outcome(missed=["a"], expected_total=1, produced_total=0)
        self.assertEqual(compute_prf(o)["precision"], 0.0)


class PrecisionRecallAreIndependentTests(unittest.TestCase):
    """The reason metrics are returned separately: they diagnose
    different failures and must be able to move independently."""

    def test_missing_expectations_lowers_recall_only(self):
        o = Outcome(matched=["a"], missed=["b"], expected_total=2,
                    produced_total=1)
        m = compute_prf(o)
        self.assertEqual(m["recall"], 0.5)
        self.assertEqual(m["precision"], 1.0)

    def test_inventing_content_lowers_precision_only(self):
        o = Outcome(matched=["a"], extra=["x"], expected_total=1,
                    produced_total=2)
        m = compute_prf(o)
        self.assertEqual(m["recall"], 1.0)
        self.assertEqual(m["precision"], 0.5)

    def test_f1_is_harmonic_mean(self):
        o = Outcome(matched=["a"], missed=["b"], extra=["x"],
                    expected_total=2, produced_total=2)
        m = compute_prf(o)
        self.assertEqual(m["precision"], 0.5)
        self.assertEqual(m["recall"], 0.5)
        self.assertEqual(m["f1"], 0.5)


class PartialCreditTests(unittest.TestCase):
    def test_default_weight_is_half(self):
        self.assertEqual(DEFAULT_PARTIAL_WEIGHT, 0.5)

    def test_partial_scores_between_miss_and_match(self):
        """The whole point of partial credit: it preserves the gradient."""
        base = dict(expected_total=2, produced_total=2)
        miss = compute_prf(Outcome(matched=["a"], missed=["b"], **base))["recall"]
        part = compute_prf(Outcome(matched=["a"], partial=["b"], **base))["recall"]
        full = compute_prf(Outcome(matched=["a", "b"], **base))["recall"]
        self.assertLess(miss, part)
        self.assertLess(part, full)

    def test_partial_weight_zero_equals_a_miss(self):
        o = Outcome(matched=["a"], partial=["b"], expected_total=2,
                    produced_total=2)
        self.assertEqual(compute_prf(o, partial_weight=0.0)["recall"], 0.5)

    def test_partial_weight_one_equals_a_match(self):
        o = Outcome(matched=["a"], partial=["b"], expected_total=2,
                    produced_total=2)
        self.assertEqual(compute_prf(o, partial_weight=1.0)["recall"], 1.0)

    def test_partial_counts_toward_precision_too(self):
        """Something WAS produced for a partial, so it is not free."""
        o = Outcome(partial=["a"], expected_total=1, produced_total=1)
        self.assertEqual(compute_prf(o)["precision"], 0.5)

    def test_weight_above_one_is_rejected(self):
        """Would let a metric exceed 1.0 and silently break thresholds."""
        with self.assertRaises(ValueError):
            compute_prf(Outcome(), partial_weight=1.5)

    def test_negative_weight_is_rejected(self):
        with self.assertRaises(ValueError):
            compute_prf(Outcome(), partial_weight=-0.1)


class ConservationTests(unittest.TestCase):
    """The anti-inflation invariant: an LLM classifier cannot pad one
    bucket without the arithmetic ceasing to balance."""

    def test_balanced_outcome_passes(self):
        o = Outcome(matched=["a"], partial=["b"], missed=["c"],
                    expected_total=3)
        check_conservation(o)  # must not raise

    def test_inflated_matched_is_caught(self):
        """Claiming 3 matches when only 2 were expected must not slip through."""
        o = Outcome(matched=["a", "b", "c"], expected_total=2)
        with self.assertRaises(ConservationError):
            check_conservation(o)

    def test_undercount_is_caught(self):
        o = Outcome(matched=["a"], expected_total=3)
        with self.assertRaises(ConservationError):
            check_conservation(o)

    def test_equal_items_in_two_buckets_are_not_this_modules_business(self):
        """Identity belongs to whoever built the Outcome.

        An earlier version compared items by their string form and rejected
        any two buckets holding equal-looking entries. That voided whole
        cases for reasons that were not the candidate's fault: ground truth
        may legitimately list the same expectation twice, and a produced item
        may legitimately equal an expected one. Only the layer that knows
        whether its items are positions or values can tell a real
        double-count from a coincidence — see
        ``graders.PointCoverageGrader``, which rejects a repeated point
        *index*.
        """
        check_conservation(Outcome(matched=["a"], missed=["a"], expected_total=2))

    def test_an_extra_equal_to_a_matched_item_is_allowed(self):
        check_conservation(Outcome(matched=["a"], extra=["a"], expected_total=1))

    def test_relaxing_identity_did_not_relax_the_arithmetic(self):
        """The durable guarantee: a short partition is still rejected."""
        with self.assertRaises(ConservationError):
            check_conservation(
                Outcome(matched=["a"], missed=["a"], expected_total=3)
            )

    def test_produced_side_overflow_is_caught(self):
        o = Outcome(matched=["a", "b"], extra=["x"], expected_total=2,
                    produced_total=2)
        with self.assertRaises(ConservationError):
            check_conservation(o)

    def test_dict_items_are_supported(self):
        """Graders return dicts; unhashable items must not break the check."""
        o = Outcome(matched=[{"id": 1}], missed=[{"id": 2}], expected_total=3)
        with self.assertRaises(ConservationError):
            check_conservation(o)

    def test_equal_dict_items_are_also_permitted(self):
        o = Outcome(matched=[{"id": 1}], missed=[{"id": 1}], expected_total=2)
        check_conservation(o)  # must not raise

    def test_missing_reference_counts_skip_the_check(self):
        """None means 'no reference supplied', not 'derive it' —
        deriving would make the invariant tautological."""
        check_conservation(Outcome(matched=["a", "b"]))  # must not raise

    def test_compute_prf_verifies_by_default(self):
        o = Outcome(matched=["a", "b", "c"], expected_total=2)
        with self.assertRaises(ConservationError):
            compute_prf(o)

    def test_verification_can_be_skipped_explicitly(self):
        o = Outcome(matched=["a", "b", "c"], expected_total=2)
        self.assertIsInstance(compute_prf(o, verify=False), dict)

    def test_conservation_failure_is_not_reported_as_a_zero_score(self):
        """An evaluator fault must not be attributed to the candidate."""
        o = Outcome(matched=["a", "b", "c"], expected_total=2)
        with self.assertRaises(ConservationError):
            compute_prf(o)


class ContractTests(unittest.TestCase):
    def test_outcome_is_immutable(self):
        o = Outcome(matched=["a"])
        with self.assertRaises(Exception):
            o.matched = []

    def test_rounding_applied_at_the_end(self):
        o = Outcome(matched=["a"], missed=["b", "c"], expected_total=3,
                    produced_total=1)
        self.assertEqual(compute_prf(o)["recall"], 0.3333)

    def test_ndigits_is_configurable(self):
        o = Outcome(matched=["a"], missed=["b", "c"], expected_total=3,
                    produced_total=1)
        self.assertEqual(compute_prf(o, ndigits=2)["recall"], 0.33)

    def test_metrics_never_exceed_one(self):
        for w in (0.0, 0.25, 0.5, 0.75, 1.0):
            o = Outcome(matched=["a"], partial=["b"], expected_total=2,
                        produced_total=2)
            for v in compute_prf(o, partial_weight=w).values():
                self.assertGreaterEqual(v, 0.0)
                self.assertLessEqual(v, 1.0)


if __name__ == "__main__":
    unittest.main()
