import importlib.util
import subprocess
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


orchestrator = _load_module("skill_evolver_orchestrator", SCRIPTS_DIR / "orchestrator.py")


def _run_git(args, cwd):
    subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com",
             "PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
    )


class _FakeEvaluator:
    """Scripted (pass_rate, tokens) sequence. Call order in
    run_evolve_loop: baseline dev, baseline holdout, then per-iteration
    dev, holdout — holdout entries reuse the same tokens value, only
    pass_rate matters for them."""

    def __init__(self, sequence):
        self._queue = list(sequence)

    def quick_gate(self, skill_path, gt_path=None):
        return {"pass": True, "errors": []}

    def full_eval(self, skill_path, gt_path, split="dev", cases_dir=None):
        pass_rate, tokens = self._queue.pop(0) if self._queue else self._queue[-1]
        return {"pass_rate": pass_rate, "total_passed": 1, "total_assertions": 1,
                "tokens": tokens, "duration": 0.0, "cases": []}

    def info(self):
        return {"name": "fake", "type": "_FakeEvaluator"}


class CostBaselineTracksCurrentBestTests(unittest.TestCase):
    """Real bug found via adversarial review: cost_mean/duration_mean
    baseline stayed frozen at whatever Phase 0 measured, forever, even
    though best_rate/best_holdout tracked the current best version on
    every keep — violating gate_rules.md's own contract ("baseline:
    evaluation results for the CURRENT BEST version") for 2 of 5 gate
    dimensions. A candidate that's a modest cost increase over the
    LATEST kept version could get incorrectly cost-failed just because
    it looked expensive relative to iteration 0."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.skill_path = root / "skill"
        self.skill_path.mkdir(parents=True)
        self.workspace = root / "workspace"
        (self.workspace / "evolve").mkdir(parents=True)
        self.gt_path = root / "evals.json"
        self.gt_path.write_text("{}")

        (self.skill_path / "SKILL.md").write_text("v0\n")
        _run_git(["init"], self.skill_path)
        _run_git(["add", "."], self.skill_path)
        _run_git(["commit", "-m", "init"], self.skill_path)

        self._n = 0
        self._patches = [
            mock.patch.object(orchestrator, "find_creator_path", return_value=Path("/fake/creator")),
            mock.patch.object(orchestrator, "phase_0_setup", return_value={
                "workspace": str(self.workspace),
                "evolve_dir": str(self.workspace / "evolve"),
            }),
            mock.patch.object(orchestrator, "phase_2_diagnose", return_value={
                "failure_patterns": [], "recommended_focus": "", "layer_suggestion": "body",
                "evidence_refs": [],
            }),
            mock.patch.object(orchestrator, "phase_3_modify", side_effect=self._fake_modify),
            mock.patch.object(orchestrator, "phase_6_5_review", return_value={
                "decision": "pass", "verdicts": [], "reasoning": "clean"}),
            mock.patch.object(orchestrator, "_prepare_viewer_data", return_value=None),
            mock.patch.object(orchestrator, "_try_launch_eval_viewer", return_value=False),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _fake_modify(self, skill_path, diagnosis, current_layer, model=None):
        self._n += 1
        (self.skill_path / "SKILL.md").write_text(f"v{self._n}\n")
        return {"changed": True, "description": f"mutation {self._n}"}

    def test_second_iteration_cost_gate_compares_against_latest_keep_not_phase0(self):
        # baseline dev tokens=90; iter1 tokens=100 (1.11x baseline, within
        # the 1.2x cap) -> keep, best_tokens should become 100. iter2
        # tokens=110 is 1.1x the NEW best_tokens of 100 (within cap) but
        # 1.22x the ORIGINAL baseline of 90 -- if the bug were still
        # present (comparing against the frozen Phase-0 baseline forever),
        # this would incorrectly cost-FAIL. With the fix, iter2 keeps.
        evaluator = _FakeEvaluator([
            (0.5, 90),   # baseline dev
            (0.5, 90),   # baseline holdout
            (0.8, 100),  # iter1 dev
            (0.8, 100),  # iter1 holdout
            (0.9, 110),  # iter2 dev
            (0.9, 110),  # iter2 holdout
            (0.9, 110),  # final holdout
        ])
        result = orchestrator.run_evolve_loop(
            self.skill_path, self.gt_path, self.workspace,
            max_iterations=2, evaluator=evaluator, verbose=False,
        )
        self.assertEqual(result["best_rate"], 0.9)
        self.assertEqual((self.skill_path / "SKILL.md").read_text(), "v2\n")

    def test_bug_repro_without_fix_would_have_cost_failed_iteration_two(self):
        # Direct proof the OLD behavior (comparing against the frozen
        # Phase-0 baseline) would have discarded iter2: 110 tokens vs a
        # Phase-0 baseline of 90 is a 1.22x increase, over the 1.2x cap.
        from gate import phase_6_gate_decision
        old_baseline_style = phase_6_gate_decision(
            {"pass_rate": 0.9, "holdout_pass_rate": 0.9, "l1_pass": True,
             "trigger_f1": 1.0, "tokens_mean": 110, "duration_mean": 0.0,
             "regression_pass": 1.0},
            {"pass_rate": 0.8, "holdout_pass_rate": 0.8, "trigger_f1": 1.0,
             "tokens_mean": 90, "duration_mean": 0.0, "regression_pass": 1.0},
            {"min_delta": 0.01, "noise_threshold": 0.01},
        )
        self.assertEqual(old_baseline_style["decision"], "discard")
        self.assertTrue(any("cost FAIL" in r for r in old_baseline_style["reasons"]))

        new_baseline_style = phase_6_gate_decision(
            {"pass_rate": 0.9, "holdout_pass_rate": 0.9, "l1_pass": True,
             "trigger_f1": 1.0, "tokens_mean": 110, "duration_mean": 0.0,
             "regression_pass": 1.0},
            {"pass_rate": 0.8, "holdout_pass_rate": 0.8, "trigger_f1": 1.0,
             "tokens_mean": 100, "duration_mean": 0.0, "regression_pass": 1.0},
            {"min_delta": 0.01, "noise_threshold": 0.01},
        )
        self.assertEqual(new_baseline_style["decision"], "keep")


if __name__ == "__main__":
    unittest.main()
