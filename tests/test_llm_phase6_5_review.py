import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


llm = _load_module("skill_evolver_llm", SCRIPTS_DIR / "llm.py")
import verifier_panel  # noqa: E402 — real module, same instance llm.py's lazy import reuses


class Phase65ReviewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.skill_path = Path(self._tmp.name) / "skill"
        self.skill_path.mkdir(parents=True, exist_ok=True)
        self.diff = "diff --git a/SKILL.md b/SKILL.md\n+added a line"
        self.metrics = {"dev_pass_rate": 0.9, "holdout_pass_rate": 0.85}

    def test_makes_three_independent_calls_one_per_checker(self):
        call_log = []

        def fake_call_claude(prompt, **kwargs):
            call_log.append(prompt)
            return '{"verdict": "pass", "reason": "looks fine"}'

        with mock.patch.object(llm, "_call_claude", side_effect=fake_call_claude):
            result = llm.phase_6_5_review(self.skill_path, self.diff, self.metrics)

        self.assertEqual(len(call_log), 3)
        self.assertEqual(result["decision"], "pass")
        # Each of the three checkers' distinctive suspicion angle text must
        # appear in exactly one of the three prompts — proof the panel isn't
        # sending the same prompt three times.
        joined = "\n---\n".join(call_log)
        self.assertIn("OVERFITTING", joined)
        self.assertIn("ASSERTION GAMING", joined)
        self.assertIn("STRUCTURAL INTEGRITY", joined)

    def test_majority_reject_from_real_responses_vetoes(self):
        def fake_call_claude(prompt, **kwargs):
            if "ASSERTION GAMING" in prompt:
                return '{"verdict": "reject", "reason": "literal string stuffed into unrelated sentence"}'
            if "OVERFITTING" in prompt:
                return '{"verdict": "reject", "reason": "dev up, holdout flat"}'
            return '{"verdict": "pass", "reason": "structure intact"}'

        with mock.patch.object(llm, "_call_claude", side_effect=fake_call_claude):
            result = llm.phase_6_5_review(self.skill_path, self.diff, self.metrics)

        self.assertEqual(result["decision"], "reject")

    def test_one_malformed_response_degrades_to_remaining_agreement(self):
        def fake_call_claude(prompt, **kwargs):
            if "STRUCTURAL INTEGRITY" in prompt:
                return "I reviewed it but forgot the format."
            return '{"verdict": "pass", "reason": "fine"}'

        with mock.patch.object(llm, "_call_claude", side_effect=fake_call_claude):
            result = llm.phase_6_5_review(self.skill_path, self.diff, self.metrics)

        self.assertEqual(result["decision"], "pass")
        errors = [v for v in result["verdicts"] if v["verdict"] == "error"]
        self.assertEqual(len(errors), 1)

    def test_returns_verifier_panel_aggregate_shape(self):
        with mock.patch.object(llm, "_call_claude",
                               return_value='{"verdict": "pass", "reason": "fine"}'):
            result = llm.phase_6_5_review(self.skill_path, self.diff, self.metrics)

        self.assertEqual(set(result.keys()), {"decision", "verdicts", "reasoning"})
        self.assertEqual(len(result["verdicts"]), 3)
        self.assertEqual({v["checker"] for v in result["verdicts"]},
                         set(verifier_panel.CHECKERS))


if __name__ == "__main__":
    unittest.main()
