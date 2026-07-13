import importlib.util
import sys
import tempfile
import unittest
import warnings
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


class Phase23DeprecatedWrapperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.skill_path = self.workspace / "skill"
        self.skill_path.mkdir(parents=True, exist_ok=True)
        (self.skill_path / "SKILL.md").write_text("# Test\n")
        self.gt_path = self.workspace / "evals.json"

    def test_emits_deprecation_warning(self):
        def fake_call_claude(prompt, **kwargs):
            if "DIAGNOSING" in prompt:
                return '{"failure_patterns": [], "recommended_focus": "x", "layer_suggestion": "body", "evidence_refs": []}'
            return '{"changed": true, "description": "did x"}'

        with mock.patch.object(llm, "_call_claude", side_effect=fake_call_claude):
            with self.assertWarns(DeprecationWarning):
                llm.phase_2_3_ideate_and_modify(
                    self.skill_path, self.workspace, {}, self.gt_path, "body")

    def test_return_shape_matches_old_contract(self):
        def fake_call_claude(prompt, **kwargs):
            if "DIAGNOSING" in prompt:
                return '{"failure_patterns": [], "recommended_focus": "add missing word", "layer_suggestion": "body", "evidence_refs": []}'
            return '{"changed": true, "description": "added the missing word"}'

        with mock.patch.object(llm, "_call_claude", side_effect=fake_call_claude):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                result = llm.phase_2_3_ideate_and_modify(
                    self.skill_path, self.workspace, {}, self.gt_path, "body")

        self.assertEqual(set(result.keys()),
                         {"changed", "description", "mutation_type", "diagnosis"})
        self.assertTrue(result["changed"])
        self.assertEqual(result["description"], "added the missing word")
        self.assertEqual(result["diagnosis"], "add missing word")

    def test_makes_two_independent_calls_under_the_hood(self):
        call_log = []

        def fake_call_claude(prompt, **kwargs):
            call_log.append(prompt)
            if "DIAGNOSING" in prompt:
                return '{"failure_patterns": [], "recommended_focus": "y", "layer_suggestion": "body", "evidence_refs": []}'
            return '{"changed": false, "description": "no improvement found"}'

        with mock.patch.object(llm, "_call_claude", side_effect=fake_call_claude):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                result = llm.phase_2_3_ideate_and_modify(
                    self.skill_path, self.workspace, {}, self.gt_path, "body")

        self.assertEqual(len(call_log), 2)
        self.assertFalse(result["changed"])

    def test_no_changed_result_when_diagnosis_or_mutation_fails(self):
        with mock.patch.object(llm, "_call_claude", return_value="unparseable garbage"):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                result = llm.phase_2_3_ideate_and_modify(
                    self.skill_path, self.workspace, {}, self.gt_path, "body")

        self.assertFalse(result["changed"])


if __name__ == "__main__":
    unittest.main()
