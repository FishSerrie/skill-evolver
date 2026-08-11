import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "plugin" / "skills" / "skill-evolver" / "scripts"

# Put scripts/ on sys.path *before* loading evaluator_backends so that
# module's own top-level ``from common import ...`` / ``from evaluators
# import ...`` / lazy ``from behavioral_runner import ...`` resolve via
# the real plain-import machinery (sys.modules-cached, one instance per
# name) — that's what lets mock.patch on ``behavioral_runner`` below
# actually intercept the call evaluator_backends.py makes internally.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


eb = _load_module("skill_evolver_evaluator_backends",
                   SCRIPTS_DIR / "evaluator_backends.py")

import behavioral_runner  # noqa: E402 — real module, same instance eb's lazy imports reuse


def _write_skill(tmp: Path, skill_md: str = "# Test Skill\n\nDo the thing.\n") -> Path:
    skill_path = tmp / "test-skill"
    skill_path.mkdir(parents=True, exist_ok=True)
    (skill_path / "SKILL.md").write_text(skill_md)
    return skill_path


def _write_evals(tmp: Path, evals: list[dict]) -> Path:
    gt_path = tmp / "evals" / "evals.json"
    gt_path.parent.mkdir(parents=True, exist_ok=True)
    gt_path.write_text(json.dumps({"evals": evals}))
    return gt_path


class BehavioralEvaluatorFullEvalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.skill_path = _write_skill(self.tmp)

    def test_scores_against_transcript_not_static_doc(self):
        # Assertion looks for a string that is NOT in SKILL.md but WILL
        # be in the (fake) transcript — proves content came from the
        # transcript, not _load_skill_corpus().
        gt_path = _write_evals(self.tmp, [
            {"id": "c1", "split": "dev", "prompt": "hello",
             "assertions": [{"type": "contains", "value": "MAGIC_WORD_42"}]},
        ])

        def fake_run(skill_path, case, **kwargs):
            return behavioral_runner.build_transcript_from_text(
                "response containing MAGIC_WORD_42 for sure",
                runner_backend="fake", isolation="subprocess",
                fidelity="assume_loaded")

        with mock.patch.object(behavioral_runner, "run_case_behaviorally",
                               side_effect=fake_run):
            evaluator = eb.BehavioralEvaluator(sample_size=8)
            result = evaluator.full_eval(self.skill_path, gt_path, split="dev")

        self.assertEqual(result["total_assertions"], 1)
        self.assertEqual(result["total_passed"], 1)
        self.assertEqual(result["pass_rate"], 1.0)
        self.assertEqual(len(result["cases"]), 1)
        self.assertIn("transcript", result["cases"][0])
        self.assertEqual(result["cases"][0]["transcript"]["output_text"],
                         "response containing MAGIC_WORD_42 for sure")

    def test_assertion_fails_when_transcript_lacks_expected_text(self):
        gt_path = _write_evals(self.tmp, [
            {"id": "c1", "split": "dev", "prompt": "hello",
             "assertions": [{"type": "contains", "value": "NEVER_APPEARS"}]},
        ])

        def fake_run(skill_path, case, **kwargs):
            return behavioral_runner.build_transcript_from_text(
                "a totally unrelated response",
                runner_backend="fake", isolation="subprocess",
                fidelity="assume_loaded")

        with mock.patch.object(behavioral_runner, "run_case_behaviorally",
                               side_effect=fake_run):
            evaluator = eb.BehavioralEvaluator(sample_size=8)
            result = evaluator.full_eval(self.skill_path, gt_path, split="dev")

        self.assertEqual(result["total_passed"], 0)
        self.assertEqual(len(result["failed"]), 1)

    def test_target_skill_doc_scores_against_static_corpus_not_transcript(self):
        # SKILL.md contains "references/foo.md"; the transcript does not.
        # A target="skill_doc" assertion should still pass because it's
        # checked against the static corpus, not the transcript.
        skill_path = _write_skill(
            self.tmp, "# Test Skill\n\nSee references/foo.md for details.\n")
        gt_path = _write_evals(self.tmp, [
            {"id": "c1", "split": "dev", "prompt": "hello",
             "assertions": [
                 {"type": "contains", "value": "references/foo.md",
                  "target": "skill_doc"},
                 {"type": "contains", "value": "references/foo.md",
                  "target": "output"},
             ]},
        ])

        def fake_run(skill_path, case, **kwargs):
            return behavioral_runner.build_transcript_from_text(
                "response that never mentions any file paths",
                runner_backend="fake", isolation="subprocess",
                fidelity="assume_loaded")

        with mock.patch.object(behavioral_runner, "run_case_behaviorally",
                               side_effect=fake_run):
            evaluator = eb.BehavioralEvaluator(sample_size=8)
            result = evaluator.full_eval(skill_path, gt_path, split="dev")

        assertions = result["cases"][0]["assertions"]
        skill_doc_assertion = next(a for a in assertions if a["target"] == "skill_doc")
        output_assertion = next(a for a in assertions if a["target"] == "output")
        self.assertTrue(skill_doc_assertion["pass"])
        self.assertFalse(output_assertion["pass"])

    def test_default_target_is_output(self):
        gt_path = _write_evals(self.tmp, [
            {"id": "c1", "split": "dev", "prompt": "hello",
             "assertions": [{"type": "contains", "value": "x"}]},  # no target key
        ])

        def fake_run(skill_path, case, **kwargs):
            return behavioral_runner.build_transcript_from_text(
                "x", runner_backend="fake", isolation="subprocess",
                fidelity="assume_loaded")

        with mock.patch.object(behavioral_runner, "run_case_behaviorally",
                               side_effect=fake_run):
            evaluator = eb.BehavioralEvaluator(sample_size=8)
            result = evaluator.full_eval(self.skill_path, gt_path, split="dev")

        self.assertEqual(result["cases"][0]["assertions"][0]["target"], "output")

    def test_only_dev_split_cases_are_run(self):
        gt_path = _write_evals(self.tmp, [
            {"id": "dev1", "split": "dev",
             "assertions": [{"type": "contains", "value": "x"}]},
            {"id": "holdout1", "split": "holdout",
             "assertions": [{"type": "contains", "value": "x"}]},
        ])
        seen_ids = []

        def fake_run(skill_path, case, **kwargs):
            seen_ids.append(case.get("id"))
            return behavioral_runner.build_transcript_from_text(
                "x", runner_backend="fake", isolation="subprocess",
                fidelity="assume_loaded")

        with mock.patch.object(behavioral_runner, "run_case_behaviorally",
                               side_effect=fake_run):
            evaluator = eb.BehavioralEvaluator(sample_size=8)
            evaluator.full_eval(self.skill_path, gt_path, split="dev")

        self.assertEqual(seen_ids, ["dev1"])

    def test_rotation_sampling_applies_when_more_cases_than_sample_size(self):
        gt_path = _write_evals(self.tmp, [
            {"id": f"c{i}", "split": "dev",
             "assertions": [{"type": "contains", "value": "x"}]}
            for i in range(5)
        ])
        seen_ids = []

        def fake_run(skill_path, case, **kwargs):
            seen_ids.append(case.get("id"))
            return behavioral_runner.build_transcript_from_text(
                "x", runner_backend="fake", isolation="subprocess",
                fidelity="assume_loaded")

        with mock.patch.object(behavioral_runner, "run_case_behaviorally",
                               side_effect=fake_run):
            evaluator = eb.BehavioralEvaluator(sample_size=2)
            evaluator.full_eval(self.skill_path, gt_path, split="dev")

        self.assertEqual(seen_ids, ["c0", "c1"])
        state_path = self.tmp / "evolve" / "behavioral_rotation.json"
        self.assertTrue(state_path.exists())

    def test_quick_gate_is_inherited_unchanged_from_local_evaluator(self):
        evaluator = eb.BehavioralEvaluator()
        self.assertIs(evaluator.quick_gate.__func__,
                      eb.LocalEvaluator.quick_gate)

    def test_info_reports_behavioral_config(self):
        evaluator = eb.BehavioralEvaluator(sample_size=4, fidelity="assume_loaded")
        info = evaluator.info()
        self.assertEqual(info["name"], "behavioral")
        self.assertEqual(info["sample_size"], 4)


class BehavioralEvaluatorTwoStageTests(unittest.TestCase):
    """Conversation-mode split: build_full_eval_specs() / finish_full_eval()."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.skill_path = _write_skill(self.tmp)

    def test_two_stage_round_trip_produces_same_shape_as_full_eval(self):
        gt_path = _write_evals(self.tmp, [
            {"id": "c1", "split": "dev", "prompt": "hello",
             "assertions": [{"type": "contains", "value": "OK"}]},
        ])
        evaluator = eb.BehavioralEvaluator(sample_size=8)

        staged = evaluator.build_full_eval_specs(self.skill_path, gt_path, split="dev")
        self.assertEqual(len(staged["specs"]), 1)
        self.assertEqual(staged["specs"][0]["case_id"], "c1")

        # Simulate the driving Claude issuing the Agent tool call itself.
        transcripts = {"c1": "the answer is OK, definitely"}
        result = evaluator.finish_full_eval(self.skill_path, staged, transcripts)

        self.assertEqual(result["total_passed"], 1)
        self.assertEqual(result["pass_rate"], 1.0)
        self.assertEqual(set(result.keys()),
                         {"pass_rate", "total_passed", "total_assertions",
                          "failed", "tokens", "duration", "cases"})

    def test_missing_transcript_for_a_case_fails_its_assertions_not_the_whole_round(self):
        gt_path = _write_evals(self.tmp, [
            {"id": "c1", "split": "dev",
             "assertions": [{"type": "contains", "value": "x"}]},
            {"id": "c2", "split": "dev",
             "assertions": [{"type": "contains", "value": "y"}]},
        ])
        evaluator = eb.BehavioralEvaluator(sample_size=8)
        staged = evaluator.build_full_eval_specs(self.skill_path, gt_path, split="dev")

        # c2's Agent call "never came back" — only c1 has a transcript.
        result = evaluator.finish_full_eval(self.skill_path, staged, {"c1": "x"})

        self.assertEqual(result["total_assertions"], 2)
        self.assertEqual(result["total_passed"], 1)
        statuses = {c["case_id"]: c["transcript"]["exit_status"] for c in result["cases"]}
        self.assertEqual(statuses["c1"], "ok")
        self.assertEqual(statuses["c2"], "error")


if __name__ == "__main__":
    unittest.main()
