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


isolation = _load_module("skill_evolver_isolation", SCRIPTS_DIR / "isolation.py")


class NarrowSignatureTests(unittest.TestCase):
    """The isolation guarantee IS the function signature — this test
    exists so a future edit that adds review/gt_path/workspace back
    onto the mutator fails CI instead of silently reopening the leak."""

    def test_build_mutator_prompt_signature_has_no_evidence_params(self):
        params = set(inspect.signature(isolation.build_mutator_prompt).parameters)
        self.assertNotIn("review", params)
        self.assertNotIn("gt_path", params)
        self.assertNotIn("workspace", params)
        self.assertEqual(params, {"skill_path", "diagnosis", "current_layer"})

    def test_build_mutator_task_spec_signature_has_no_evidence_params(self):
        params = set(inspect.signature(isolation.build_mutator_task_spec).parameters)
        self.assertNotIn("review", params)
        self.assertNotIn("gt_path", params)
        self.assertNotIn("workspace", params)


class BuildMutatorPromptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.skill_path = Path(self._tmp.name) / "skill"
        self.skill_path.mkdir(parents=True, exist_ok=True)

    def test_diagnosis_content_propagates_into_prompt(self):
        diagnosis = {
            "failure_patterns": [{"case_id": "1", "symptom": "missing keyword"}],
            "recommended_focus": "add the missing keyword",
            "evidence_refs": ["case_1.json"],
        }
        prompt = isolation.build_mutator_prompt(self.skill_path, diagnosis, "body")
        self.assertIn("add the missing keyword", prompt)
        self.assertIn("missing keyword", prompt)

    def test_empty_diagnosis_still_produces_valid_prompt(self):
        prompt = isolation.build_mutator_prompt(self.skill_path, {}, "body")
        self.assertIn("MAKING ONE ATOMIC CHANGE", prompt)
        self.assertIn("none given", prompt)

    def test_prompt_forbids_git_and_re_diagnosis(self):
        prompt = isolation.build_mutator_prompt(self.skill_path, {}, "body")
        self.assertIn("Do NOT run git commands", prompt)
        self.assertIn("Do NOT re-derive your own diagnosis", prompt)

    def test_prompt_references_current_layer(self):
        prompt = isolation.build_mutator_prompt(self.skill_path, {}, "script")
        self.assertIn("Current layer: script", prompt)


class BuildMutatorTaskSpecTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.skill_path = Path(self._tmp.name) / "skill"
        self.skill_path.mkdir(parents=True, exist_ok=True)

    def test_spec_has_expected_keys(self):
        spec = isolation.build_mutator_task_spec(self.skill_path, {}, "body")
        self.assertEqual(set(spec.keys()),
                         {"prompt", "subagent_type", "description", "isolation"})
        self.assertEqual(spec["isolation"], "subagent_context")

    def test_spec_prompt_matches_direct_call(self):
        diagnosis = {"recommended_focus": "fix X"}
        spec = isolation.build_mutator_task_spec(self.skill_path, diagnosis, "body")
        direct = isolation.build_mutator_prompt(self.skill_path, diagnosis, "body")
        self.assertEqual(spec["prompt"], direct)


class ParseMutationResponseTests(unittest.TestCase):
    def test_parses_valid_json_line(self):
        text = 'reasoning...\n{"changed": true, "description": "added missing keyword"}'
        result = isolation.parse_mutation_response(text)
        self.assertTrue(result["changed"])
        self.assertEqual(result["description"], "added missing keyword")

    def test_no_change_case(self):
        text = '{"changed": false, "description": "no improvement found"}'
        result = isolation.parse_mutation_response(text)
        self.assertFalse(result["changed"])

    def test_no_json_line_returns_safe_default(self):
        result = isolation.parse_mutation_response("I made some edits but forgot the format.")
        self.assertEqual(result, {"changed": False, "description": "could not parse response"})

    def test_malformed_json_returns_safe_default(self):
        result = isolation.parse_mutation_response('{"changed": true, broken')
        self.assertFalse(result["changed"])

    def test_missing_description_gets_default_text(self):
        result = isolation.parse_mutation_response('{"changed": true}')
        self.assertTrue(result["changed"])
        self.assertEqual(result["description"], "llm did not provide description")


if __name__ == "__main__":
    unittest.main()
