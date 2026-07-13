import importlib.util
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


isolation = _load_module("skill_evolver_isolation", SCRIPTS_DIR / "isolation.py")


class BuildDiagnoserTaskSpecTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.skill_path = self.workspace / "skill"
        self.skill_path.mkdir(parents=True, exist_ok=True)
        (self.skill_path / "SKILL.md").write_text("# Test\n")
        self.gt_path = self.workspace / "evals.json"

    def test_spec_has_expected_keys(self):
        spec = isolation.build_diagnoser_task_spec(
            self.skill_path, self.workspace, {}, self.gt_path, "body")
        self.assertEqual(set(spec.keys()),
                         {"prompt", "subagent_type", "description", "isolation"})
        self.assertEqual(spec["isolation"], "subagent_context")
        self.assertEqual(spec["subagent_type"], "general-purpose")

    def test_spec_prompt_matches_build_diagnoser_prompt(self):
        review = {"recent_failures": ["x"]}
        spec = isolation.build_diagnoser_task_spec(
            self.skill_path, self.workspace, review, self.gt_path, "body")
        direct_prompt = isolation.build_diagnoser_prompt(
            self.skill_path, self.workspace, review, self.gt_path, "body")
        self.assertEqual(spec["prompt"], direct_prompt)

    def test_spec_prompt_excludes_holdout_same_as_direct_call(self):
        review = {"failed_case_paths": ["iteration-E3/holdout_cases/case_5.json"]}
        spec = isolation.build_diagnoser_task_spec(
            self.skill_path, self.workspace, review, self.gt_path, "body")
        self.assertNotIn("holdout", spec["prompt"].lower())

    def test_description_mentions_skill_name_and_layer(self):
        spec = isolation.build_diagnoser_task_spec(
            self.skill_path, self.workspace, {}, self.gt_path, "script")
        self.assertIn(self.skill_path.name, spec["description"])
        self.assertIn("script", spec["description"])

    def test_custom_subagent_type_is_respected(self):
        spec = isolation.build_diagnoser_task_spec(
            self.skill_path, self.workspace, {}, self.gt_path, "body",
            subagent_type="Explore")
        self.assertEqual(spec["subagent_type"], "Explore")


class ParseDiagnosisResponseTests(unittest.TestCase):
    def test_parses_valid_json_line(self):
        text = (
            "I looked at the cases.\n"
            '{"failure_patterns": [{"case_id": "1", "assertion_index": 0, '
            '"symptom": "missing keyword", "hypothesis": "typo"}], '
            '"recommended_focus": "fix typo", "layer_suggestion": "body", '
            '"evidence_refs": ["case_1.json"]}'
        )
        diagnosis = isolation.parse_diagnosis_response(text)
        self.assertEqual(len(diagnosis["failure_patterns"]), 1)
        self.assertEqual(diagnosis["recommended_focus"], "fix typo")
        self.assertEqual(diagnosis["layer_suggestion"], "body")
        self.assertEqual(diagnosis["evidence_refs"], ["case_1.json"])

    def test_no_json_line_returns_safe_defaults(self):
        diagnosis = isolation.parse_diagnosis_response("I could not find a clear pattern.")
        self.assertEqual(diagnosis, {
            "failure_patterns": [], "recommended_focus": "",
            "layer_suggestion": "", "evidence_refs": [],
        })

    def test_malformed_json_line_returns_safe_defaults(self):
        text = '{"failure_patterns": [}broken json'
        diagnosis = isolation.parse_diagnosis_response(text)
        self.assertEqual(diagnosis["failure_patterns"], [])

    def test_null_fields_default_to_empty_not_none(self):
        text = '{"failure_patterns": null, "evidence_refs": null}'
        diagnosis = isolation.parse_diagnosis_response(text)
        self.assertEqual(diagnosis["failure_patterns"], [])
        self.assertEqual(diagnosis["evidence_refs"], [])

    def test_picks_last_json_line_when_multiple_present(self):
        text = (
            '{"failure_patterns": [], "recommended_focus": "old", '
            '"layer_suggestion": "body", "evidence_refs": []}\n'
            "more reasoning text\n"
            '{"failure_patterns": [], "recommended_focus": "new", '
            '"layer_suggestion": "script", "evidence_refs": []}'
        )
        diagnosis = isolation.parse_diagnosis_response(text)
        self.assertEqual(diagnosis["recommended_focus"], "new")
        self.assertEqual(diagnosis["layer_suggestion"], "script")

    def test_non_dict_json_line_is_skipped(self):
        text = '["not", "a", "dict", "but contains failure_patterns word"]'
        diagnosis = isolation.parse_diagnosis_response(text)
        self.assertEqual(diagnosis["failure_patterns"], [])


if __name__ == "__main__":
    unittest.main()
