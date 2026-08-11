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
import isolation  # noqa: E402 — real module, same instance llm.py's lazy imports reuse


class Phase2DiagnoseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.skill_path = self.workspace / "skill"
        self.skill_path.mkdir(parents=True, exist_ok=True)
        (self.skill_path / "SKILL.md").write_text("# Test\n")
        self.gt_path = self.workspace / "evals.json"

    def test_calls_claude_with_diagnoser_prompt_and_parses_result(self):
        captured_prompts = []

        def fake_call_claude(prompt, **kwargs):
            captured_prompts.append(prompt)
            return (
                '{"failure_patterns": [{"case_id": "1", "symptom": "x"}], '
                '"recommended_focus": "fix x", "layer_suggestion": "body", '
                '"evidence_refs": ["case_1.json"]}'
            )

        with mock.patch.object(llm, "_call_claude", side_effect=fake_call_claude):
            diagnosis = llm.phase_2_diagnose(
                self.skill_path, self.workspace, {}, self.gt_path, "body")

        self.assertEqual(diagnosis["recommended_focus"], "fix x")
        self.assertEqual(len(captured_prompts), 1)
        self.assertIn("DIAGNOSING", captured_prompts[0])

    def test_diagnoser_prompt_excludes_holdout_even_via_llm_entrypoint(self):
        review = {"failed_case_paths": ["iteration-E3/holdout_cases/case_5.json"]}
        captured_prompts = []

        def fake_call_claude(prompt, **kwargs):
            captured_prompts.append(prompt)
            return '{"failure_patterns": [], "recommended_focus": "", "layer_suggestion": "body", "evidence_refs": []}'

        with mock.patch.object(llm, "_call_claude", side_effect=fake_call_claude):
            llm.phase_2_diagnose(self.skill_path, self.workspace, review, self.gt_path, "body")

        self.assertNotIn("holdout", captured_prompts[0].lower())

    def test_malformed_response_returns_safe_defaults(self):
        with mock.patch.object(llm, "_call_claude", return_value="no json here"):
            diagnosis = llm.phase_2_diagnose(
                self.skill_path, self.workspace, {}, self.gt_path, "body")
        self.assertEqual(diagnosis["failure_patterns"], [])


class Phase3ModifyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.skill_path = Path(self._tmp.name) / "skill"
        self.skill_path.mkdir(parents=True, exist_ok=True)

    def test_calls_claude_with_mutator_prompt_and_parses_result(self):
        captured_prompts = []

        def fake_call_claude(prompt, **kwargs):
            captured_prompts.append(prompt)
            return '{"changed": true, "description": "fixed the typo"}'

        diagnosis = {"recommended_focus": "fix the typo", "failure_patterns": []}
        with mock.patch.object(llm, "_call_claude", side_effect=fake_call_claude):
            result = llm.phase_3_modify(self.skill_path, diagnosis, "body")

        self.assertTrue(result["changed"])
        self.assertEqual(result["description"], "fixed the typo")
        self.assertIn("fix the typo", captured_prompts[0])
        self.assertIn("MAKING ONE ATOMIC CHANGE", captured_prompts[0])

    def test_signature_accepts_no_review_or_gt_path_kwargs(self):
        # If someone tries to smuggle raw evidence through, this must
        # raise a TypeError — the parameter simply does not exist.
        with self.assertRaises(TypeError):
            llm.phase_3_modify(
                self.skill_path, {}, "body", review={"secret": "evidence"})

    def test_malformed_response_returns_safe_default(self):
        with mock.patch.object(llm, "_call_claude", return_value="I edited something"):
            result = llm.phase_3_modify(self.skill_path, {}, "body")
        self.assertFalse(result["changed"])


class Phase2And3IndependentCallsTests(unittest.TestCase):
    """Verifies the two phases are genuinely two separate _call_claude
    invocations, not one call reused — the actual isolation mechanism
    in CLI mode."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.skill_path = self.workspace / "skill"
        self.skill_path.mkdir(parents=True, exist_ok=True)
        self.gt_path = self.workspace / "evals.json"

    def test_diagnose_then_modify_makes_two_independent_calls(self):
        call_log = []

        def fake_call_claude(prompt, **kwargs):
            call_log.append(prompt)
            if "DIAGNOSING" in prompt:
                return '{"failure_patterns": [], "recommended_focus": "do X", "layer_suggestion": "body", "evidence_refs": []}'
            return '{"changed": true, "description": "did X"}'

        with mock.patch.object(llm, "_call_claude", side_effect=fake_call_claude):
            diagnosis = llm.phase_2_diagnose(
                self.skill_path, self.workspace, {}, self.gt_path, "body")
            result = llm.phase_3_modify(self.skill_path, diagnosis, "body")

        self.assertEqual(len(call_log), 2)
        self.assertTrue(result["changed"])
        # The mutator's prompt must never contain the diagnoser's own
        # instructions/prompt text verbatim (would indicate shared
        # context rather than two independent calls).
        self.assertNotIn("DIAGNOSING", call_log[1])


if __name__ == "__main__":
    unittest.main()
