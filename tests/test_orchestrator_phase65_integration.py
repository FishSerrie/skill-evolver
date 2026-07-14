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
    """Returns a scripted sequence of full_eval results — call order in
    run_evolve_loop is: baseline dev, baseline holdout, iteration dev,
    iteration holdout, (final) holdout."""

    def __init__(self, dev_holdout_sequence):
        self._queue = list(dev_holdout_sequence)

    def quick_gate(self, skill_path, gt_path=None):
        return {"pass": True, "errors": []}

    def full_eval(self, skill_path, gt_path, split="dev", cases_dir=None):
        result = self._queue.pop(0) if self._queue else self._queue[-1]
        return {"pass_rate": result[0], "total_passed": 1, "total_assertions": 1,
                "tokens": 0, "duration": 0.0, "cases": []}

    def info(self):
        return {"name": "fake", "type": "_FakeEvaluator"}


class Phase65IntegrationTests(unittest.TestCase):
    """Real bug found via adversarial review: the actual point of
    Module D — the reject-overrides-decision / skip-when-not-keep
    control flow inserted into run_evolve_loop — had zero test
    coverage. Only _git_diff_for_commit (a helper) was tested. These
    tests drive the REAL run_evolve_loop through one real iteration
    (real git commit/revert, real phase_6_gate_decision) with only the
    expensive/external pieces (creator, evaluator, LLM calls, viewer)
    mocked."""

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

        (self.skill_path / "SKILL.md").write_text("# Test\noriginal\n")
        _run_git(["init"], self.skill_path)
        _run_git(["add", "."], self.skill_path)
        _run_git(["commit", "-m", "init"], self.skill_path)

        self._patches = [
            mock.patch.object(orchestrator, "require_creator", return_value=Path("/fake/creator")),
            mock.patch.object(orchestrator, "phase_0_setup", return_value={
                "workspace": str(self.workspace),
                "evolve_dir": str(self.workspace / "evolve"),
            }),
            mock.patch.object(orchestrator, "phase_2_diagnose", return_value={
                "failure_patterns": [], "recommended_focus": "", "layer_suggestion": "body",
                "evidence_refs": [],
            }),
            mock.patch.object(orchestrator, "phase_3_modify", side_effect=self._fake_modify),
            mock.patch.object(orchestrator, "_prepare_viewer_data", return_value=None),
            mock.patch.object(orchestrator, "_try_launch_eval_viewer", return_value=False),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _fake_modify(self, skill_path, diagnosis, current_layer, model=None):
        (self.skill_path / "SKILL.md").write_text("# Test\nmodified\n")
        return {"changed": True, "description": "test mutation"}

    def _run_one_iteration(self, dev_holdout_sequence, adversarial_side_effect):
        evaluator = _FakeEvaluator(dev_holdout_sequence)
        with mock.patch.object(orchestrator, "phase_6_5_review",
                               side_effect=adversarial_side_effect) as mocked_review:
            result = orchestrator.run_evolve_loop(
                self.skill_path, self.gt_path, self.workspace,
                max_iterations=1, evaluator=evaluator, verbose=False,
            )
        return result, mocked_review

    def test_panel_reject_overrides_keep_to_discard(self):
        # baseline dev=0.5, baseline holdout=0.5, iter dev=0.8 (keep-worthy
        # per numeric gate alone), iter holdout=0.8, final holdout=0.8
        result, mocked_review = self._run_one_iteration(
            [(0.5,), (0.5,), (0.8,), (0.8,), (0.8,)],
            adversarial_side_effect=lambda *a, **k: {
                "decision": "reject", "verdicts": [], "reasoning": "gamed"},
        )
        mocked_review.assert_called_once()
        # best_rate must NOT have been updated to the discarded candidate
        self.assertEqual(result["best_rate"], 0.5)
        # the mutation must have been reverted (working tree back to original)
        self.assertEqual((self.skill_path / "SKILL.md").read_text(), "# Test\noriginal\n")

    def test_panel_pass_keeps_the_keep_decision(self):
        result, mocked_review = self._run_one_iteration(
            [(0.5,), (0.5,), (0.8,), (0.8,), (0.8,)],
            adversarial_side_effect=lambda *a, **k: {
                "decision": "pass", "verdicts": [], "reasoning": "clean"},
        )
        mocked_review.assert_called_once()
        self.assertEqual(result["best_rate"], 0.8)
        self.assertEqual((self.skill_path / "SKILL.md").read_text(), "# Test\nmodified\n")

    def test_panel_skipped_falls_back_to_numeric_gate_keep(self):
        result, mocked_review = self._run_one_iteration(
            [(0.5,), (0.5,), (0.8,), (0.8,), (0.8,)],
            adversarial_side_effect=lambda *a, **k: {
                "decision": "skipped", "verdicts": [], "reasoning": ">=2 errors"},
        )
        mocked_review.assert_called_once()
        self.assertEqual(result["best_rate"], 0.8)

    def test_panel_never_invoked_when_numeric_gate_already_discards(self):
        # iter dev=0.5 == baseline dev=0.5 -> no improvement -> numeric
        # gate discards on its own; Phase 6.5 must not spend a single call.
        result, mocked_review = self._run_one_iteration(
            [(0.5,), (0.5,), (0.5,), (0.5,), (0.5,)],
            adversarial_side_effect=lambda *a, **k: {
                "decision": "pass", "verdicts": [], "reasoning": "should never run"},
        )
        mocked_review.assert_not_called()
        self.assertEqual(result["best_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
