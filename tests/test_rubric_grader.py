"""Tests for RubricGrader and commit-first.

The reason commit-first exists is a measured failure: a judge shown only a
candidate rates how plausible it looks, and self-play drove approval from
0.72 to 0.94 while real accuracy stayed at 0.20. Stronger judges, other
model families and strict ensembles all failed; answering the task first
cut false acceptance from 0.719 to 0.012.

So the tests here are not "does it call the model twice". They are:

- **Isolation is structural.** The solving step's signature admits only a
  task, so no wording is relied on to keep the candidate out of it.
- **A controlled comparison.** A judge that rubber-stamps anything
  plausible is approximated by a stub that says YES to fluent text, and the
  same candidate is graded with the protection off and on.
"""

import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "plugin" / "skills" / "skill-evolver" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from graders import (  # noqa: E402
    BaseGrader,
    PointCoverageGrader,
    ProgrammaticGrader,
    RubricGrader,
)
from grader_evaluator import build_grader  # noqa: E402


class RecordingJudge:
    """Answers YES unless told otherwise, and records every prompt.

    Exposes both channels, matching the real judge: ``complete`` returns a
    reply intact, ``judge_with_reasoning`` consumes the last line as its
    verdict. Reproducing that split matters — a stub that returned the whole
    reply on the binary channel would hide a caller routing prose through it
    and losing the final line.
    """

    def __init__(self, verdicts=None, solve_reply="an independent answer"):
        self.verdicts = verdicts or {}
        self.solve_reply = solve_reply
        self.prompts = []
        self.total_tokens = 60
        self.total_duration = 0.3

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.solve_reply

    def judge_with_reasoning(self, question, context):
        self.prompts.append(question)
        for needle, verdict in self.verdicts.items():
            if needle in question:
                return verdict, f"because of {needle}"
        return True, "looks fine"

    # Convenience for the tests.
    @property
    def solve_prompts(self):
        return [p for p in self.prompts if "Answer the task directly" in p]

    @property
    def check_prompts(self):
        return [p for p in self.prompts if "Answer the task directly" not in p]


class PlausibilityJudge:
    """Stands in for a judge that rewards fluency over correctness.

    Approves anything that reads confidently when it has nothing to compare
    against, and only notices the error once it has produced its own answer
    — which is the behaviour the paper measured and the reason commit-first
    exists.

    Reads the candidate out of the prompt rather than searching the whole
    text: the reference block contains the correct answer by construction,
    so a whole-prompt search would find it there and the stub would approve
    every candidate regardless.
    """

    CORRECT_KEYWORD = "released"

    def __init__(self):
        self.solved = None
        self.total_tokens = 0
        self.total_duration = 0.0

    @staticmethod
    def _candidate(question: str) -> str:
        marker = "Response to check:\n"
        return question.split(marker, 1)[1] if marker in question else question

    def complete(self, prompt):
        self.solved = f"the licence is {self.CORRECT_KEYWORD} automatically"
        return self.solved

    def judge_with_reasoning(self, question, context):
        candidate = self._candidate(question)
        if "independently produced answer" not in question:
            # Nothing to compare against: fluency wins.
            confident = "certainly" in candidate or "definitely" in candidate
            return confident, "sounds authoritative"
        # With its own answer in hand, the contradiction becomes visible.
        agrees = self.CORRECT_KEYWORD in candidate
        return agrees, "compared against my own answer"


CASE = {
    "id": "c1",
    "input": "what happens to the licence when the holder leaves?",
    "rubric": [
        "States what happens to the licence",
        "Does not speculate beyond the material",
    ],
}


class RubricBasicsTests(unittest.TestCase):
    def test_all_criteria_satisfied_scores_one(self):
        grader = RubricGrader(RecordingJudge(), commit_first=False)
        j = grader.grade(CASE, "the licence is released automatically")
        self.assertEqual(j.metrics["recall"], 1.0)
        self.assertTrue(j.passed)

    def test_a_violated_criterion_lowers_the_score(self):
        judge = RecordingJudge(verdicts={"speculate": False})
        j = RubricGrader(judge, commit_first=False).grade(CASE, "maybe, probably")
        self.assertEqual(j.metrics["recall"], 0.5)
        self.assertFalse(j.passed)

    def test_each_criterion_is_asked_as_its_own_question(self):
        """A binary question has lower variance than a rating.

        And a judge that can only answer YES or NO cannot hand back a score
        to inflate.
        """
        judge = RecordingJudge()
        RubricGrader(judge, commit_first=False).grade(CASE, "an answer")
        self.assertEqual(len(judge.check_prompts), 2)
        self.assertIn("States what happens", judge.check_prompts[0])
        self.assertIn("Does not speculate", judge.check_prompts[1])

    def test_the_prompt_asks_for_yes_or_no_not_a_rating(self):
        judge = RecordingJudge()
        RubricGrader(judge, commit_first=False).grade(CASE, "an answer")
        prompt = judge.check_prompts[0].lower()
        self.assertIn("yes or no", prompt)
        for forbidden in ("out of 10", "score from", "0-100", "rate the"):
            self.assertNotIn(forbidden, prompt)

    def test_ranks_on_recall_because_precision_is_unobservable(self):
        """Checking a list of rules cannot see content no rule asked about."""
        self.assertEqual(RubricGrader.primary_metric, "recall")
        j = RubricGrader(RecordingJudge(), commit_first=False).grade(CASE, "x")
        self.assertEqual(j.primary, "recall")

    def test_missing_criteria_is_an_error_not_a_zero(self):
        j = RubricGrader(RecordingJudge()).grade({"id": "c1", "rubric": []}, "x")
        self.assertIsNotNone(j.error)

    def test_the_rubric_field_is_configurable(self):
        grader = RubricGrader(RecordingJudge(), rubric_key="rules",
                              commit_first=False)
        j = grader.grade({"id": "c1", "rules": ["Is polite"]}, "please")
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_criteria_may_arrive_as_a_json_string(self):
        grader = RubricGrader(RecordingJudge(), commit_first=False)
        j = grader.grade(
            {"id": "c1", "rubric": json.dumps(["Is polite", "Is brief"])}, "hi"
        )
        self.assertEqual(j.evidence["expected_total"], 2)

    def test_criteria_may_be_dicts_with_a_description(self):
        grader = RubricGrader(RecordingJudge(), commit_first=False)
        j = grader.grade(
            {"id": "c1", "rubric": [{"description": "Cites a source"}]}, "x"
        )
        self.assertEqual(j.evidence["expected_total"], 1)

    def test_feedback_names_the_unsatisfied_requirement_and_why(self):
        """A score says worse; feedback says why. Only the latter is actionable."""
        judge = RecordingJudge(verdicts={"speculate": False})
        j = RubricGrader(judge, commit_first=False).grade(CASE, "maybe")
        self.assertIn("Does not speculate", j.feedback)
        self.assertIn("because of", j.feedback)

    def test_feedback_is_bounded(self):
        criteria = [f"Requirement number {i}" for i in range(20)]
        judge = RecordingJudge(verdicts={"Requirement": False})
        j = RubricGrader(judge, commit_first=False).grade(
            {"id": "c1", "rubric": criteria}, "x"
        )
        self.assertIn("and 12 more", j.feedback)

    def test_cost_counts_every_call(self):
        judge = RecordingJudge()
        j = RubricGrader(judge, commit_first=False).grade(CASE, "x")
        self.assertEqual(j.cost["classifier_calls"], 2)

    def test_cost_reports_this_cases_spend_not_the_running_total(self):
        """A cumulative counter reported per case inflates the run's total."""
        class CountingJudge(RecordingJudge):
            def judge_with_reasoning(self, question, context):
                self.total_tokens += 30
                return super().judge_with_reasoning(question, context)

        judge = CountingJudge()
        judge.total_tokens = 0
        grader = RubricGrader(judge, commit_first=False)
        per_case = [grader.grade(CASE, "x").cost["tokens"] for _ in range(3)]
        self.assertEqual(per_case, [60, 60, 60])  # two criteria, 30 each

    def test_the_candidate_text_is_capped(self):
        judge = RecordingJudge()
        RubricGrader(judge, commit_first=False, max_output_chars=20).grade(
            CASE, "y" * 500
        )
        self.assertNotIn("y" * 21, judge.check_prompts[0])


class CommitFirstTests(unittest.TestCase):
    def test_it_is_on_by_default(self):
        """A safeguard that must be switched on is one that gets forgotten."""
        self.assertTrue(RubricGrader(RecordingJudge()).commit_first)

    def test_the_judge_answers_the_task_before_checking(self):
        judge = RecordingJudge()
        RubricGrader(judge).grade(CASE, "a candidate answer")
        self.assertEqual(len(judge.solve_prompts), 1)
        self.assertEqual(judge.prompts.index(judge.solve_prompts[0]), 0)

    def test_the_solving_prompt_contains_the_task(self):
        judge = RecordingJudge()
        RubricGrader(judge).grade(CASE, "a candidate answer")
        self.assertIn("what happens to the licence", judge.solve_prompts[0])

    def test_the_solving_prompt_never_contains_the_candidate(self):
        """The core guarantee. Isolation is structural, not requested."""
        judge = RecordingJudge()
        marker = "UNIQUE-CANDIDATE-MARKER-9f2a"
        RubricGrader(judge).grade(CASE, f"answer containing {marker}")
        self.assertNotIn(marker, judge.solve_prompts[0])

    def test_the_signature_of_the_solving_step_admits_only_a_task(self):
        """Asserted on the signature so a future edit cannot quietly widen it.

        Relying on prompt wording would leave the protection to whatever the
        model chose to ignore; relying on the signature means the candidate
        has no path in at all.
        """
        import inspect

        params = list(inspect.signature(RubricGrader._solve).parameters)
        self.assertEqual(params, ["self", "task"])

    def test_the_reference_reaches_the_checking_step(self):
        judge = RecordingJudge(solve_reply="MY-OWN-ANSWER")
        RubricGrader(judge).grade(CASE, "a candidate")
        self.assertIn("MY-OWN-ANSWER", judge.check_prompts[0])

    def test_the_reference_is_marked_as_non_authoritative(self):
        """It orients the judge; it is not a substitute ground truth."""
        judge = RecordingJudge()
        RubricGrader(judge).grade(CASE, "a candidate")
        prompt = judge.check_prompts[0]
        self.assertIn("not authoritative", prompt)
        self.assertIn("may itself be imperfect", prompt)

    def test_no_reference_block_appears_when_disabled(self):
        judge = RecordingJudge()
        RubricGrader(judge, commit_first=False).grade(CASE, "a candidate")
        self.assertEqual(judge.solve_prompts, [])
        self.assertNotIn("independently produced", judge.check_prompts[0])

    def test_an_empty_task_skips_solving_rather_than_asking_blind(self):
        """Asking a judge to answer nothing would produce noise as a reference."""
        judge = RecordingJudge()
        RubricGrader(judge).grade({"id": "c1", "rubric": ["Is polite"]}, "hi")
        self.assertEqual(judge.solve_prompts, [])

    def test_the_task_field_is_configurable(self):
        judge = RecordingJudge()
        grader = RubricGrader(judge, task_key="prompt")
        grader.grade(
            {"id": "c1", "prompt": "THE TASK TEXT", "rubric": ["Is polite"]}, "hi"
        )
        self.assertIn("THE TASK TEXT", judge.solve_prompts[0])

    def test_extra_framing_can_be_supplied_for_solving(self):
        judge = RecordingJudge()
        RubricGrader(judge, instruction="You are a support agent.").grade(
            CASE, "candidate"
        )
        self.assertIn("You are a support agent.", judge.solve_prompts[0])

    def test_a_failure_while_solving_errors_the_case(self):
        """Grading without the protection is worse than not grading.

        With commit-first off the same wrong-but-fluent candidate scores 1.0
        instead of 0.0, so a lapsed safeguard does not weaken the
        measurement — it inverts it. An earlier version degraded to grading
        without a reference on the reasoning that a weaker measurement beats
        none; under rate limiting that would silently disable the protection
        exactly during the longest unattended runs.
        """
        class Flaky(RecordingJudge):
            def complete(self, prompt):
                raise RuntimeError("backend down")

        j = RubricGrader(Flaky()).grade(CASE, "a candidate")
        self.assertIsNotNone(j.error)
        self.assertIn("commit-first", j.error.lower().replace("_", "-"))
        self.assertEqual(j.metrics, {})

    def test_an_empty_solve_reply_errors_the_case(self):
        """No independent answer means the comparison never happened."""
        j = RubricGrader(RecordingJudge(solve_reply="")).grade(CASE, "candidate")
        self.assertIsNotNone(j.error)
        self.assertEqual(j.metrics, {})

    def test_a_case_with_no_task_errors_rather_than_grading_unprotected(self):
        """It used to skip solving and grade anyway, spending nothing.

        That made the lapse invisible even in the cost figures.
        """
        judge = RecordingJudge()
        j = RubricGrader(judge).grade(
            {"id": "c1", "rubric": ["Is polite"]}, "hi"
        )
        self.assertIsNotNone(j.error)
        self.assertEqual(judge.solve_prompts, [])

    def test_evidence_records_that_the_protection_applied(self):
        """A score with the protection means something different from one
        without it, so the logs must distinguish them."""
        j = RubricGrader(RecordingJudge()).grade(CASE, "a candidate")
        self.assertIs(j.evidence["commit_first_applied"], True)

    def test_evidence_records_when_the_protection_was_off(self):
        j = RubricGrader(RecordingJudge(), commit_first=False).grade(CASE, "x")
        self.assertIs(j.evidence["commit_first_applied"], False)

    def test_the_reference_is_capped(self):
        judge = RecordingJudge(solve_reply="z" * 500)
        RubricGrader(judge, max_output_chars=30).grade(CASE, "a candidate")
        self.assertNotIn("z" * 31, judge.check_prompts[0])

    def test_solving_happens_once_per_case_not_once_per_criterion(self):
        """Otherwise the cost scales with the rubric for no added protection."""
        judge = RecordingJudge()
        RubricGrader(judge).grade(
            {"id": "c1", "input": "a task", "rubric": ["a", "b", "c", "d"]}, "x"
        )
        self.assertEqual(len(judge.solve_prompts), 1)
        self.assertEqual(len(judge.check_prompts), 4)


    def test_a_judge_without_a_raw_channel_is_rejected_at_construction(self):
        """Skipping the solve step would turn commit-first off without saying so.

        The protection is the whole reason this grader is usable, so losing
        it must be loud — and loud at wiring time, not per case.
        """
        class BinaryOnly:
            def judge_with_reasoning(self, question, context):
                return True, "fine"

        with self.assertRaises(TypeError) as ctx:
            RubricGrader(BinaryOnly(), commit_first=True)
        self.assertIn("complete()", str(ctx.exception))

    def test_a_binary_only_judge_is_fine_with_the_protection_off(self):
        """The requirement applies only when solving will actually happen."""
        class BinaryOnly:
            def judge_with_reasoning(self, question, context):
                return True, "fine"

        j = RubricGrader(BinaryOnly(), commit_first=False).grade(CASE, "x")
        self.assertIsNone(j.error)
        self.assertEqual(j.metrics["recall"], 1.0)


class CommitFirstControlledComparisonTests(unittest.TestCase):
    """The same wrong-but-fluent candidate, graded with and without.

    This is the experiment the design rests on, run against a stub whose
    behaviour matches the reported failure: it approves confident prose when
    it has nothing to compare against, and catches the error once it has
    produced its own answer.
    """

    CASE = {
        "id": "c1",
        "input": "what happens to the licence when the holder leaves?",
        "rubric": ["States correctly what happens to the licence"],
    }
    WRONG_BUT_FLUENT = (
        "The licence is certainly retained by the organisation indefinitely "
        "and definitely requires no further action."
    )
    CORRECT = "the licence is released automatically"

    def grade(self, output, commit_first):
        judge = PlausibilityJudge()
        grader = RubricGrader(judge, commit_first=commit_first)
        return grader.grade(self.CASE, output), judge

    def test_without_protection_a_wrong_fluent_answer_is_approved(self):
        j, judge = self.grade(self.WRONG_BUT_FLUENT, commit_first=False)
        self.assertEqual(j.metrics["recall"], 1.0)
        self.assertIsNone(judge.solved)

    def test_with_protection_the_same_answer_is_rejected(self):
        j, judge = self.grade(self.WRONG_BUT_FLUENT, commit_first=True)
        self.assertEqual(j.metrics["recall"], 0.0)
        self.assertIsNotNone(judge.solved)

    def test_protection_does_not_reject_a_correct_answer(self):
        """A safeguard that fails good candidates is not usable."""
        j, _ = self.grade(self.CORRECT, commit_first=True)
        self.assertEqual(j.metrics["recall"], 1.0)

    def test_without_protection_a_correct_but_plain_answer_is_rejected(self):
        """The failure mode itself: fluency, not correctness, is scored."""
        j, _ = self.grade(self.CORRECT, commit_first=False)
        self.assertEqual(j.metrics["recall"], 0.0)


class RubricArchitectureTests(unittest.TestCase):
    def test_it_shares_the_grader_skeleton(self):
        self.assertTrue(issubclass(RubricGrader, BaseGrader))

    def test_it_does_not_inherit_from_the_other_graders(self):
        """Their notions of "an expectation" are unrelated."""
        self.assertFalse(issubclass(RubricGrader, ProgrammaticGrader))
        self.assertFalse(issubclass(RubricGrader, PointCoverageGrader))
        self.assertFalse(issubclass(PointCoverageGrader, RubricGrader))

    def test_the_judge_backed_graders_share_one_cost_implementation(self):
        """Two copies would drift; the shared base holds it once."""
        self.assertEqual(
            RubricGrader._cost, PointCoverageGrader._cost,
            "cost reporting must not be reimplemented per grader",
        )

    def test_a_judge_without_counters_still_works(self):
        class Bare:
            def judge_with_reasoning(self, q, c):
                return True, "fine"

        j = RubricGrader(Bare(), commit_first=False).grade(CASE, "x")
        self.assertEqual(j.cost, {"classifier_calls": 2})

    def test_it_computes_no_scores_of_its_own(self):
        """I2 again: the arithmetic stays in scoring."""
        import ast

        tree = ast.parse((SCRIPTS / "graders.py").read_text())
        divisions = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.BinOp)
            and isinstance(node.op, (ast.Div, ast.FloorDiv))
        ]
        self.assertEqual(divisions, [])


class RubricFactoryTests(unittest.TestCase):
    def test_the_factory_builds_it(self):
        grader = build_grader({"grader": "rubric"})
        self.assertIsInstance(grader, RubricGrader)

    def test_commit_first_is_on_unless_asked_otherwise(self):
        self.assertTrue(build_grader({"grader": "rubric"}).commit_first)
        self.assertFalse(
            build_grader({"grader": "rubric", "commit_first": False}).commit_first
        )

    def test_field_names_are_configurable(self):
        grader = build_grader({
            "grader": "rubric", "rubric_key": "rules", "input_key": "prompt",
        })
        self.assertEqual(grader.rubric_key, "rules")
        self.assertEqual(grader.task_key, "prompt")

    def test_framing_is_forwarded(self):
        grader = build_grader({
            "grader": "rubric", "rubric_instruction": "You are an agent.",
        })
        self.assertEqual(grader.instruction, "You are an agent.")

    def test_it_reuses_the_engines_binary_judge(self):
        from binary_judge import BinaryLLMJudge

        grader = build_grader({"grader": "rubric", "judge_model": "j"})
        self.assertIsInstance(grader.judge, BinaryLLMJudge)
        self.assertEqual(grader.judge.model, "j")

    def test_the_error_message_lists_rubric_as_an_option(self):
        with self.assertRaises(ValueError) as ctx:
            build_grader({"grader": "nonsense"})
        self.assertIn("rubric", str(ctx.exception))

    def test_both_judge_backed_graders_share_the_judge_builder(self):
        """One place decides how a model is reached."""
        a = build_grader({"grader": "points", "judge_model": "m"})
        b = build_grader({"grader": "rubric", "judge_model": "m"})
        self.assertEqual(type(a.judge), type(b.judge))
        self.assertEqual(a.judge.model, b.judge.model)


if __name__ == "__main__":
    unittest.main()
