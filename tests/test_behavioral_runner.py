import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "plugin" / "skills" / "skill-evolver" / "scripts"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


br = _load_module("skill_evolver_behavioral_runner",
                   SCRIPTS_DIR / "behavioral_runner.py")


def _cases(n: int) -> list[dict]:
    return [{"id": i} for i in range(n)]


class RotationSampleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "evolve" / "behavioral_rotation.json"

    def test_full_run_when_case_count_at_or_below_sample_size(self):
        cases = _cases(5)
        sample = br.get_rotation_sample(cases, 8, self.state_path, "dev")
        self.assertEqual(sample, cases)
        # No rotation state needed/touched when everything runs every round.
        self.assertFalse(self.state_path.exists())

    def test_full_run_when_case_count_equals_sample_size(self):
        cases = _cases(8)
        sample = br.get_rotation_sample(cases, 8, self.state_path, "dev")
        self.assertEqual(sample, cases)
        self.assertFalse(self.state_path.exists())

    def test_no_cases_returns_empty(self):
        self.assertEqual(br.get_rotation_sample([], 8, self.state_path, "dev"), [])

    def test_rotation_advances_and_wraps_around(self):
        cases = _cases(10)
        # Round 1: offset 0 -> [0,1,2], new offset 3
        s1 = br.get_rotation_sample(cases, 3, self.state_path, "dev")
        self.assertEqual([c["id"] for c in s1], [0, 1, 2])

        # Round 2: offset 3 -> [3,4,5], new offset 6
        s2 = br.get_rotation_sample(cases, 3, self.state_path, "dev")
        self.assertEqual([c["id"] for c in s2], [3, 4, 5])

        # Round 3: offset 6 -> [6,7,8], new offset 9
        s3 = br.get_rotation_sample(cases, 3, self.state_path, "dev")
        self.assertEqual([c["id"] for c in s3], [6, 7, 8])

        # Round 4: offset 9, window runs past the end -> wraps to [9,0,1]
        s4 = br.get_rotation_sample(cases, 3, self.state_path, "dev")
        self.assertEqual([c["id"] for c in s4], [9, 0, 1])

        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["dev"]["offset"], 2)

    def test_corrupt_state_file_restarts_from_offset_zero(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text("{not valid json::")

        cases = _cases(10)
        sample = br.get_rotation_sample(cases, 3, self.state_path, "dev")
        self.assertEqual([c["id"] for c in sample], [0, 1, 2])

    def test_state_with_out_of_range_offset_restarts_from_zero(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"dev": {"offset": 999}}))

        cases = _cases(10)
        sample = br.get_rotation_sample(cases, 3, self.state_path, "dev")
        self.assertEqual([c["id"] for c in sample], [0, 1, 2])

    def test_missing_state_file_starts_from_offset_zero(self):
        self.assertFalse(self.state_path.exists())
        cases = _cases(10)
        sample = br.get_rotation_sample(cases, 3, self.state_path, "dev")
        self.assertEqual([c["id"] for c in sample], [0, 1, 2])

    def test_per_split_offsets_are_independent(self):
        cases = _cases(10)

        dev1 = br.get_rotation_sample(cases, 3, self.state_path, "dev")
        self.assertEqual([c["id"] for c in dev1], [0, 1, 2])

        # holdout has never rotated before -> starts fresh at 0,
        # unaffected by dev's advanced offset.
        holdout1 = br.get_rotation_sample(cases, 3, self.state_path, "holdout")
        self.assertEqual([c["id"] for c in holdout1], [0, 1, 2])

        dev2 = br.get_rotation_sample(cases, 3, self.state_path, "dev")
        self.assertEqual([c["id"] for c in dev2], [3, 4, 5])

        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["dev"]["offset"], 6)
        self.assertEqual(state["holdout"]["offset"], 3)


if __name__ == "__main__":
    unittest.main()
