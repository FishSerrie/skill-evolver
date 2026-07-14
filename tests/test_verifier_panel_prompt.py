import importlib.util
import inspect
import sys
import tempfile
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


class NarrowSignatureTests(unittest.TestCase):
    """The isolation guarantee IS the function signature — no
    diagnosis/description parameter means there is no code path by
    which the proposer's own account of its change could reach a
    verifier's prompt, mirroring isolation.build_mutator_prompt."""

    def test_build_verifier_task_spec_signature_has_no_proposer_params(self):
        params = set(inspect.signature(verifier_panel.build_verifier_task_spec).parameters)
        self.assertNotIn("diagnosis", params)
        self.assertNotIn("description", params)
        self.assertEqual(params, {"skill_path", "diff", "metrics", "checker"})


class BuildVerifierTaskSpecTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.skill_path = Path(self._tmp.name) / "hello-skill"
        self.skill_path.mkdir(parents=True, exist_ok=True)
        self.diff = "diff --git a/SKILL.md b/SKILL.md\n+added a line"
        self.metrics = {"dev_pass_rate": 0.9, "holdout_pass_rate": 0.7}

    def test_spec_has_expected_keys(self):
        spec = verifier_panel.build_verifier_task_spec(
            self.skill_path, self.diff, self.metrics, "overfit")
        self.assertEqual(set(spec.keys()), {"prompt", "checker", "description"})
        self.assertEqual(spec["checker"], "overfit")

    def test_overfit_prompt_contains_suspicion_angle_and_diff_and_metrics(self):
        spec = verifier_panel.build_verifier_task_spec(
            self.skill_path, self.diff, self.metrics, "overfit")
        self.assertIn("OVERFITTING", spec["prompt"])
        self.assertIn("added a line", spec["prompt"])
        self.assertIn("0.9", spec["prompt"])
        self.assertIn("0.7", spec["prompt"])

    def test_assertion_gaming_prompt_contains_suspicion_angle(self):
        spec = verifier_panel.build_verifier_task_spec(
            self.skill_path, self.diff, self.metrics, "assertion_gaming")
        self.assertIn("ASSERTION GAMING", spec["prompt"])
        self.assertIn("added a line", spec["prompt"])

    def test_structural_prompt_contains_suspicion_angle(self):
        spec = verifier_panel.build_verifier_task_spec(
            self.skill_path, self.diff, self.metrics, "structural")
        self.assertIn("STRUCTURAL INTEGRITY", spec["prompt"])
        self.assertIn("added a line", spec["prompt"])

    def test_prompt_does_not_reveal_other_checkers_finding(self):
        spec = verifier_panel.build_verifier_task_spec(
            self.skill_path, self.diff, self.metrics, "overfit")
        self.assertIn("do not know what the other two verifiers found", spec["prompt"])

    def test_unknown_checker_raises(self):
        with self.assertRaises(ValueError):
            verifier_panel.build_verifier_task_spec(
                self.skill_path, self.diff, self.metrics, "not_a_real_checker")

    def test_read_only_instruction_present(self):
        spec = verifier_panel.build_verifier_task_spec(
            self.skill_path, self.diff, self.metrics, "structural")
        self.assertIn("Do NOT modify anything", spec["prompt"])


class ParseVerifierResponseTests(unittest.TestCase):
    def test_parses_pass_verdict(self):
        text = 'looks fine\n{"verdict": "pass", "reason": "no issue found"}'
        result = verifier_panel.parse_verifier_response(text, "overfit")
        self.assertEqual(result, {"checker": "overfit", "verdict": "pass",
                                  "reason": "no issue found"})

    def test_parses_reject_verdict(self):
        text = '{"verdict": "reject", "reason": "stuffed the literal string into an unrelated sentence"}'
        result = verifier_panel.parse_verifier_response(text, "assertion_gaming")
        self.assertEqual(result["verdict"], "reject")
        self.assertEqual(result["checker"], "assertion_gaming")

    def test_picks_last_json_line_when_multiple_present(self):
        text = '{"verdict": "pass", "reason": "first"}\n{"verdict": "reject", "reason": "final"}'
        result = verifier_panel.parse_verifier_response(text, "structural")
        self.assertEqual(result["verdict"], "reject")
        self.assertEqual(result["reason"], "final")

    def test_invalid_verdict_value_becomes_error(self):
        text = '{"verdict": "maybe", "reason": "unsure"}'
        result = verifier_panel.parse_verifier_response(text, "overfit")
        self.assertEqual(result["verdict"], "error")

    def test_malformed_json_returns_error(self):
        result = verifier_panel.parse_verifier_response('{"verdict": "pass", broken', "overfit")
        self.assertEqual(result["verdict"], "error")

    def test_no_json_line_returns_error(self):
        result = verifier_panel.parse_verifier_response(
            "I reviewed it but forgot the format.", "structural")
        self.assertEqual(result, {"checker": "structural", "verdict": "error",
                                  "reason": "could not parse verifier response"})

    def test_non_dict_json_line_is_skipped(self):
        text = '[1, 2, 3]\n{"verdict": "pass", "reason": "ok"}'
        result = verifier_panel.parse_verifier_response(text, "overfit")
        self.assertEqual(result["verdict"], "pass")


if __name__ == "__main__":
    unittest.main()
