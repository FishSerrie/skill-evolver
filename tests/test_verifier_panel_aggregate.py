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
        verdicts = [_verdict("overfit", "error"), _verdict("assertion_gaming", "pass"),
                   _verdict("structural", "reject")]
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


if __name__ == "__main__":
    unittest.main()
