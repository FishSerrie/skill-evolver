import importlib.util
import json
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


evolve_loop = _load_module("skill_evolver_evolve_loop", SCRIPTS_DIR / "evolve_loop.py")
orchestrator = _load_module("skill_evolver_orchestrator", SCRIPTS_DIR / "orchestrator.py")


class PersistHoldoutCasesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)

    def test_writes_to_holdout_cases_not_cases(self):
        cases = [{"case_id": "h1", "assertions": []}]
        result_dir = evolve_loop.persist_holdout_cases(self.workspace, 3, cases)

        expected_dir = self.workspace / "evolve" / "iteration-E3" / "holdout_cases"
        self.assertEqual(result_dir, expected_dir)
        self.assertTrue(expected_dir.exists())
        # Sibling cases/ dir must NOT be created by this call — that's
        # the dev-split directory, a different function's job.
        sibling = self.workspace / "evolve" / "iteration-E3" / "cases"
        self.assertFalse(sibling.exists())

    def test_empty_cases_returns_none_and_creates_nothing(self):
        result = evolve_loop.persist_holdout_cases(self.workspace, 3, [])
        self.assertIsNone(result)
        self.assertFalse((self.workspace / "evolve").exists())

    def test_none_cases_returns_none(self):
        self.assertIsNone(evolve_loop.persist_holdout_cases(self.workspace, 3, None))

    def test_written_file_content_matches_input(self):
        cases = [{"case_id": "h1", "assertions": [], "prompt": "test prompt"}]
        evolve_loop.persist_holdout_cases(self.workspace, 5, cases)

        holdout_dir = self.workspace / "evolve" / "iteration-E5" / "holdout_cases"
        written_files = list(holdout_dir.glob("*.json"))
        self.assertEqual(len(written_files), 1)
        content = json.loads(written_files[0].read_text())
        self.assertEqual(content["prompt"], "test prompt")


class EvalHoldoutOrNoneTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.skill_path = self.workspace / "skill"
        self.gt_path = self.workspace / "evals.json"

    class _FakeEvaluator:
        def __init__(self, result):
            self._result = result

        def full_eval(self, skill_path, gt_path, split="dev"):
            if isinstance(self._result, Exception):
                raise self._result
            return self._result

    def test_persists_cases_when_workspace_and_iteration_given(self):
        cases = [{"case_id": "h1", "assertions": []}]
        ev = self._FakeEvaluator({"pass_rate": 0.75, "total_assertions": 2, "cases": cases})

        rate = orchestrator._eval_holdout_or_none(
            ev, self.skill_path, self.gt_path,
            workspace=self.workspace, iteration=7)

        self.assertEqual(rate, 0.75)
        holdout_dir = self.workspace / "evolve" / "iteration-E7" / "holdout_cases"
        self.assertTrue(holdout_dir.exists())
        self.assertEqual(len(list(holdout_dir.glob("*.json"))), 1)

    def test_does_not_persist_when_workspace_or_iteration_missing(self):
        cases = [{"case_id": "h1", "assertions": []}]
        ev = self._FakeEvaluator({"pass_rate": 0.75, "total_assertions": 2, "cases": cases})

        # Old call signature — no workspace/iteration — must still work,
        # and must NOT create any holdout_cases directory (backward
        # compatible with any caller that only wants the pass rate).
        rate = orchestrator._eval_holdout_or_none(ev, self.skill_path, self.gt_path)

        self.assertEqual(rate, 0.75)
        self.assertFalse((self.workspace / "evolve").exists())

    def test_returns_none_when_evaluator_raises(self):
        ev = self._FakeEvaluator(RuntimeError("no holdout split configured"))
        rate = orchestrator._eval_holdout_or_none(
            ev, self.skill_path, self.gt_path,
            workspace=self.workspace, iteration=1)
        self.assertIsNone(rate)
        self.assertFalse((self.workspace / "evolve").exists())

    def test_returns_none_when_zero_assertions_and_does_not_persist(self):
        ev = self._FakeEvaluator({"pass_rate": 0.0, "total_assertions": 0, "cases": []})
        rate = orchestrator._eval_holdout_or_none(
            ev, self.skill_path, self.gt_path,
            workspace=self.workspace, iteration=1)
        self.assertIsNone(rate)
        self.assertFalse((self.workspace / "evolve").exists())


if __name__ == "__main__":
    unittest.main()
