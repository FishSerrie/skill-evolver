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


class BuildDiagnoserPromptHoldoutExclusionTests(unittest.TestCase):
    """The literal regression test the architecture plan (§4) specifies:
    holdout must never reach the diagnoser's prompt, regardless of what
    the caller passes in."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.skill_path = self.workspace / "skill"
        self.skill_path.mkdir(parents=True, exist_ok=True)
        (self.skill_path / "SKILL.md").write_text("# Test\n")
        self.gt_path = self.workspace / "evals.json"

    def test_clean_review_produces_no_holdout_mention(self):
        review = {
            "recent_failures": ["case 1 failed on contains check"],
            "successful_patterns": ["body_rewrite"],
            "cases_dir": str(self.workspace / "evolve" / "iteration-E3" / "cases"),
            "failed_case_paths": ["iteration-E3/cases/case_1.json"],
            "suggested_greps": ["grep -l '\"pass\": false' iteration-E*/cases/*.json"],
            "current_best_metric": 0.8,
            "stuck": False,
        }
        prompt = isolation.build_diagnoser_prompt(
            self.skill_path, self.workspace, review, self.gt_path, "body")
        self.assertNotIn("holdout", prompt.lower())

    def test_cases_dir_pointing_at_holdout_is_dropped(self):
        review = {
            "cases_dir": str(self.workspace / "evolve" / "iteration-E3" / "holdout_cases"),
        }
        prompt = isolation.build_diagnoser_prompt(
            self.skill_path, self.workspace, review, self.gt_path, "body")
        self.assertNotIn("holdout", prompt.lower())

    def test_failed_case_paths_mentioning_holdout_are_filtered(self):
        review = {
            "failed_case_paths": [
                "iteration-E3/cases/case_1.json",
                "iteration-E3/holdout_cases/case_5.json",
            ],
        }
        prompt = isolation.build_diagnoser_prompt(
            self.skill_path, self.workspace, review, self.gt_path, "body")
        self.assertNotIn("holdout", prompt.lower())
        # The legitimate dev path must still survive the filter.
        self.assertIn("case_1.json", prompt)

    def test_suggested_greps_mentioning_holdout_are_filtered(self):
        review = {
            "suggested_greps": [
                "grep -l 'fail' iteration-E*/cases/*.json",
                "grep -l 'fail' iteration-E*/holdout_cases/*.json",
            ],
        }
        prompt = isolation.build_diagnoser_prompt(
            self.skill_path, self.workspace, review, self.gt_path, "body")
        self.assertNotIn("holdout", prompt.lower())

    def test_past_diagnoses_mentioning_holdout_are_filtered(self):
        review = {
            "past_diagnoses": [
                "iteration 2: fixed contains check",
                "iteration 4: noticed holdout regression but ignored it",
            ],
        }
        prompt = isolation.build_diagnoser_prompt(
            self.skill_path, self.workspace, review, self.gt_path, "body")
        self.assertNotIn("holdout", prompt.lower())

    def test_empty_review_still_produces_valid_prompt(self):
        prompt = isolation.build_diagnoser_prompt(
            self.skill_path, self.workspace, {}, self.gt_path, "body")
        self.assertNotIn("holdout", prompt.lower())
        self.assertIn("DIAGNOSING", prompt)

    def test_prompt_instructs_no_modification(self):
        prompt = isolation.build_diagnoser_prompt(
            self.skill_path, self.workspace, {}, self.gt_path, "body")
        self.assertIn("Do NOT edit any files", prompt)

    def test_prompt_references_current_layer(self):
        prompt = isolation.build_diagnoser_prompt(
            self.skill_path, self.workspace, {}, self.gt_path, "script")
        self.assertIn("Current layer: script", prompt)


if __name__ == "__main__":
    unittest.main()
