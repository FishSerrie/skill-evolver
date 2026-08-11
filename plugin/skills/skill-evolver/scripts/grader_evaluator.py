"""Evaluation by running the artifact and grading what it produces.

Single responsibility: wire the pieces together. It obtains a candidate's
output for each case, hands case and output to a grader, and rolls the
judgments up into the result dict the loop already consumes. It decides
nothing itself — not how to reach a model, not what counts as correct, not
how to compute a score.

Why this is a separate evaluator rather than a change to the existing one
------------------------------------------------------------------------
``LocalEvaluator`` scores a candidate by matching assertions against its
concatenated *text*. That answers "does the document say the right
things", which is a real question and the only one available without
executing anything. This evaluator answers a different one: "does the
artifact, when actually used, produce the right output". Both are worth
having, and folding the second into the first would leave no way to run
the first — so the existing evaluator is untouched and this one is opt-in.

Why it lives in its own module
------------------------------
``evaluators.py`` already carries the protocol and the text-matching
evaluator; ``evaluator_backends.py`` carries four unrelated backends and is
over six hundred lines. Adding to either would grow a second junk drawer.
This module has one dependency direction — it imports the protocol, never
the reverse.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import judgment
from datasets import ColumnMap, load_cases, split_cases
from case_store import write_cases_to_dir
from evaluators import Evaluator
from target import Target, resolve_target

__all__ = ["GraderEvaluator", "PromptRunner", "RunnerFailed", "build_grader"]

#: How a candidate's output is obtained for one case. Injected rather than
#: implemented here: what "running" means differs per artifact — a system
#: prompt sent to a model, a skill executed by an agent, a script invoked
#: with arguments — and an evaluator that picked one would be usable for
#: only that kind.
Runner = Callable[[str, Mapping], str]


class RunnerFailed(RuntimeError):
    """The candidate could not be run, so nothing was measured.

    Distinct from a bad candidate, and the distinction is the whole point:
    the backend layer reports failure by *returning a string* like
    ``[ERROR: claude timed out after 120s]`` rather than raising. Scored as
    output, that string reads as a confident and terrible answer — a run
    where every call timed out would report ``pass_rate=0.0`` with zero
    errors, which looks like a clean measurement of a hopeless candidate.
    The gate would then reject a prompt that was never actually tried.
    """


class PromptRunner:
    """Runs a candidate by sending it to a model as an instruction.

    The common case for prompt optimization: the artifact under test *is*
    the instruction, and the case supplies the input. Reaches the model
    through the engine's existing backend layer, so a new backend works
    here without changes — and the model is named per run, which is what
    makes optimizing against one model and then continuing on another a
    matter of configuration rather than code.
    """

    def __init__(
        self,
        model: str | None = None,
        backend: str | None = None,
        timeout: int = 120,
        input_key: str = "input",
        template: str = "{instruction}\n\n---\n\n{input}",
    ):
        """
        Args:
            model: model identifier passed to the backend.
            backend: which backend to use; auto-detected when omitted.
            timeout: per-call limit in seconds.
            input_key: which case field carries the input.
            template: how instruction and input are combined. A parameter
                because the separator that a given instruction expects is
                part of that instruction's contract, not a property of the
                engine.
        """
        self.model = model
        self.backend = backend
        self.timeout = timeout
        self.input_key = input_key
        self.template = template
        self.calls = 0
        self.failures = 0

    def __call__(self, instruction: str, case: Mapping) -> str:
        from llm import _call_llm, is_llm_error  # local import: avoids a cycle

        prompt = self.template.format(
            instruction=instruction,
            input=case.get(self.input_key, ""),
        )
        self.calls += 1
        try:
            output = _call_llm(
                prompt, model=self.model, timeout=self.timeout,
                backend=self.backend,
            ) or ""
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            raise RunnerFailed(f"{type(exc).__name__}: {exc}") from exc

        # The backend reports failure by *returning* a string, not raising,
        # so this check is what makes the clause above more than dead code.
        # Without it an error string is scored as the candidate's answer: a
        # run where every call timed out would report pass_rate 0.0 with
        # zero errors, which reads as a clean measurement of a hopeless
        # candidate rather than as nothing having been measured.
        if is_llm_error(output):
            self.failures += 1
            raise RunnerFailed(output.strip())
        return output


def build_grader(config: Mapping) -> Any:
    """Construct the grader named in ``config``.

    The single place that maps a configuration name to a grader class.
    Concentrating it here is what keeps the evaluator free of type
    branching: it holds a grader and calls ``grade``, without knowing
    which one it got.

    Recognised keys:
        grader: "points" | "assertions" | "rubric"
        model / grader_timeout: passed to the judge for model-backed graders
        partial_weight / pass_threshold: passed to the grader
        points_key / expectations_key / rubric_key: which case field holds
            expectations
        commit_first: for "rubric", whether the judge answers the task
            before seeing the candidate
    """
    import graders

    name = str(config.get("grader", "points"))
    shared = {
        key: config[key]
        for key in ("partial_weight", "pass_threshold")
        if key in config
    }

    if name == "assertions":
        return graders.ProgrammaticGrader(
            expectations_key=config.get("expectations_key", "expectations"),
            **shared,
        )

    if name in ("points", "rubric"):
        judge = _judge_from(config)
        if name == "points":
            return graders.PointCoverageGrader(
                judge=judge,
                points_key=config.get("points_key", "points"),
                **shared,
            )
        return graders.RubricGrader(
            judge=judge,
            rubric_key=config.get("rubric_key", "rubric"),
            task_key=config.get("input_key", "input"),
            # Defaults to on. The unprotected mode is the one with a
            # measured failure — approval rising to 0.94 while real
            # accuracy stayed at 0.20 — so it must be asked for explicitly.
            commit_first=config.get("commit_first", True),
            instruction=config.get("rubric_instruction", ""),
            **shared,
        )

    raise ValueError(
        f"unknown grader {name!r}; expected 'points', 'assertions', or 'rubric'"
    )


def _judge_from(config: Mapping):
    """Build the binary judge a model-backed grader needs.

    Reuses the engine's existing judge rather than opening a second client:
    one place decides how a model is reached, so a new backend works
    everywhere at once. The judge model can be named separately from the
    model under optimization — judging with the model being tuned would let
    its own blind spots go unnoticed.

    ``judge_backend`` is likewise separate from ``runner_backend``. Both
    default to auto-detection, which picks the first usable CLI; naming one
    matters when the first detected is not the one you want — an
    unauthenticated CLI ahead of a working one fails every case with the
    same transport error, and without this there is no way to route around
    it from configuration.
    """
    from binary_judge import BinaryLLMJudge

    return BinaryLLMJudge(
        model=config.get("judge_model") or config.get("model"),
        timeout=int(config.get("grader_timeout", 60)),
        backend=config.get("judge_backend") or config.get("runner_backend"),
    )


class GraderEvaluator(Evaluator):
    """Evaluates by producing output and grading it.

    Everything variable is injected: the target says what is being
    optimized, the runner says how to exercise it, the grader says what
    counts as correct. This class only sequences them, which is why it
    needs no branch on any of the three.
    """

    name = "grader"

    def __init__(
        self,
        grader,
        runner: Runner,
        columns: ColumnMap | None = None,
        splits: Mapping[str, float] | None = None,
        stratify: bool = True,
        section: str | None = None,
    ):
        """
        Args:
            grader: object exposing ``grade(case, output) -> Judgment``.
            runner: callable producing a candidate's output for one case.
            columns: how the dataset's columns map onto case fields.
            splits: split weights, used only when the dataset does not
                already assign splits.
            stratify: whether to balance strata across splits.
            section: restrict optimization to one heading of the target
                file. Held here rather than passed per call because it
                identifies *which artifact* is under test, and letting it
                vary between calls would mean two runs silently measured
                different things.
        """
        self.grader = grader
        self.runner = runner
        self.columns = columns
        self.splits = splits
        self.stratify = stratify
        self.section = section

    # ── protocol ─────────────────────────────

    def quick_gate(self, skill_path: Path, gt_path: Path | None = None) -> dict:
        """Structural checks only, delegated to the existing gate.

        Deliberately not "run a few cases": the quick gate exists to reject
        a malformed candidate before spending model calls on it, and a
        sampled run would spend exactly what the gate is meant to save.
        """
        from run_l1_gate import run_l1_gate

        return run_l1_gate(skill_path, gt_path)

    def _run_full_eval(self, skill_path: Path, gt_path: Path,
                  split: str = "dev",
                  cases_dir: Path | None = None) -> dict:
        """Run every case in ``split`` and grade the outputs.

        Returns the same shape as the existing evaluator — ``pass_rate``,
        ``total_passed``, ``total_assertions``, ``failed``, ``tokens``,
        ``duration``, ``cases`` — so the gate, the results log and the
        report all keep working unchanged. Multi-dimensional metrics are
        added alongside rather than replacing anything, because a
        downstream reader that only knows ``pass_rate`` must not break.
        """
        t0 = time.time()
        target = self._target(skill_path)
        instruction = target.context()
        cases = self._cases(gt_path, split)

        judgments = []
        records = []
        for case in cases:
            verdict, output = self._grade_one(instruction, case)
            judgments.append(verdict)
            records.append(self._record(case, verdict, output))

        if cases_dir is not None and records:

            write_cases_to_dir(Path(cases_dir), records)

        result = self._result(judgments, records, time.time() - t0)
        # Measured once here rather than left to the gate to collect. The
        # gate is a pure function of the numbers it is handed, and giving it
        # filesystem access to go and measure the artifact itself would make
        # its verdict depend on when it ran.
        result["snapshot"] = target.snapshot()
        return result

    def info(self) -> dict:
        return {
            "name": self.name,
            "type": type(self).__name__,
            "grader": type(self.grader).__name__,
            "runner": type(self.runner).__name__,
            "primary_metric": self._primary_metric(),
        }

    # ── conversation mode ────────────────────
    #
    # `_run_full_eval` above reaches a model through `self.runner`, which
    # means through a CLI or an HTTP endpoint. That is the fallback path,
    # not the primary one: this engine ships as a Skill, and whoever
    # installs it has their own agent's model and nothing else — no
    # `claude` binary on PATH, no endpoint, no credentials. A path that
    # requires those is a path they cannot run.
    #
    # A Python method cannot block and wait for the agent driving it to
    # produce a completion, so the work is split. Each stage does only
    # what code can do — render prompts, parse replies, compute scores —
    # and hands the model calls back to the driver in between.

    def build_run_specs(self, skill_path: Path, gt_path: Path,
                        split: str = "dev") -> dict:
        """Stage 1: what to ask, for every case in ``split``.

        Calls no model. Returns the prompts to send and everything stage 2
        needs to score the replies without re-reading anything.

        The instruction and the snapshot are captured **here** and carried
        through, rather than re-read in stage 2. Between the two stages the
        driver may well have rewritten the artifact — that is what the loop
        does — and a stage 2 that re-read from disk would score replies
        produced by the old text against a snapshot of the new.

        Returns:
            dict with ``run_specs`` (one ``{case_id, prompt}`` per case),
            ``instruction``, ``snapshot``, and ``cases_by_id``. Pass it
            back to :meth:`grade_outputs` unchanged.
        """
        target = self._target(skill_path)
        instruction = target.context()
        cases = self._cases(gt_path, split)

        # Positional ids, not `case.get("id")`. Two cases without an id
        # would both answer to the same key and the second would silently
        # replace the first — losing a case while every count still looked
        # consistent. The dataset loader assigns ids, but this method is
        # public and takes whatever it is given.
        cases_by_id = {self._case_key(case, i): case
                       for i, case in enumerate(cases)}

        return {
            "run_specs": [
                {"case_id": case_id,
                 "prompt": self._render_run_prompt(instruction, case)}
                for case_id, case in cases_by_id.items()
            ],
            "instruction": instruction,
            "snapshot": target.snapshot(),
            "cases_by_id": cases_by_id,
        }

    def grade_outputs(self, staged: Mapping, outputs: Mapping[str, str],
                      cases_dir: Path | None = None) -> dict:
        """Stage 2: score the replies the driver collected.

        Calls no model **for graders that need none** — which is what makes
        this usable with no transport at all. A grader that checks its
        expectations in Python never leaves the process, so a run using one
        needs nothing installed. A judge-backed grader will still try to
        reach a model from here, which is a limit of this stage split rather
        than of the mode: grading in the conversation needs the same
        treatment as running, and that is not done yet.

        Args:
            staged: exactly what :meth:`build_run_specs` returned.
            outputs: ``{case_id: reply_text}``. A case_id absent from this
                mapping is recorded as unmeasured rather than as a zero —
                the driver's call for it may simply have failed, and
                scoring that as a bad answer would blame the candidate for
                the harness.
            cases_dir: optional directory to persist per-case records.

        Returns:
            The same shape as :meth:`full_eval`, so the gate and the logs
            need no knowledge of which mode produced it.
        """
        t0 = time.time()
        judgments, records = [], []
        for case_id, case in staged["cases_by_id"].items():
            output = outputs.get(case_id)
            if output is None:
                verdict = judgment.Judgment.failed(
                    str(case_id),
                    error="no output was collected for this case",
                    feedback=(
                        "The driver did not supply a reply for this case, so "
                        "it was not measured. This reflects the harness, not "
                        "the candidate."
                    ),
                )
                output = ""
            else:
                # Tell a driver-supplied judge which case this is, so its
                # question keys match the ones collected earlier. Without
                # it every question is keyed to the same placeholder case
                # and no answer is ever found — the round then reports every
                # case as unmeasured while the answers sit unused.
                self._announce_case(case_id)
                verdict = self.grader.grade(case, output)
            judgments.append(verdict)
            records.append(self._record(case, verdict, output))

        if cases_dir is not None and records:
            write_cases_to_dir(Path(cases_dir), records)

        result = self._result(judgments, records, time.time() - t0)
        result["snapshot"] = staged.get("snapshot")
        return result

    def collect_grading_questions(self, staged: Mapping,
                                  outputs: Mapping[str, str]) -> list[dict]:
        """The questions a judge-backed grader needs answered.

        Between :meth:`build_run_specs` and :meth:`grade_outputs` for a
        grader that judges semantically. Returns
        ``[{key, kind, case_id, prompt}, ...]`` for the driver to answer with
        its own model; pass ``{key: reply}`` to :meth:`grade_outputs` and the
        scoring runs with no transport.

        Returns an empty list for a grader that needs no model, so a caller
        can treat "nothing to ask" and "programmatic grading" as the same
        case rather than branching on which grader it holds.

        The questions are produced by *grading the outputs* with a judge in
        collect mode, not by a separate prompt-rendering path. The scores
        from that pass are placeholders and discarded — the questions are
        the point. Rendering them any other way would be a second
        implementation of every prompt, free to drift from the one that
        scores, and the drift would look like a judging difference.
        """
        judge = self._collecting_judge()
        if judge is None:
            return []
        for case_id, case in staged["cases_by_id"].items():
            output = outputs.get(case_id)
            if output is None:
                continue
            self._announce_case(case_id)
            # Discarded: grading with placeholder answers scores nothing.
            # It is the act of grading that reaches every question.
            self.grader.grade(case, output)
        return judge.pending()

    def _announce_case(self, case_id) -> None:
        """Tell a driver-supplied judge which case is being graded.

        A no-op for a judge that reaches a model itself — it has no need to
        key anything. Asked for by attribute rather than by type so that
        this class still holds no knowledge of which judge it has.
        """
        holder = self._judge_holder()
        if holder is None:
            return
        announce = getattr(holder.judge, "for_case", None)
        if callable(announce):
            announce(str(case_id))

    def _collecting_judge(self):
        """Swap in a question-recording judge, or report there is none to swap.

        Returns None when the grader exposes no judge — which is how a
        purely programmatic grader announces that it needs no model, without
        this method having to know which grader that is.
        """
        from driver_judge import DriverJudge

        holder = self._judge_holder()
        if holder is None:
            return None
        judge = DriverJudge(mode="collect")
        holder.judge = judge
        return judge

    def _judge_holder(self):
        """Whichever object owns the judge, or None if nothing does.

        A composite grader may wrap several graders, each with its own
        judge; this returns the first that has one. Located by attribute
        rather than by type, so a new grader needs no change here.
        """
        if hasattr(self.grader, "judge"):
            return self.grader
        for name in vars(self.grader):
            candidate = getattr(self.grader, name)
            if hasattr(candidate, "judge"):
                return candidate
        return None

    def apply_grading_answers(self, answers: Mapping[str, str]) -> None:
        """Put the driver's answers where the grader will find them.

        Call between :meth:`collect_grading_questions` and
        :meth:`grade_outputs`. Separate from ``grade_outputs`` so that the
        answers are installed once for the whole round rather than threaded
        through a per-case argument that only one kind of grader would use.
        """
        from driver_judge import DriverJudge

        holder = self._judge_holder()
        if holder is not None:
            holder.judge = DriverJudge(mode="replay", answers=answers)

    def _render_run_prompt(self, instruction: str, case: Mapping) -> str:
        """The text to send for one case.

        Delegates to the runner when it knows how to build one, so that
        conversation mode and CLI mode send the *same* prompt — a runner
        with its own template would otherwise be optimizing against a
        different input than the one measured here.
        """
        build = getattr(self.runner, "render_prompt", None)
        if callable(build):
            return build(instruction, case)
        return f"{instruction}\n\n---\n\n{case.get('input', '')}"

    @staticmethod
    def _case_key(case: Mapping, index: int) -> str:
        """A key that is unique even when the case has no id."""
        declared = str(case.get("id", "")).strip()
        return declared or f"case-{index}"

    # ── internals ────────────────────────────

    def _target(self, path: Path) -> Target:
        return resolve_target(path, self.section)

    def _cases(self, gt_path: Path, split: str) -> list[dict]:
        """Load the dataset and return the requested split.

        An empty split name means every case. A name that exists but holds
        nothing returns nothing — silently falling back to all cases would
        make a typo in a split name look like a successful run over the
        wrong data.
        """
        cases = load_cases(gt_path, self.columns)
        if not split:
            return cases
        partitioned = split_cases(cases, self.splits, stratify=self.stratify)
        return partitioned.get(split, [])

    def _grade_one(self, instruction: str, case: Mapping):
        """Produce output for one case and grade it.

        A runner failure becomes an errored judgment rather than an empty
        output. The distinction matters: an empty output grades as a total
        miss, which the loop would read as the candidate being terrible,
        when in fact nothing was measured.

        The returned text is checked for a transport-failure marker as well
        as the call raising. That belt-and-braces is deliberate: a custom
        runner is injected by the caller and cannot be relied on to convert
        the backend's return-a-string convention into an exception, and the
        cost of missing one is a confident zero for a candidate that was
        never run.
        """
        case_id = str(case.get("id", "?"))
        failed_feedback = (
            "The candidate could not be run, so this case was not measured. "
            "This reflects the harness, not the candidate."
        )
        try:
            output = self.runner(instruction, dict(case))
        except Exception as exc:  # noqa: BLE001 - see docstring
            return (
                judgment.Judgment.failed(
                    case_id,
                    error=f"runner: {type(exc).__name__}: {exc}",
                    feedback=failed_feedback,
                ),
                "",
            )

        from llm import is_llm_error  # local import: avoids a load-time cycle

        if is_llm_error(output):
            return (
                judgment.Judgment.failed(
                    case_id,
                    error=f"runner: {str(output).strip()[:200]}",
                    feedback=failed_feedback,
                ),
                "",
            )
        return self.grader.grade(case, output), output

    def _record(self, case: Mapping, verdict, output: str) -> dict:
        """Per-case record, in the shape the existing case files use.

        Keeps ``id`` / ``pass`` / ``assertions`` so the tooling that greps
        these files keeps working, and adds the fields this evaluator can
        supply that the text-matching one cannot: the actual output, the
        metrics, and the diagnosis.
        """
        return {
            "id": verdict.case_id,
            "pass": verdict.passed,
            "error": verdict.error,
            "metrics": dict(verdict.metrics),
            "primary": verdict.primary,
            "score": verdict.score if verdict.metrics else None,
            "feedback": verdict.feedback,
            "evidence": dict(verdict.evidence),
            "output": output,
            "input": case.get("input", ""),
            "stratum": case.get("stratum", ""),
            "split": case.get("split", ""),
            # Named `assertions` for continuity with the existing case
            # format, which several tools read.
            "assertions": [],
        }

    def _result(self, judgments: Sequence, records: Sequence, elapsed: float) -> dict:
        """Roll per-case judgments into the loop's result dict.

        ``pass_rate`` is computed over cases that were actually measured.
        Errored cases are reported separately rather than counted as
        failures, because averaging a harness fault into the score would
        make the candidate look worse than it is and could reject a real
        improvement.
        """
        rollup = judgment.aggregate(list(judgments))

        return {
            # Existing contract.
            "pass_rate": rollup["pass_rate"],
            "total_passed": rollup["passed"],
            "total_assertions": rollup["scored"],
            "failed": [
                r["id"] for r in records if not r["pass"] and not r["error"]
            ],
            "tokens": rollup["cost"]["tokens"],
            "duration": round(elapsed, 2),
            "cases": list(records),
            # Added alongside, never replacing.
            "metrics": rollup["metrics"],
            "primary": self._primary_metric(),
            "errored": rollup["errored"],
            "errors": [
                {"id": r["id"], "error": r["error"]} for r in records if r["error"]
            ],
        }

    def _primary_metric(self) -> str:
        """Which metric the gate should rank on.

        Read from the grader because only it knows which of its metrics is
        meaningful — a grader that cannot observe unrequested content has
        no useful precision, and ranking it on f1 would halve a perfectly
        good recall.
        """
        return getattr(self.grader, "primary_metric", "f1")
