import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "plugin" / "skills" / "skill-evolver" / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


verifier_panel = _load_module("skill_evolver_verifier_panel", SCRIPTS_DIR / "verifier_panel.py")


def _verdict(checker, verdict, reason="because"):
    return {"checker": checker, "verdict": verdict, "reason": reason}


class AggregateVerdictsAllCleanTests(unittest.TestCase):
    def test_all_three_pass(self):
        verdicts = [_verdict("overfit", "pass"), _verdict("assertion_gaming", "pass"),
                   _verdict("structural", "pass")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        self.assertEqual(result["decision"], "pass")
        self.assertEqual(result["verdicts"], verdicts)

    def test_two_of_three_reject_is_majority_veto(self):
        verdicts = [_verdict("overfit", "reject", "dev-holdout gap"),
                   _verdict("assertion_gaming", "reject", "literal string stuffed"),
                   _verdict("structural", "pass")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        self.assertEqual(result["decision"], "reject")
        self.assertIn("dev-holdout gap", result["reasoning"])
        self.assertIn("literal string stuffed", result["reasoning"])

    def test_one_of_three_reject_is_not_enough_to_veto(self):
        verdicts = [_verdict("overfit", "reject"), _verdict("assertion_gaming", "pass"),
                   _verdict("structural", "pass")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        self.assertEqual(result["decision"], "pass")

    def test_all_three_reject(self):
        verdicts = [_verdict("overfit", "reject"), _verdict("assertion_gaming", "reject"),
                   _verdict("structural", "reject")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        self.assertEqual(result["decision"], "reject")


class AggregateVerdictsOneErrorTests(unittest.TestCase):
    def test_one_error_remaining_two_agree_pass(self):
        verdicts = [_verdict("overfit", "error"), _verdict("assertion_gaming", "pass"),
                   _verdict("structural", "pass")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        self.assertEqual(result["decision"], "pass")

    def test_one_error_remaining_two_agree_reject(self):
        verdicts = [_verdict("overfit", "error"), _verdict("assertion_gaming", "reject"),
                   _verdict("structural", "reject")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        self.assertEqual(result["decision"], "reject")

    def test_one_error_remaining_two_disagree_defaults_to_reject(self):
        # Neither remaining verdict is "structural" here, so this
        # exercises the generic 1-error-disagree conservative-reject
        # path, not the structural veto (see StructuralVetoTests below
        # for that).
        verdicts = [_verdict("structural", "error"), _verdict("assertion_gaming", "pass"),
                   _verdict("overfit", "reject")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        self.assertEqual(result["decision"], "reject")
        self.assertIn("conservative", result["reasoning"])


class AggregateVerdictsMultiErrorTests(unittest.TestCase):
    def test_two_errors_is_skipped_not_pass_or_reject(self):
        verdicts = [_verdict("overfit", "error"), _verdict("assertion_gaming", "error"),
                   _verdict("structural", "pass")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        self.assertEqual(result["decision"], "skipped")

    def test_three_errors_is_skipped(self):
        verdicts = [_verdict("overfit", "error"), _verdict("assertion_gaming", "error"),
                   _verdict("structural", "error")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        self.assertEqual(result["decision"], "skipped")

    def test_skipped_result_preserves_full_verdict_list(self):
        verdicts = [_verdict("overfit", "error"), _verdict("assertion_gaming", "error"),
                   _verdict("structural", "pass")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        self.assertEqual(result["verdicts"], verdicts)
        self.assertIn("2/3", result["reasoning"])


class StructuralVetoTests(unittest.TestCase):
    """Real gap found via a live red-team round: a genuine structural
    violation (a required section silently deleted) got a real
    "reject" from the structural checker, but overfit/assertion_gaming
    correctly said "pass" from their own narrow angles — the old
    "≥2/3 reject" majority rule outvoted the one checker that actually
    saw the problem. structural is different in kind from the other
    two (near-objective consistency check, not a judgment call about
    intent), so its reject is now an independent veto that bypasses
    the majority count entirely."""

    def test_structural_reject_alone_vetoes_even_when_other_two_pass(self):
        verdicts = [_verdict("overfit", "pass"), _verdict("assertion_gaming", "pass"),
                   _verdict("structural", "reject", "deleted Error Handling section")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        self.assertEqual(result["decision"], "reject")
        self.assertIn("structural veto", result["reasoning"])
        self.assertIn("deleted Error Handling section", result["reasoning"])

    def test_structural_pass_does_not_trigger_veto_path(self):
        verdicts = [_verdict("overfit", "pass"), _verdict("assertion_gaming", "pass"),
                   _verdict("structural", "pass")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        self.assertEqual(result["decision"], "pass")
        self.assertNotIn("structural veto", result["reasoning"])

    def test_structural_veto_fires_even_if_the_other_two_calls_errored(self):
        # A structural reject is real signal that shouldn't be
        # discarded just because two unrelated calls failed.
        verdicts = [_verdict("overfit", "error"), _verdict("assertion_gaming", "error"),
                   _verdict("structural", "reject", "deleted a helper script")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        self.assertEqual(result["decision"], "reject")
        self.assertIn("structural veto", result["reasoning"])

    def test_structural_error_does_not_veto_only_a_real_reject_does(self):
        verdicts = [_verdict("overfit", "pass"), _verdict("assertion_gaming", "pass"),
                   _verdict("structural", "error")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        # Only 1 error total, remaining 2 agree "pass" -> pass, not a veto.
        self.assertEqual(result["decision"], "pass")
        self.assertNotIn("structural veto", result["reasoning"])


class AggregateVerdictsRobustnessTests(unittest.TestCase):
    """Real bugs found via adversarial review: aggregate_verdicts used
    to accept any-length input and hardcode "3" into the reasoning
    text regardless of actual list length, and silently treated any
    verdict value outside pass/reject/error as an implicit pass."""

    def test_wrong_length_raises_value_error(self):
        with self.assertRaises(ValueError):
            verifier_panel.aggregate_verdicts([_verdict("overfit", "pass")])

    def test_empty_list_raises_value_error(self):
        with self.assertRaises(ValueError):
            verifier_panel.aggregate_verdicts([])

    def test_too_many_verdicts_raises_value_error(self):
        with self.assertRaises(ValueError):
            verifier_panel.aggregate_verdicts(
                [_verdict("overfit", "pass")] * 5)

    def test_unrecognized_verdict_value_is_treated_as_error_not_pass(self):
        verdicts = [_verdict("overfit", "maybe"), _verdict("assertion_gaming", "reject"),
                   _verdict("structural", "pass")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        # 1 error (the "maybe") + 1 reject + 1 pass among the clean two
        # -> degrades to the 2-remaining-disagree conservative-reject rule,
        # NOT a silent pass from treating "maybe" as an implicit pass.
        self.assertEqual(result["decision"], "reject")

    def test_reasoning_denominator_matches_actual_clean_count(self):
        verdicts = [_verdict("overfit", "pass"), _verdict("assertion_gaming", "pass"),
                   _verdict("structural", "pass")]
        result = verifier_panel.aggregate_verdicts(verdicts)
        self.assertIn("3/3", result["reasoning"])
        self.assertNotIn("0/3", result["reasoning"])


if __name__ == "__main__":
    unittest.main()
