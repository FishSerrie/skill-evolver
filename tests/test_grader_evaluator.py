"""Tests for grader_evaluator — running the artifact and grading it.

Two things carry the weight here:

- **Regression protection.** The existing text-matching evaluator answers a
  different question and must keep working untouched. Several tests assert
  that, because "I only added code" is a claim, not a fact.
- **No type branching.** The evaluator holds a target, a runner and a
  grader, and sequences them. If it grew a branch on which one it got, the
  abstractions would be decorative.

No network calls: the runner is injected, so a stub replaces it.
"""

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).parent.parent / "plugin" / "skills" / "skill-evolver" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import evaluators  # noqa: E402
from datasets import ColumnMap  # noqa: E402
from grader_evaluator import (  # noqa: E402
    GraderEvaluator,
    PromptRunner,
    build_grader,
)
from graders import PointCoverageGrader, ProgrammaticGrader  # noqa: E402


class StubJudge:
    """Returns a canned coverage classification for every call.

    Exposes ``complete``, the raw-text channel, because a classification is
    structured output rather than a yes/no answer — the binary channel would
    consume its last line.
    """

    def __init__(self, reply_for=None, default=None):
        self.reply_for = reply_for or {}
        self.default = default or {"matched": [], "partial": [], "missed": [1]}
        self.total_tokens = 100
        self.total_duration = 0.5
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        for needle, reply in self.reply_for.items():
            if needle in prompt:
                return json.dumps(reply)
        return json.dumps(self.default)

    def judge_with_reasoning(self, question, context):
        self.calls += 1
        return True, "not used by the coverage grader"


class EchoRunner:
    """Returns whatever the case says it should, ignoring the instruction."""

    def __init__(self, outputs=None, fail_on=None):
        self.outputs = outputs or {}
        self.fail_on = set(fail_on or ())
        self.seen = []

    def __call__(self, instruction, case):
        self.seen.append((instruction, case))
        if case.get("id") in self.fail_on:
            raise RuntimeError("backend unavailable")
        return self.outputs.get(case.get("id"), case.get("input", ""))


class GraderEvaluatorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name, text):
        p = self.tmp / name
        p.write_text(text)
        return p

    def prompt_file(self, text="Answer the question accurately.\n"):
        return self.write("instruction.md", text)

    def assertions_gt(self):
        return self.write("gt.jsonl", "\n".join([
            json.dumps({"id": "c1", "q": "say alpha",
                        "asserts": [{"type": "contains", "value": "alpha"}]}),
            json.dumps({"id": "c2", "q": "say beta",
                        "asserts": [{"type": "contains", "value": "beta"}]}),
        ]) + "\n")

    def assertions_evaluator(self, runner, **kw):
        return GraderEvaluator(
            grader=ProgrammaticGrader(expectations_key="expectations"),
            runner=runner,
            columns=ColumnMap(input=("q",), expectations="asserts"),
            splits={"dev": 1.0},
            **kw,
        )


# ─────────────────────────────────────────────
# Regression: the existing evaluator is untouched
# ─────────────────────────────────────────────

class ExistingBehaviourTests(unittest.TestCase):
    def test_local_evaluator_is_still_the_named_default_path(self):
        ev = evaluators.get_evaluator({"evaluator": "local"})
        self.assertEqual(ev.name, "local")
        self.assertIsInstance(ev, evaluators.LocalEvaluator)

    def test_local_evaluator_still_scores_document_text(self):
        """Its question — "does the document say the right things" — remains."""
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "demo"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: A demo.\n---\n\nmentions alpha\n"
            )
            gt = Path(tmp) / "gt.json"
            gt.write_text(json.dumps({"evals": [{
                "id": "c1", "prompt": "p", "split": "dev",
                "assertions": [{"type": "contains", "value": "alpha"}],
            }]}))
            result = evaluators.LocalEvaluator().full_eval(skill, gt, "dev")
        self.assertEqual(result["pass_rate"], 1.0)
        self.assertEqual(result["total_assertions"], 1)

    def test_the_new_evaluator_returns_a_superset_of_the_old_keys(self):
        """Asserted on real return values, not on documentation.

        Anything downstream that reads pass_rate from one evaluator must
        find it in the other, or swapping evaluators would break the gate.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            skill = tmp / "demo"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: A demo.\n---\n\nmentions alpha\n"
            )
            old_gt = tmp / "old.json"
            old_gt.write_text(json.dumps({"evals": [{
                "id": "c1", "prompt": "p", "split": "dev",
                "assertions": [{"type": "contains", "value": "alpha"}],
            }]}))
            old = evaluators.LocalEvaluator().full_eval(skill, old_gt, "dev")

            new_gt = tmp / "new.jsonl"
            new_gt.write_text(json.dumps({
                "id": "c1", "q": "say alpha",
                "asserts": [{"type": "contains", "value": "alpha"}],
            }) + "\n")
            new = GraderEvaluator(
                grader=ProgrammaticGrader(expectations_key="expectations"),
                runner=EchoRunner(outputs={"c1": "alpha"}),
                columns=ColumnMap(input=("q",), expectations="asserts"),
                splits={"dev": 1.0},
            ).full_eval(skill, new_gt, "dev")

        self.assertTrue(
            set(old).issubset(set(new)),
            f"new evaluator is missing {set(old) - set(new)}",
        )

    def test_every_previously_available_name_still_resolves(self):
        for name in ("local", "creator", "script", "pytest", "behavioral"):
            self.assertIn(name, evaluators.EVALUATOR_NAMES)

    def test_an_unknown_name_still_raises_and_lists_the_options(self):
        with self.assertRaises(ValueError) as ctx:
            evaluators.get_evaluator({"evaluator": "telepathy"})
        self.assertIn("grader", str(ctx.exception))

    def test_importing_evaluators_does_not_pull_in_the_new_module(self):
        """Lazy import: nothing pays for a backend it did not ask for."""
        tree = ast.parse((SCRIPTS / "evaluators.py").read_text())
        toplevel = {
            node.module for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertNotIn("grader_evaluator", toplevel)
        self.assertNotIn("graders", toplevel)
        self.assertNotIn("datasets", toplevel)


# ─────────────────────────────────────────────
# The new path
# ─────────────────────────────────────────────

class FactoryTests(unittest.TestCase):
    def test_grader_is_a_registered_name(self):
        self.assertIn("grader", evaluators.EVALUATOR_NAMES)

    def test_the_factory_builds_it(self):
        ev = evaluators.get_evaluator({"evaluator": "grader", "grader": "assertions"})
        self.assertIsInstance(ev, GraderEvaluator)
        self.assertIsInstance(ev.grader, ProgrammaticGrader)

    def test_a_column_map_may_be_given_as_a_dict(self):
        """Config arrives from a plan file, where it is plain data."""
        ev = evaluators.get_evaluator({
            "evaluator": "grader", "grader": "assertions",
            "columns": {"input": ("q",), "expectations": "asserts"},
        })
        self.assertEqual(ev.columns.expectations, "asserts")

    def test_a_column_map_instance_is_passed_through(self):
        cols = ColumnMap(input=("q",))
        ev = evaluators.get_evaluator({
            "evaluator": "grader", "grader": "assertions", "columns": cols,
        })
        self.assertIs(ev.columns, cols)

    def test_a_custom_runner_overrides_the_default(self):
        runner = EchoRunner()
        ev = evaluators.get_evaluator({
            "evaluator": "grader", "grader": "assertions", "runner": runner,
        })
        self.assertIs(ev.runner, runner)

    def test_the_default_runner_is_the_prompt_runner(self):
        ev = evaluators.get_evaluator({"evaluator": "grader", "grader": "assertions"})
        self.assertIsInstance(ev.runner, PromptRunner)

    def test_build_grader_makes_a_point_coverage_grader(self):
        grader = build_grader({"grader": "points"})
        self.assertIsInstance(grader, PointCoverageGrader)

    def test_build_grader_reuses_the_engines_binary_judge(self):
        """Not a second client. One place decides how a model is reached."""
        from binary_judge import BinaryLLMJudge

        grader = build_grader({"grader": "points", "model": "some-model"})
        self.assertIsInstance(grader.judge, BinaryLLMJudge)
        self.assertEqual(grader.judge.model, "some-model")

    def test_a_separate_judge_model_can_be_named(self):
        """The judge need not be the model under optimization."""
        grader = build_grader({
            "grader": "points", "model": "candidate-model",
            "judge_model": "judge-model",
        })
        self.assertEqual(grader.judge.model, "judge-model")

    def test_shared_grader_options_are_forwarded(self):
        grader = build_grader({
            "grader": "points", "partial_weight": 0.25, "pass_threshold": 0.8,
        })
        self.assertEqual(grader.partial_weight, 0.25)
        self.assertEqual(grader.pass_threshold, 0.8)

    def test_an_unknown_grader_names_the_valid_ones(self):
        with self.assertRaises(ValueError) as ctx:
            build_grader({"grader": "vibes"})
        self.assertIn("points", str(ctx.exception))

    def test_field_names_are_configurable(self):
        a = build_grader({"grader": "assertions", "expectations_key": "checks"})
        b = build_grader({"grader": "points", "points_key": "must_cover"})
        self.assertEqual(a.expectations_key, "checks")
        self.assertEqual(b.points_key, "must_cover")


class FullEvalTests(GraderEvaluatorTestCase):
    def test_a_bare_prompt_file_can_be_evaluated(self):
        """The capability the whole abstraction was built for."""
        instruction = self.prompt_file()
        gt = self.assertions_gt()
        runner = EchoRunner(outputs={"c1": "alpha here", "c2": "beta here"})
        result = self.assertions_evaluator(runner).full_eval(instruction, gt, "dev")
        self.assertEqual(result["pass_rate"], 1.0)
        self.assertEqual(result["total_passed"], 2)

    def test_the_runner_receives_the_targets_context(self):
        instruction = self.prompt_file("BE PRECISE\n")
        gt = self.assertions_gt()
        runner = EchoRunner(outputs={"c1": "alpha", "c2": "beta"})
        self.assertions_evaluator(runner).full_eval(instruction, gt, "dev")
        self.assertIn("BE PRECISE", runner.seen[0][0])

    def test_a_failing_case_lowers_the_pass_rate(self):
        instruction = self.prompt_file()
        gt = self.assertions_gt()
        runner = EchoRunner(outputs={"c1": "alpha", "c2": "wrong"})
        result = self.assertions_evaluator(runner).full_eval(instruction, gt, "dev")
        self.assertEqual(result["pass_rate"], 0.5)
        self.assertEqual(result["failed"], ["c2"])

    def test_the_result_keeps_the_existing_contract_keys(self):
        """So the gate, the results log and the report keep working."""
        result = self.assertions_evaluator(EchoRunner()).full_eval(
            self.prompt_file(), self.assertions_gt(), "dev"
        )
        for key in ("pass_rate", "total_passed", "total_assertions",
                    "failed", "tokens", "duration", "cases"):
            self.assertIn(key, result)

    def test_multidimensional_metrics_are_added_alongside(self):
        """Never replacing pass_rate — a reader that only knows it must work."""
        result = self.assertions_evaluator(EchoRunner()).full_eval(
            self.prompt_file(), self.assertions_gt(), "dev"
        )
        self.assertIn("metrics", result)
        self.assertIn("recall", result["metrics"])
        self.assertIn("pass_rate", result)

    def test_the_primary_metric_comes_from_the_grader(self):
        """A grader with no observable precision must not be ranked on f1."""
        result = self.assertions_evaluator(EchoRunner()).full_eval(
            self.prompt_file(), self.assertions_gt(), "dev"
        )
        self.assertEqual(result["primary"], "recall")

    def test_per_case_records_carry_the_output_and_the_diagnosis(self):
        instruction = self.prompt_file()
        gt = self.assertions_gt()
        runner = EchoRunner(outputs={"c1": "alpha", "c2": "wrong"})
        result = self.assertions_evaluator(runner).full_eval(instruction, gt, "dev")
        failing = [c for c in result["cases"] if c["id"] == "c2"][0]
        self.assertEqual(failing["output"], "wrong")
        self.assertIn("beta", failing["feedback"])

    def test_per_case_records_keep_the_greppable_keys(self):
        """Existing tooling greps these files for '"pass": false'."""
        result = self.assertions_evaluator(EchoRunner()).full_eval(
            self.prompt_file(), self.assertions_gt(), "dev"
        )
        for key in ("id", "pass", "assertions"):
            self.assertIn(key, result["cases"][0])

    def test_the_result_carries_a_structural_snapshot(self):
        """The gate is a pure function, so it must be handed the numbers.

        Letting it measure the artifact itself would make its verdict depend
        on when it ran.
        """
        from gate import SNAPSHOT_KEYS

        result = self.assertions_evaluator(EchoRunner()).full_eval(
            self.prompt_file(), self.assertions_gt(), "dev"
        )
        self.assertIn("snapshot", result)
        for key in SNAPSHOT_KEYS:
            self.assertIn(key, result["snapshot"])

    def test_the_snapshot_and_the_gate_agree_end_to_end(self):
        """A grown candidate, measured by the evaluator, rejected by the gate."""
        from gate import check_structure

        small = self.assertions_evaluator(EchoRunner()).full_eval(
            self.prompt_file("short\n"), self.assertions_gt(), "dev"
        )
        big = self.assertions_evaluator(EchoRunner()).full_eval(
            self.prompt_file("padding " * 500), self.assertions_gt(), "dev"
        )
        ok, reasons = check_structure(big["snapshot"], small["snapshot"])
        self.assertFalse(ok, reasons)

    def test_cases_are_persisted_when_a_directory_is_given(self):
        out = self.tmp / "cases"
        self.assertions_evaluator(EchoRunner()).full_eval(
            self.prompt_file(), self.assertions_gt(), "dev", cases_dir=out
        )
        self.assertTrue(list(out.glob("*.json")))

    def test_a_section_of_a_file_can_be_the_target(self):
        instruction = self.write(
            "doc.md", "# Doc\n\n## Rules\n\nBe brief.\n\n## Notes\n\nignore me\n"
        )
        gt = self.assertions_gt()
        runner = EchoRunner(outputs={"c1": "alpha", "c2": "beta"})
        ev = self.assertions_evaluator(runner, section="Rules")
        ev.full_eval(instruction, gt, "dev")
        # context() for a section is the whole file, which is what shapes
        # the behaviour being judged.
        self.assertIn("Be brief.", runner.seen[0][0])

    def test_a_skill_directory_still_works_as_a_target(self):
        skill = self.tmp / "demo"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: demo\ndescription: A demo.\n---\n\nBe accurate.\n"
        )
        runner = EchoRunner(outputs={"c1": "alpha", "c2": "beta"})
        result = self.assertions_evaluator(runner).full_eval(
            skill, self.assertions_gt(), "dev"
        )
        self.assertEqual(result["pass_rate"], 1.0)
        self.assertIn("Be accurate.", runner.seen[0][0])


class RunnerFailureTests(GraderEvaluatorTestCase):
    def test_a_runner_failure_is_an_error_not_a_zero(self):
        """An empty output grades as a total miss and would libel the candidate."""
        runner = EchoRunner(outputs={"c1": "alpha"}, fail_on=["c2"])
        result = self.assertions_evaluator(runner).full_eval(
            self.prompt_file(), self.assertions_gt(), "dev"
        )
        self.assertEqual(result["errored"], 1)
        self.assertEqual(result["pass_rate"], 1.0, "errored case must not count")
        self.assertEqual(result["total_assertions"], 1)

    def test_the_error_is_reported_with_its_case_id(self):
        runner = EchoRunner(fail_on=["c2"])
        result = self.assertions_evaluator(runner).full_eval(
            self.prompt_file(), self.assertions_gt(), "dev"
        )
        self.assertEqual([e["id"] for e in result["errors"]], ["c2"])
        self.assertIn("runner", result["errors"][0]["error"])

    def test_an_errored_case_is_not_listed_as_failed(self):
        """`failed` means measured and wrong, not "we could not tell"."""
        runner = EchoRunner(fail_on=["c2"])
        result = self.assertions_evaluator(runner).full_eval(
            self.prompt_file(), self.assertions_gt(), "dev"
        )
        self.assertNotIn("c2", result["failed"])

    def test_every_case_failing_yields_a_zero_rate_without_crashing(self):
        runner = EchoRunner(fail_on=["c1", "c2"])
        result = self.assertions_evaluator(runner).full_eval(
            self.prompt_file(), self.assertions_gt(), "dev"
        )
        self.assertEqual(result["pass_rate"], 0.0)
        self.assertEqual(result["errored"], 2)


class SplitSelectionTests(GraderEvaluatorTestCase):
    def gt_with_splits(self):
        rows = [
            {"id": f"c{i}", "q": f"q{i}", "which": "dev" if i < 3 else "holdout",
             "asserts": [{"type": "contains", "value": "ok"}]}
            for i in range(5)
        ]
        return self.write("gt.jsonl", "\n".join(json.dumps(r) for r in rows) + "\n")

    def evaluator(self):
        return GraderEvaluator(
            grader=ProgrammaticGrader(expectations_key="expectations"),
            runner=EchoRunner(),
            columns=ColumnMap(input=("q",), expectations="asserts", split="which"),
        )

    def test_only_the_requested_split_is_run(self):
        result = self.evaluator().full_eval(
            self.prompt_file(), self.gt_with_splits(), "dev"
        )
        self.assertEqual(len(result["cases"]), 3)

    def test_a_different_split_selects_different_cases(self):
        result = self.evaluator().full_eval(
            self.prompt_file(), self.gt_with_splits(), "holdout"
        )
        self.assertEqual(len(result["cases"]), 2)

    def test_an_empty_split_name_runs_everything(self):
        result = self.evaluator().full_eval(
            self.prompt_file(), self.gt_with_splits(), ""
        )
        self.assertEqual(len(result["cases"]), 5)

    def test_a_nonexistent_split_runs_nothing_rather_than_everything(self):
        """A typo must not look like a successful run over the wrong data."""
        result = self.evaluator().full_eval(
            self.prompt_file(), self.gt_with_splits(), "typo"
        )
        self.assertEqual(len(result["cases"]), 0)
        self.assertEqual(result["pass_rate"], 0.0)


class PointGraderIntegrationTests(GraderEvaluatorTestCase):
    def test_a_point_coverage_run_end_to_end(self):
        gt = self.write("gt.jsonl", json.dumps({
            "id": "c1", "q": "explain it", "pts": ["alpha", "beta"],
        }) + "\n")
        judge = StubJudge(default={"matched": [1, 2], "partial": [], "missed": []})
        ev = GraderEvaluator(
            grader=PointCoverageGrader(judge, points_key="points"),
            runner=EchoRunner(outputs={"c1": "alpha and beta"}),
            columns=ColumnMap(input=("q",), points="pts"),
            splits={"dev": 1.0},
        )
        result = ev.full_eval(self.prompt_file(), gt, "dev")
        self.assertEqual(result["pass_rate"], 1.0)
        self.assertEqual(result["primary"], "f1")
        self.assertIn("precision", result["metrics"])

    def test_a_dishonest_classification_is_reported_as_an_error(self):
        """The conservation guard reaching all the way through the stack."""
        gt = self.write("gt.jsonl", json.dumps({
            "id": "c1", "q": "explain it", "pts": ["alpha", "beta", "gamma"],
        }) + "\n")
        judge = StubJudge(default={"matched": [1], "partial": [], "missed": []})
        ev = GraderEvaluator(
            grader=PointCoverageGrader(judge, points_key="points"),
            runner=EchoRunner(outputs={"c1": "alpha only"}),
            columns=ColumnMap(input=("q",), points="pts"),
            splits={"dev": 1.0},
        )
        result = ev.full_eval(self.prompt_file(), gt, "dev")
        self.assertEqual(result["errored"], 1)
        self.assertIn("conservation", result["errors"][0]["error"])

    def test_judge_tokens_are_reported(self):
        """Cost is a per-case delta, so the stub must actually accumulate.

        A stub holding a constant counter reports zero spend — correctly,
        since nothing was consumed between one call and the next.
        """
        gt = self.write("gt.jsonl", json.dumps({
            "id": "c1", "q": "explain", "pts": ["alpha"],
        }) + "\n")

        class SpendingJudge(StubJudge):
            def complete(self, prompt):
                self.total_tokens += 100
                return super().complete(prompt)

        judge = SpendingJudge(default={"matched": [1], "partial": [], "missed": []})
        judge.total_tokens = 0
        ev = GraderEvaluator(
            grader=PointCoverageGrader(judge, points_key="points"),
            runner=EchoRunner(outputs={"c1": "alpha"}),
            columns=ColumnMap(input=("q",), points="pts"),
            splits={"dev": 1.0},
        )
        self.assertEqual(ev.full_eval(self.prompt_file(), gt, "dev")["tokens"], 100)


class NoTypeBranchingTests(unittest.TestCase):
    def test_the_evaluator_does_not_branch_on_target_or_grader_type(self):
        """I4: otherwise the abstractions are decorative.

        The one isinstance permitted is the ColumnMap coercion in the
        factory, which distinguishes plain config data from a built object
        rather than one artifact shape from another — so it is checked in
        the evaluator module, where no such coercion happens.
        """
        tree = ast.parse((SCRIPTS / "grader_evaluator.py").read_text())
        offenders = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
        ]
        self.assertEqual(offenders, [])

    def test_the_evaluator_names_no_grader_class(self):
        """It holds a grader and calls grade; it must not know which one."""
        source = (SCRIPTS / "grader_evaluator.py").read_text()
        body = source.split("class GraderEvaluator", 1)[1]
        for name in ("ProgrammaticGrader", "PointCoverageGrader", "RubricGrader"):
            self.assertNotIn(name, body)

    def test_the_evaluator_names_no_target_subclass(self):
        source = (SCRIPTS / "grader_evaluator.py").read_text()
        body = source.split("class GraderEvaluator", 1)[1]
        for name in ("SkillTarget", "PromptFileTarget", "SectionTarget"):
            self.assertNotIn(name, body)


class PromptRunnerTests(unittest.TestCase):
    def test_the_instruction_and_input_are_combined(self):
        runner = PromptRunner(model="m")
        with mock.patch("llm._call_llm", return_value="reply") as called:
            out = runner("INSTRUCTION", {"input": "THE INPUT"})
        prompt = called.call_args[0][0]
        self.assertIn("INSTRUCTION", prompt)
        self.assertIn("THE INPUT", prompt)
        self.assertEqual(out, "reply")

    def test_the_template_is_configurable(self):
        """The separator is part of the instruction's contract, not ours."""
        runner = PromptRunner(template="<i>{instruction}</i><q>{input}</q>")
        with mock.patch("llm._call_llm", return_value="r") as called:
            runner("A", {"input": "B"})
        self.assertEqual(called.call_args[0][0], "<i>A</i><q>B</q>")

    def test_the_input_field_is_configurable(self):
        runner = PromptRunner(input_key="prompt")
        with mock.patch("llm._call_llm", return_value="r") as called:
            runner("A", {"prompt": "FROM PROMPT FIELD"})
        self.assertIn("FROM PROMPT FIELD", called.call_args[0][0])

    def test_the_model_is_passed_through(self):
        """Continuing on another model is configuration, not code."""
        runner = PromptRunner(model="other-model")
        with mock.patch("llm._call_llm", return_value="r") as called:
            runner("A", {"input": "B"})
        self.assertEqual(called.call_args.kwargs["model"], "other-model")

    def test_a_none_reply_becomes_an_empty_string(self):
        runner = PromptRunner()
        with mock.patch("llm._call_llm", return_value=None):
            self.assertEqual(runner("A", {"input": "B"}), "")

    def test_a_transport_failure_propagates_and_is_counted(self):
        """So the evaluator records an error rather than grading nothing."""
        runner = PromptRunner()
        with mock.patch("llm._call_llm", side_effect=RuntimeError("down")):
            with self.assertRaises(RuntimeError):
                runner("A", {"input": "B"})
        self.assertEqual(runner.failures, 1)
        self.assertEqual(runner.calls, 1)


class InfoTests(GraderEvaluatorTestCase):
    def test_info_names_the_parts_in_play(self):
        ev = self.assertions_evaluator(EchoRunner())
        info = ev.info()
        self.assertEqual(info["name"], "grader")
        self.assertEqual(info["grader"], "ProgrammaticGrader")
        self.assertEqual(info["primary_metric"], "recall")


class QuickGateTests(GraderEvaluatorTestCase):
    def test_the_quick_gate_delegates_and_does_not_run_cases(self):
        """It exists to reject a malformed candidate before spending calls.

        A sampled run would spend exactly what the gate is meant to save.
        """
        runner = EchoRunner()
        ev = self.assertions_evaluator(runner)
        with mock.patch("run_l1_gate.run_l1_gate", return_value={"pass": True}) as m:
            result = ev.quick_gate(self.prompt_file())
        self.assertTrue(result["pass"])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(runner.seen, [])


if __name__ == "__main__":
    unittest.main()


class TransportFailureTests(GraderEvaluatorTestCase):
    """The backend reports failure by *returning* a string, not raising.

    Scored as output, an error string reads as a confident and terrible
    answer: a run where every call timed out reported pass_rate 0.0 with
    zero errors, which looks like a clean measurement of a hopeless
    candidate. The gate would then reject a prompt that was never tried —
    and the first thing a new user sees is "my prompt scores 0" rather than
    "the model was never reached".
    """

    ERRORS = [
        "[ERROR: claude timed out after 120s]",
        "[ERROR: claude CLI not found — install it or set LLM_BACKEND]",
        "[ERROR: EVOLVER_LLM_URL not set for http backend]",
        "[ERROR: HTTP LLM call failed: 500]",
        "[ERROR: claude CLI exited with status 1]",
        "[ERROR: claude CLI failed to run: oops]",
    ]

    def test_every_transport_failure_becomes_an_errored_case(self):
        """All six, not just the timeout — one missed shape is a silent zero."""
        for error in self.ERRORS:
            with self.subTest(error=error):
                ev = self.assertions_evaluator(lambda i, c, e=error: e)
                result = ev.full_eval(
                    self.prompt_file(), self.assertions_gt(), "dev"
                )
                self.assertEqual(result["errored"], 2)
                self.assertEqual(result["total_assertions"], 0)
                self.assertEqual(result["failed"], [])

    def test_the_programmatic_grader_is_also_protected(self):
        """It is the one most exposed: `contains` matches the error text."""
        ev = self.assertions_evaluator(lambda i, c: self.ERRORS[0])
        result = ev.full_eval(self.prompt_file(), self.assertions_gt(), "dev")
        self.assertEqual(result["errored"], 2)
        self.assertTrue(all("runner" in e["error"] for e in result["errors"]))

    def test_a_leading_newline_does_not_hide_the_marker(self):
        ev = self.assertions_evaluator(lambda i, c: "\n  " + self.ERRORS[0])
        result = ev.full_eval(self.prompt_file(), self.assertions_gt(), "dev")
        self.assertEqual(result["errored"], 2)

    def test_output_merely_mentioning_an_error_is_still_graded(self):
        """Only a reply that *starts* with the marker is a transport failure."""
        ev = self.assertions_evaluator(
            lambda i, c: "The docs mention [ERROR: x] and also alpha and beta"
        )
        result = ev.full_eval(self.prompt_file(), self.assertions_gt(), "dev")
        self.assertEqual(result["errored"], 0)
        self.assertEqual(result["pass_rate"], 1.0)

    def test_the_prompt_runner_raises_on_a_returned_error(self):
        from grader_evaluator import PromptRunner, RunnerFailed

        runner = PromptRunner()
        with mock.patch("llm._call_llm", return_value=self.ERRORS[0]):
            with self.assertRaises(RunnerFailed):
                runner("instruction", {"input": "x"})
        self.assertEqual(runner.failures, 1)

    def test_the_prompt_runner_still_returns_a_real_reply(self):
        from grader_evaluator import PromptRunner

        runner = PromptRunner()
        with mock.patch("llm._call_llm", return_value="a real answer"):
            self.assertEqual(runner("i", {"input": "x"}), "a real answer")
        self.assertEqual(runner.failures, 0)


class GateWiringTests(GraderEvaluatorTestCase):
    """The structural and metric gates must receive what they need.

    Both pass when handed nothing, so omitting the snapshot left them
    permanently inactive — a plan could document a size cap that never
    applied, which is worse than having no cap: the reader believes they
    are protected.
    """

    def test_the_evaluator_supplies_a_snapshot_the_gate_can_use(self):
        from gate import check_structure

        small = self.assertions_evaluator(EchoRunner()).full_eval(
            self.prompt_file("short\n"), self.assertions_gt(), "dev"
        )
        big = self.assertions_evaluator(EchoRunner()).full_eval(
            self.prompt_file("padding " * 500), self.assertions_gt(), "dev"
        )
        self.assertFalse(check_structure(big["snapshot"], small["snapshot"])[0])

    def test_the_orchestrators_argument_shape_activates_both_gates(self):
        """Built the way orchestrator builds it, not a hand-written dict."""
        from gate import phase_6_gate_decision

        lean = self.assertions_evaluator(EchoRunner()).full_eval(
            self.prompt_file("short\n"), self.assertions_gt(), "dev"
        )
        grown = self.assertions_evaluator(EchoRunner()).full_eval(
            self.prompt_file("padding " * 500), self.assertions_gt(), "dev"
        )

        def as_orchestrator(result, rate):
            return {
                "pass_rate": rate, "holdout_pass_rate": None, "l1_pass": True,
                "trigger_f1": 1.0, "tokens_mean": result.get("tokens", 0),
                "duration_mean": result.get("duration", 0.0),
                "regression_pass": 1.0, "snapshot": result.get("snapshot"),
                "metrics": result.get("metrics", {}),
            }

        thresholds = {"min_delta": 0.01, "noise_threshold": 0.005,
                      "max_structure_growth": 0.25}
        verdict = phase_6_gate_decision(
            as_orchestrator(grown, 0.9), as_orchestrator(lean, 0.7), thresholds
        )
        self.assertEqual(verdict["decision"], "discard")
        self.assertTrue(
            any("structure FAIL" in r for r in verdict["reasons"]),
            verdict["reasons"],
        )


class PlanConfigTests(GraderEvaluatorTestCase):
    """Documented settings must reach the code that implements them."""

    def plan(self, body: str):
        p = self.tmp / "evolve_plan.md"
        p.write_text(body)
        return p

    def test_every_documented_gate_threshold_is_parsed(self):
        from evaluators import parse_evaluator_from_plan

        config = parse_evaluator_from_plan(self.plan(
            "## Gate Thresholds\n"
            "- max_structure_growth: 0.25\n"
            '- max_structure: {"lines": 200}\n'
            '- min_metrics: {"precision": 0.9}\n'
            '- max_metric_regression: {"recall": 0.05}\n'
        ))
        self.assertEqual(config["max_structure_growth"], 0.25)
        self.assertEqual(config["max_structure"], {"lines": 200})
        self.assertEqual(config["min_metrics"], {"precision": 0.9})
        self.assertEqual(config["max_metric_regression"], {"recall": 0.05})

    def test_grader_settings_are_parsed(self):
        from evaluators import parse_evaluator_from_plan

        config = parse_evaluator_from_plan(self.plan(
            "evaluator: grader\n"
            "grader: points\n"
            "points_key: pts\n"
            "commit_first: true\n"
            'columns: {"input": ["q"], "points": "pts"}\n'
        ))
        self.assertEqual(config["grader"], "points")
        self.assertEqual(config["points_key"], "pts")
        self.assertIs(config["commit_first"], True)
        self.assertEqual(config["columns"]["points"], "pts")

    def test_a_misspelled_setting_is_reported_not_dropped(self):
        """Silence here is how a plan appears to cap size and does not."""
        from evaluators import parse_evaluator_from_plan

        config = parse_evaluator_from_plan(self.plan("- max_structures: 0.25\n"))
        self.assertIn("max_structures", config.get("_unknown", []))

    def test_prose_is_not_mistaken_for_configuration(self):
        from evaluators import parse_evaluator_from_plan

        config = parse_evaluator_from_plan(self.plan(
            "## Notes\n"
            "Reference: see the design doc\n"
            "Owner: someone\n"
        ))
        self.assertEqual(config.get("_unknown", []), [])

    def test_a_malformed_value_leaves_the_default_in_place(self):
        from evaluators import parse_evaluator_from_plan

        config = parse_evaluator_from_plan(self.plan(
            "max_structure_growth: not-a-number\n"
            "max_structure: not-json\n"
        ))
        self.assertNotIn("max_structure_growth", config)
        self.assertNotIn("max_structure", config)

    def test_the_legacy_keys_still_parse(self):
        from evaluators import parse_evaluator_from_plan

        config = parse_evaluator_from_plan(self.plan(
            "evaluator: script\n"
            "evaluator_script: ./my_eval.py\n"
            "evaluator_timeout: 300\n"
            "model: some-model\n"
            "behavioral_sample_size: 8\n"
        ))
        self.assertEqual(config["evaluator"], "script")
        self.assertEqual(config["evaluator_script"], "./my_eval.py")
        self.assertEqual(config["evaluator_timeout"], 300)
        self.assertEqual(config["model"], "some-model")
        self.assertEqual(config["behavioral_sample_size"], 8)


class PlanTypoReportingTests(GraderEvaluatorTestCase):
    """A setting that fails to take effect must say so.

    An earlier version matched misspellings against a hand-written list of
    look-alike names. Of fourteen realistic typos only one happened to be on
    it, so thirteen stayed silent — in a mechanism whose entire purpose is to
    catch "I set a threshold and it did nothing". Shape recognition replaces
    the list: prose is capitalised, spaced or punctuated, settings are
    lower-case snake_case.
    """

    TYPOS = [
        "mn_metrics", "min_metrcis", "max_structure_growh",
        "max_stucture_growth", "commit_frist", "points_ky", "judge_mdoel",
        "colums", "stratefy", "max_metric_regresion", "min_metric",
        "evaluatr", "graderr", "spilts",
    ]

    def plan(self, body: str):
        p = self.tmp / "evolve_plan.md"
        p.write_text(body)
        return p

    def parse(self, body: str):
        from evaluators import parse_evaluator_from_plan

        return parse_evaluator_from_plan(self.plan(body))

    def test_every_realistic_typo_is_reported(self):
        reported = self.parse(
            "\n".join(f"- {t}: 0.25" for t in self.TYPOS)
        ).get("_unknown", [])
        for typo in self.TYPOS:
            with self.subTest(typo=typo):
                self.assertTrue(
                    any(typo in entry for entry in reported),
                    f"{typo} was dropped silently",
                )

    def test_a_valid_key_with_an_unusable_value_is_also_reported(self):
        """Same symptom as a typo: the setting has no effect."""
        config = self.parse(
            "- max_structure_growth: 25%\n"
            "- min_metrics: {broken json\n"
            "- evaluator_timeout: soon\n"
        )
        for key in ("max_structure_growth", "min_metrics", "evaluator_timeout"):
            with self.subTest(key=key):
                self.assertNotIn(key, config)
                self.assertTrue(
                    any(key in entry for entry in config["_unknown"]),
                    f"{key} was dropped without a warning",
                )

    def test_the_report_names_the_expected_type(self):
        config = self.parse("- max_structure_growth: 25%\n")
        self.assertIn("float", config["_unknown"][0])

    def test_prose_is_still_not_flagged(self):
        """The check must stay quiet, or it gets ignored and then removed."""
        config = self.parse(
            "## Notes\n"
            "Warning: be careful here\n"
            "TODO: fix later\n"
            "URL: https://example.com/a\n"
            "Ratio: 70/20/10\n"
            "Step 1: do the thing\n"
            "Reference: see the design doc\n"
            "Owner: someone\n"
        )
        self.assertEqual(config.get("_unknown", []), [])

    def test_valid_settings_produce_no_warning(self):
        config = self.parse(
            "evaluator: grader\n"
            "grader: points\n"
            "commit_first: true\n"
            "model: claude-3.5-sonnet\n"
            "max_structure_growth: 0.25\n"
            'min_metrics: {"precision": 0.9}\n'
            'columns: {"input": ["q"]}\n'
        )
        self.assertEqual(config.get("_unknown", []), [])
        self.assertEqual(config["model"], "claude-3.5-sonnet")
        self.assertEqual(config["max_structure_growth"], 0.25)

    def test_a_blank_value_leaves_the_default_and_is_reported(self):
        """"I wrote the key but no value" is still a setting that did nothing."""
        config = self.parse("- max_structure_growth:\n")
        self.assertNotIn("max_structure_growth", config)
        self.assertTrue(config["_unknown"])

    def test_false_written_several_ways(self):
        for text in ("false", "no", "off", "0", "False", "NO"):
            with self.subTest(text=text):
                config = self.parse(f"commit_first: {text}\n")
                self.assertIs(config["commit_first"], False)

    def test_true_written_several_ways(self):
        for text in ("true", "yes", "on", "1", "True", "YES"):
            with self.subTest(text=text):
                config = self.parse(f"commit_first: {text}\n")
                self.assertIs(config["commit_first"], True)

    def test_a_non_boolean_for_a_boolean_key_is_reported(self):
        config = self.parse("commit_first: maybe\n")
        self.assertNotIn("commit_first", config)
        self.assertIn("bool", config["_unknown"][0])

    def test_json_of_the_wrong_shape_is_reported(self):
        """A list where a mapping is expected is not usable."""
        config = self.parse('min_metrics: ["precision"]\n')
        self.assertNotIn("min_metrics", config)
        self.assertTrue(config["_unknown"])

    def test_a_list_valued_key_accepts_a_json_array(self):
        config = self.parse('splits: {"dev": 0.7, "holdout": 0.3}\n')
        self.assertEqual(config["splits"], {"dev": 0.7, "holdout": 0.3})


class ErroredCaseCostTests(GraderEvaluatorTestCase):
    """A case that failed still spent money.

    A run where every classification came back malformed used to report zero
    tokens, so the budget gate compared nothing against the baseline and
    passed. Combined with a transport failure that also reports zero score,
    a broken run looked both free and hopeless.
    """

    def test_a_failed_classification_still_reports_its_cost(self):
        import judgment
        from graders import PointCoverageGrader

        class Paying:
            def __init__(self):
                self.total_tokens = 0
                self.total_duration = 0.0

            def complete(self, prompt):
                self.total_tokens += 100
                return "garbage, not json"

            def judge_with_reasoning(self, q, c):
                return True, "unused"

        grader = PointCoverageGrader(Paying())
        judgments = [
            grader.grade({"id": f"c{i}", "points": ["a"]}, "out")
            for i in range(10)
        ]
        rollup = judgment.aggregate(judgments)
        self.assertEqual(rollup["errored"], 10)
        self.assertEqual(rollup["cost"]["tokens"], 1000)

    def test_a_mixed_run_reports_the_full_spend(self):
        import judgment
        from graders import PointCoverageGrader

        class Flaky:
            def __init__(self):
                self.total_tokens = 0
                self.total_duration = 0.0
                self.calls = 0

            def complete(self, prompt):
                self.total_tokens += 100
                self.calls += 1
                if self.calls % 2:
                    return json.dumps({
                        "matched": [1], "partial": [], "missed": [], "extra": [],
                    })
                return "garbage"

            def judge_with_reasoning(self, q, c):
                return True, "unused"

        grader = PointCoverageGrader(Flaky())
        judgments = [
            grader.grade({"id": f"c{i}", "points": ["a"]}, "out")
            for i in range(10)
        ]
        rollup = judgment.aggregate(judgments)
        self.assertEqual(rollup["errored"], 5)
        self.assertEqual(rollup["cost"]["tokens"], 1000)
