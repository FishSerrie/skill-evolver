"""Evaluating without a transport of one's own.

This engine ships as a Skill. Whoever installs it has their own agent's
model and nothing else: no `claude` binary on PATH, no HTTP endpoint, no
credentials. `SKILL.md` says so — CLI mode is the fallback, the primary
path is in the conversation — and these tests are what make that true of
the grader path rather than merely intended.

The failure they guard against is not a crash. It is that the path works
perfectly for whoever wrote it, on a machine that happens to have a CLI
installed and authenticated, and is dead on arrival for everyone else.
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugin/skills/skill-evolver/scripts"
sys.path.insert(0, str(SCRIPTS))

from datasets import ColumnMap  # noqa: E402
from grader_evaluator import GraderEvaluator  # noqa: E402
from graders import ProgrammaticGrader  # noqa: E402


def _fixture(cases: list[dict]) -> tuple[Path, Path]:
    """A prompt file and a GT file, in a fresh directory."""
    root = Path(tempfile.mkdtemp())
    prompt = root / "prompt.md"
    prompt.write_text("Answer using only the facts you are given.\n")
    gt = root / "gt.json"
    gt.write_text(json.dumps(cases))
    return prompt, gt


COLUMNS = ColumnMap(id="id", input=("input",),
                    expectations="expectations", split="split")


def _evaluator() -> GraderEvaluator:
    """An evaluator with no runner at all.

    ``runner=None`` is the point: in conversation mode nothing in this
    process reaches a model, so there is no runner to supply. A path that
    needed one would fail here rather than in front of a user.
    """
    return GraderEvaluator(grader=ProgrammaticGrader(), runner=None,
                           columns=COLUMNS)


class AFullRoundNeedsNoTransport(unittest.TestCase):
    """Stage 1 → driver → stage 2, with nothing installed."""

    def test_a_round_completes_and_scores(self):
        prompt, gt = _fixture([
            {"id": "c1", "input": "2+2?", "split": "dev",
             "expectations": [{"type": "contains", "value": "4"}]},
            {"id": "c2", "input": "Capital of France?", "split": "dev",
             "expectations": [{"type": "contains", "value": "Paris"}]},
        ])
        evaluator = _evaluator()

        staged = evaluator.build_run_specs(prompt, gt, split="dev")
        self.assertEqual([s["case_id"] for s in staged["run_specs"]],
                         ["c1", "c2"])
        # The prompt must carry both the candidate and the case, or the
        # driver has nothing to answer from.
        first = staged["run_specs"][0]["prompt"]
        self.assertIn("Answer using only the facts", first)
        self.assertIn("2+2?", first)

        # The driver answers. In a real run this is an Agent call; here it
        # is a dict, which is the whole advantage of the split.
        result = evaluator.grade_outputs(
            staged, {"c1": "The answer is 4.", "c2": "It is Paris."})

        self.assertEqual(result["errored"], 0)
        self.assertEqual(result["pass_rate"], 1.0)
        self.assertEqual(result["total_passed"], 2)

    def test_a_wrong_answer_scores_zero_rather_than_erroring(self):
        """Grading still discriminates; the mode changes plumbing only."""
        prompt, gt = _fixture([
            {"id": "c1", "input": "2+2?", "split": "dev",
             "expectations": [{"type": "contains", "value": "4"}]},
        ])
        evaluator = _evaluator()
        staged = evaluator.build_run_specs(prompt, gt, split="dev")
        result = evaluator.grade_outputs(staged, {"c1": "I would rather not."})
        self.assertEqual(result["errored"], 0)
        self.assertEqual(result["pass_rate"], 0.0)

    def test_the_result_has_the_shape_the_gate_reads(self):
        """The gate must not care which mode produced its numbers."""
        prompt, gt = _fixture([
            {"id": "c1", "input": "q", "split": "dev",
             "expectations": [{"type": "contains", "value": "a"}]},
        ])
        evaluator = _evaluator()
        staged = evaluator.build_run_specs(prompt, gt, split="dev")
        result = evaluator.grade_outputs(staged, {"c1": "a"})
        for key in ("pass_rate", "total_passed", "total_assertions", "failed",
                    "tokens", "duration", "cases", "metrics", "snapshot"):
            self.assertIn(key, result, key)
        self.assertTrue(result["snapshot"], "the gate needs a real snapshot")


class AMissingReplyIsNotAZero(unittest.TestCase):
    """A case the driver could not answer was not measured.

    Scoring it zero would blame the candidate for the harness — the same
    rule the CLI path already follows for a transport failure. It matters
    more here, because in conversation mode a dropped Agent call is an
    ordinary event rather than an exception someone will see.
    """

    def test_an_absent_case_id_is_recorded_as_unmeasured(self):
        prompt, gt = _fixture([
            {"id": "c1", "input": "q1", "split": "dev",
             "expectations": [{"type": "contains", "value": "a"}]},
            {"id": "c2", "input": "q2", "split": "dev",
             "expectations": [{"type": "contains", "value": "b"}]},
        ])
        evaluator = _evaluator()
        staged = evaluator.build_run_specs(prompt, gt, split="dev")

        result = evaluator.grade_outputs(staged, {"c1": "a"})

        self.assertEqual(result["errored"], 1)
        self.assertEqual([e["id"] for e in result["errors"]], ["c2"])
        # The measured case still scores, and the unmeasured one is kept out
        # of the average rather than dragging it toward zero.
        self.assertEqual(result["pass_rate"], 1.0)


class CaseKeysSurviveAMissingId(unittest.TestCase):
    """Two cases without an id must not collapse into one.

    They previously would have: both answered to the same fallback key, so
    the second replaced the first in the mapping. The run then measured
    fewer cases than it staged, while every count downstream stayed
    internally consistent — a silent loss.
    """

    def test_two_unidentified_cases_stay_distinct(self):
        prompt, gt = _fixture([
            {"input": "q1", "split": "dev",
             "expectations": [{"type": "contains", "value": "a"}]},
            {"input": "q2", "split": "dev",
             "expectations": [{"type": "contains", "value": "b"}]},
        ])
        evaluator = _evaluator()
        staged = evaluator.build_run_specs(prompt, gt, split="dev")

        self.assertEqual(len(staged["cases_by_id"]), 2)
        self.assertEqual(len(staged["run_specs"]), 2)
        keys = [s["case_id"] for s in staged["run_specs"]]
        self.assertEqual(len(set(keys)), 2, f"keys collapsed: {keys}")


class StagedStateIsCarriedNotReRead(unittest.TestCase):
    """Stage 2 must not re-read the artifact.

    The loop rewrites the candidate between rounds. A stage 2 that went
    back to disk would score replies produced by the old text against a
    snapshot of the new one, and report the pair as one measurement.
    """

    def test_the_snapshot_is_the_one_taken_at_stage_one(self):
        prompt, gt = _fixture([
            {"id": "c1", "input": "q", "split": "dev",
             "expectations": [{"type": "contains", "value": "a"}]},
        ])
        evaluator = _evaluator()
        staged = evaluator.build_run_specs(prompt, gt, split="dev")
        before = staged["snapshot"]["chars"]

        # Someone rewrites the candidate, at length, mid-round.
        prompt.write_text("x" * (before * 5))

        result = evaluator.grade_outputs(staged, {"c1": "a"})
        self.assertEqual(
            result["snapshot"]["chars"], before,
            "stage 2 re-read the artifact instead of using the staged "
            "snapshot, so this round mixes two versions",
        )


class TheConversationPathTouchesNoTransport(unittest.TestCase):
    """I14 — neither stage may reach a model.

    Checked statically, because the runtime symptom is absence: on the
    author's machine a CLI is installed and authenticated, so a stray call
    works and the test passes. It fails only for the person who installed
    the Skill, which is exactly who cannot report it.
    """

    def test_neither_stage_calls_the_llm_layer(self):
        tree = ast.parse((SCRIPTS / "grader_evaluator.py").read_text())
        targets = {"build_run_specs", "grade_outputs", "_render_run_prompt",
                   "_case_key"}
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in targets:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.ImportFrom) and inner.module == "llm":
                    offenders.append(f"{node.name} imports llm@{inner.lineno}")
                if isinstance(inner, ast.Call):
                    called = ast.unparse(inner.func)
                    if "_call_llm" in called or "subprocess" in called:
                        offenders.append(f"{node.name} calls {called}@{inner.lineno}")
        self.assertEqual(
            offenders, [],
            "conversation mode must leave model calls to the driver:\n  "
            + "\n  ".join(offenders),
        )

    def test_the_round_runs_with_the_llm_module_unimportable(self):
        """The strongest form of the check: break the import and retry.

        Passing this proves the round needs no transport, rather than
        happening not to use one on this machine.
        """
        import builtins

        prompt, gt = _fixture([
            {"id": "c1", "input": "q", "split": "dev",
             "expectations": [{"type": "contains", "value": "a"}]},
        ])
        evaluator = _evaluator()

        real_import = builtins.__import__

        def refuse_llm(name, *args, **kwargs):
            if name == "llm":
                raise ImportError("no transport available in this environment")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = refuse_llm
        try:
            staged = evaluator.build_run_specs(prompt, gt, split="dev")
            result = evaluator.grade_outputs(staged, {"c1": "a"})
        finally:
            builtins.__import__ = real_import

        self.assertEqual(result["errored"], 0)
        self.assertEqual(result["pass_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
