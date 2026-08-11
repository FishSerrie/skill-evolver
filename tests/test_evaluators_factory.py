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


evaluators = _load_module("skill_evolver_evaluators", SCRIPTS_DIR / "evaluators.py")


class GetEvaluatorBehavioralTests(unittest.TestCase):
    def test_behavioral_name_is_registered(self):
        self.assertIn("behavioral", evaluators.EVALUATOR_NAMES)

    def test_get_evaluator_behavioral_returns_behavioral_evaluator(self):
        ev = evaluators.get_evaluator({"evaluator": "behavioral"})
        self.assertEqual(ev.name, "behavioral")
        self.assertEqual(ev.sample_size, 8)  # default
        self.assertEqual(ev.fidelity, "assume_loaded")  # default

    def test_get_evaluator_behavioral_respects_config_overrides(self):
        ev = evaluators.get_evaluator({
            "evaluator": "behavioral",
            "behavioral_sample_size": 4,
            "behavioral_backend": "claude",
            "behavioral_fidelity": "assume_loaded",
            "evaluator_timeout": 90,
            "model": "claude-sonnet-4-6",
        })
        self.assertEqual(ev.sample_size, 4)
        self.assertEqual(ev.backend, "claude")
        self.assertEqual(ev.timeout, 90)
        self.assertEqual(ev.model, "claude-sonnet-4-6")

    def test_local_evaluator_still_works_unaffected(self):
        ev = evaluators.get_evaluator({"evaluator": "local"})
        self.assertEqual(ev.name, "local")

    def test_unknown_evaluator_still_raises(self):
        with self.assertRaises(ValueError):
            evaluators.get_evaluator({"evaluator": "nonexistent"})


class ParseEvaluatorFromPlanBehavioralTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _write_plan(self, text: str) -> Path:
        plan_path = self.tmp / "evolve_plan.md"
        plan_path.write_text(text)
        return plan_path

    def test_parses_behavioral_config_keys(self):
        plan_path = self._write_plan(
            "- evaluator: behavioral\n"
            "- behavioral_sample_size: 6\n"
            "- behavioral_backend: claude\n"
            "- behavioral_fidelity: assume_loaded\n"
        )
        config = evaluators.parse_evaluator_from_plan(plan_path)
        self.assertEqual(config["evaluator"], "behavioral")
        self.assertEqual(config["behavioral_sample_size"], 6)
        self.assertEqual(config["behavioral_backend"], "claude")
        self.assertEqual(config["behavioral_fidelity"], "assume_loaded")

    def test_missing_plan_file_returns_empty_config(self):
        config = evaluators.parse_evaluator_from_plan(self.tmp / "no_such_file.md")
        self.assertEqual(config, {})

    def test_non_integer_sample_size_is_ignored_not_raised(self):
        plan_path = self._write_plan("- behavioral_sample_size: not-a-number\n")
        config = evaluators.parse_evaluator_from_plan(plan_path)
        self.assertNotIn("behavioral_sample_size", config)

    def test_end_to_end_plan_to_evaluator(self):
        plan_path = self._write_plan(
            "- evaluator: behavioral\n"
            "- behavioral_sample_size: 5\n"
        )
        config = evaluators.parse_evaluator_from_plan(plan_path)
        ev = evaluators.get_evaluator(config)
        self.assertEqual(ev.name, "behavioral")
        self.assertEqual(ev.sample_size, 5)


if __name__ == "__main__":
    unittest.main()
