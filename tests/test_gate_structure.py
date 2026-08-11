"""Tests for the structural gate and per-dimension metric floors.

The acceptance criterion is that a candidate which grew is rejected. The
harder property is that the gate does this without asking what kind of
artifact it is looking at: testing whether a key is present would be a type
check wearing a dictionary lookup as a disguise, and the uniform snapshot
contract exists precisely so the gate never has to.

Existing gate behaviour must be unchanged, so the first group re-asserts it
against real return values.
"""

import ast
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "plugin" / "skills" / "skill-evolver" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from gate import (  # noqa: E402
    SNAPSHOT_KEYS,
    check_metric_thresholds,
    check_structure,
    phase_6_gate_decision,
)
from target import SNAPSHOT_KEYS as TARGET_SNAPSHOT_KEYS  # noqa: E402


def snapshot(**overrides):
    """A snapshot with every contract key present, as a target produces."""
    base = {
        "chars": 1000, "lines": 40, "non_empty_lines": 30,
        "child_units": 2, "child_lines": 100,
    }
    base.update(overrides)
    return base


def improving(**overrides):
    """Metrics that clear the quality gate, so other gates decide the outcome."""
    base = {"pass_rate": 0.80, "l1_pass": True}
    base.update(overrides)
    return base


BASELINE = {"pass_rate": 0.70, "tokens_mean": 100, "duration_mean": 10}


# ─────────────────────────────────────────────
# Regression: existing behaviour
# ─────────────────────────────────────────────

class ExistingGateBehaviourTests(unittest.TestCase):
    def test_a_clear_improvement_is_still_kept(self):
        result = phase_6_gate_decision(improving(), BASELINE)
        self.assertEqual(result["decision"], "keep")

    def test_no_improvement_is_still_discarded(self):
        result = phase_6_gate_decision(
            improving(pass_rate=0.70), BASELINE
        )
        self.assertEqual(result["decision"], "discard")

    def test_a_crash_still_reverts(self):
        result = phase_6_gate_decision({"status": "crash"}, BASELINE)
        self.assertEqual(result["decision"], "revert")

    def test_an_l1_failure_still_discards(self):
        result = phase_6_gate_decision(
            improving(l1_pass=False), BASELINE
        )
        self.assertEqual(result["decision"], "discard")

    def test_a_holdout_regression_still_vetoes(self):
        result = phase_6_gate_decision(
            improving(holdout_pass_rate=0.50),
            {**BASELINE, "holdout_pass_rate": 0.70},
        )
        self.assertEqual(result["decision"], "discard")

    def test_a_cost_blowout_still_discards(self):
        result = phase_6_gate_decision(
            improving(tokens_mean=500), BASELINE
        )
        self.assertEqual(result["decision"], "discard")

    def test_the_new_gates_are_inert_without_configuration(self):
        """Adding gates must not change any existing verdict.

        A candidate with no snapshot and no floors set has to behave exactly
        as it did before these gates existed.
        """
        self.assertEqual(
            phase_6_gate_decision(improving(), BASELINE)["decision"], "keep"
        )


# ─────────────────────────────────────────────
# The structural gate
# ─────────────────────────────────────────────

class StructureGrowthTests(unittest.TestCase):
    def test_holding_size_steady_passes(self):
        ok, reasons = check_structure(snapshot(), snapshot())
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_shrinking_passes(self):
        ok, _ = check_structure(snapshot(chars=500), snapshot())
        self.assertTrue(ok)

    def test_growth_within_the_allowance_passes(self):
        ok, _ = check_structure(snapshot(chars=1200), snapshot())
        self.assertTrue(ok)

    def test_growth_beyond_the_allowance_fails(self):
        """The acceptance criterion: a bloated candidate is rejected."""
        ok, reasons = check_structure(snapshot(chars=2000), snapshot())
        self.assertFalse(ok)
        self.assertIn("chars", reasons[0])

    def test_the_allowance_is_configurable(self):
        grown = snapshot(chars=1200)
        self.assertTrue(check_structure(grown, snapshot())[0])
        self.assertFalse(
            check_structure(grown, snapshot(),
                            {"max_structure_growth": 0.05})[0]
        )

    def test_growth_in_any_dimension_is_caught(self):
        for key in SNAPSHOT_KEYS:
            with self.subTest(key=key):
                base = snapshot()
                grown = snapshot(**{key: base[key] * 3})
                ok, reasons = check_structure(grown, base)
                self.assertFalse(ok, f"{key} growth was not caught")
                self.assertIn(key, reasons[0])

    def test_relocating_bulk_into_child_files_is_still_caught(self):
        """Shrinking the entry point while moving the words elsewhere.

        A gate reading only the main file's size would call this an
        improvement.
        """
        base = snapshot(chars=1000, child_lines=100)
        moved = snapshot(chars=600, child_lines=400)
        ok, reasons = check_structure(moved, base)
        self.assertFalse(ok)
        self.assertTrue(any("child_lines" in r for r in reasons))

    def test_every_offending_dimension_is_reported(self):
        """Fixing one at a time would take several runs to resolve."""
        base = snapshot()
        grown = snapshot(chars=5000, lines=400, child_lines=900)
        ok, reasons = check_structure(grown, base)
        self.assertFalse(ok)
        self.assertGreaterEqual(len(reasons), 3)


class StructureCapTests(unittest.TestCase):
    def test_an_absolute_cap_is_enforced(self):
        ok, reasons = check_structure(
            snapshot(lines=400), snapshot(lines=390),
            {"max_structure": {"lines": 200}},
        )
        self.assertFalse(ok)
        self.assertIn("cap 200", reasons[0])

    def test_a_cap_applies_without_any_baseline(self):
        """Useful on the very first iteration, when nothing came before."""
        ok, _ = check_structure(
            snapshot(lines=400), None, {"max_structure": {"lines": 200}}
        )
        self.assertFalse(ok)

    def test_staying_under_the_cap_passes(self):
        """Baseline held steady, so only the cap is under test here."""
        ok, reasons = check_structure(
            snapshot(lines=100), snapshot(lines=100),
            {"max_structure": {"lines": 200}},
        )
        self.assertTrue(ok, reasons)

    def test_a_cap_on_child_units_limits_module_count(self):
        ok, reasons = check_structure(
            snapshot(child_units=9), snapshot(child_units=8),
            {"max_structure": {"child_units": 3}},
        )
        self.assertFalse(ok)
        self.assertIn("child_units", reasons[0])


class StructureAbsentDataTests(unittest.TestCase):
    def test_no_snapshot_at_all_passes(self):
        """No structural signal is not a failure.

        Failing here would block every run that does not collect one.
        """
        self.assertTrue(check_structure(None, None)[0])
        self.assertTrue(check_structure({}, {})[0])

    def test_a_current_snapshot_with_no_baseline_passes_growth_checks(self):
        self.assertTrue(check_structure(snapshot(), None)[0])

    def test_a_zero_baseline_dimension_is_not_infinite_growth(self):
        """A first reference file must not be rejected for existing."""
        ok, _ = check_structure(
            snapshot(child_units=1, child_lines=20),
            snapshot(child_units=0, child_lines=0),
        )
        self.assertTrue(ok)

    def test_a_non_numeric_value_is_skipped_not_crashed_on(self):
        ok, _ = check_structure(
            {**snapshot(), "chars": "many"}, snapshot()
        )
        self.assertTrue(ok)

    def test_extra_keys_are_ignored(self):
        """Shape-specific detail lives under `extra` and must not be read."""
        current = {**snapshot(), "extra": {"share_of_file": 0.9, "section": "R"}}
        base = {**snapshot(), "extra": {"share_of_file": 0.1, "section": "R"}}
        self.assertTrue(check_structure(current, base)[0])


class StructureUniformityTests(unittest.TestCase):
    """The property that keeps the gate free of type branching."""

    def test_the_gate_shares_one_snapshot_contract_with_target(self):
        """A local copy would drift the moment a shape changed."""
        self.assertEqual(tuple(SNAPSHOT_KEYS), tuple(TARGET_SNAPSHOT_KEYS))

    def test_the_gate_does_no_key_presence_test_on_snapshots(self):
        """`if "share_of_file" in snap` is a type check in disguise.

        Checked against code only. Explaining why such a test is forbidden
        requires naming one, and a check that flagged its own rationale
        would be deleted — leaving the real invariant unguarded.
        """
        tree = ast.parse((SCRIPTS / "gate.py").read_text())
        docstrings = {
            id(node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        code_strings = [
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]
        for shape_specific in ("share_of_file", "supporting_files",
                              "files_per_dir", "section", "file_chars"):
            for literal in code_strings:
                self.assertNotIn(
                    shape_specific, literal,
                    f"shape-specific key {shape_specific!r} read in code",
                )

    def test_the_gate_contains_no_isinstance_on_targets(self):
        tree = ast.parse((SCRIPTS / "gate.py").read_text())
        checked = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "isinstance"):
                second = node.args[1]
                names = ast.unparse(second)
                checked.append(names)
        # The only permitted use is "is this a number", which distinguishes
        # a value from missing data rather than one artifact shape from
        # another.
        for names in checked:
            self.assertIn("int", names, f"unexpected isinstance on {names}")

    def test_all_three_target_shapes_pass_through_the_same_path(self):
        """Real snapshots from real targets, not hand-written dicts."""
        import json
        import tempfile

        from target import PromptFileTarget, SectionTarget, SkillTarget

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            skill = tmp / "demo"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: A demo skill.\n---\n\nBody.\n"
            )
            prompt = tmp / "p.md"
            prompt.write_text("An instruction.\n")
            doc = tmp / "d.md"
            doc.write_text("## Rules\n\nBe brief.\n\n## Other\n\nx\n")

            targets = [
                SkillTarget(skill),
                PromptFileTarget(prompt),
                SectionTarget(doc, "Rules"),
            ]
            for target in targets:
                with self.subTest(shape=type(target).__name__):
                    snap = target.snapshot()
                    ok, reasons = check_structure(snap, snap)
                    self.assertTrue(ok, reasons)
                    # And a tripled version of the same snapshot fails, on
                    # every shape, through the same code path.
                    grown = {
                        k: (v * 3 if isinstance(v, (int, float)) else v)
                        for k, v in snap.items()
                    }
                    if any(snap[k] for k in SNAPSHOT_KEYS):
                        self.assertFalse(check_structure(grown, snap)[0])
                    # Serialisable, since it goes into the results log.
                    json.dumps(snap)


# ─────────────────────────────────────────────
# Per-dimension metric floors
# ─────────────────────────────────────────────

class MetricFloorTests(unittest.TestCase):
    def current(self, **metrics):
        return {"metrics": metrics}

    def test_no_floors_configured_passes(self):
        ok, reasons = check_metric_thresholds(
            self.current(precision=0.1, recall=0.1), {}
        )
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_a_metric_below_its_floor_fails(self):
        ok, reasons = check_metric_thresholds(
            self.current(precision=0.5, recall=0.9), {},
            {"min_metrics": {"precision": 0.9}},
        )
        self.assertFalse(ok)
        self.assertIn("precision", reasons[0])

    def test_dimensions_are_independent(self):
        """Ranking on one blended number cannot express this constraint.

        High recall must not buy a collapse in precision: one means content
        was invented, the other that expectations were missed.
        """
        ok, _ = check_metric_thresholds(
            self.current(precision=0.4, recall=1.0), {},
            {"min_metrics": {"precision": 0.9}},
        )
        self.assertFalse(ok)

    def test_meeting_every_floor_passes(self):
        ok, _ = check_metric_thresholds(
            self.current(precision=0.95, recall=0.92), {},
            {"min_metrics": {"precision": 0.9, "recall": 0.9}},
        )
        self.assertTrue(ok)

    def test_a_floor_on_an_unreported_metric_is_reported(self):
        """A floor believed active but never evaluated is worse than none."""
        ok, reasons = check_metric_thresholds(
            self.current(recall=0.9), {}, {"min_metrics": {"precision": 0.9}}
        )
        self.assertFalse(ok)
        self.assertIn("not reported", reasons[0])

    def test_unnamed_dimensions_are_not_checked(self):
        """Inventing a default floor would reject against a bar nobody set."""
        ok, _ = check_metric_thresholds(
            self.current(precision=0.9, f1=0.01), {},
            {"min_metrics": {"precision": 0.9}},
        )
        self.assertTrue(ok)

    def test_every_violation_is_reported(self):
        ok, reasons = check_metric_thresholds(
            self.current(precision=0.1, recall=0.1), {},
            {"min_metrics": {"precision": 0.9, "recall": 0.9}},
        )
        self.assertEqual(len(reasons), 2)


class MetricRegressionTests(unittest.TestCase):
    def test_a_dimension_regressing_beyond_tolerance_fails(self):
        """Catches a trade the primary metric is designed not to notice."""
        ok, reasons = check_metric_thresholds(
            {"metrics": {"precision": 0.60, "recall": 0.99}},
            {"metrics": {"precision": 0.95, "recall": 0.70}},
            {"max_metric_regression": {"precision": 0.05}},
        )
        self.assertFalse(ok)
        self.assertIn("regressed", reasons[0])

    def test_a_regression_within_tolerance_passes(self):
        ok, _ = check_metric_thresholds(
            {"metrics": {"precision": 0.93}},
            {"metrics": {"precision": 0.95}},
            {"max_metric_regression": {"precision": 0.05}},
        )
        self.assertTrue(ok)

    def test_an_improvement_passes(self):
        ok, _ = check_metric_thresholds(
            {"metrics": {"precision": 0.99}},
            {"metrics": {"precision": 0.95}},
            {"max_metric_regression": {"precision": 0.05}},
        )
        self.assertTrue(ok)

    def test_a_missing_baseline_dimension_is_not_a_regression(self):
        ok, _ = check_metric_thresholds(
            {"metrics": {"precision": 0.5}}, {"metrics": {}},
            {"max_metric_regression": {"precision": 0.05}},
        )
        self.assertTrue(ok)

    def test_no_metrics_at_all_passes(self):
        ok, _ = check_metric_thresholds({}, {}, {"min_metrics": {}})
        self.assertTrue(ok)


# ─────────────────────────────────────────────
# Integration through the real decision
# ─────────────────────────────────────────────

class GateIntegrationTests(unittest.TestCase):
    def test_a_bloated_candidate_is_discarded_despite_better_quality(self):
        """The acceptance criterion, through the actual gate.

        Quality alone must not buy unlimited growth — otherwise every
        iteration adds one more clause and the result scores well while
        becoming unusable.
        """
        result = phase_6_gate_decision(
            improving(snapshot=snapshot(chars=5000)),
            {**BASELINE, "snapshot": snapshot()},
        )
        self.assertEqual(result["decision"], "discard")
        self.assertTrue(any("structure FAIL" in r for r in result["reasons"]))

    def test_a_lean_improvement_is_kept(self):
        result = phase_6_gate_decision(
            improving(snapshot=snapshot(chars=900)),
            {**BASELINE, "snapshot": snapshot()},
        )
        self.assertEqual(result["decision"], "keep")

    def test_a_precision_collapse_is_discarded_despite_better_pass_rate(self):
        result = phase_6_gate_decision(
            improving(metrics={"precision": 0.30, "recall": 0.99}),
            {**BASELINE, "metrics": {"precision": 0.95, "recall": 0.70}},
            {"min_metrics": {"precision": 0.9}},
        )
        self.assertEqual(result["decision"], "discard")
        self.assertTrue(any("metric FAIL" in r for r in result["reasons"]))

    def test_both_new_gates_can_fail_together_and_both_are_reported(self):
        result = phase_6_gate_decision(
            improving(snapshot=snapshot(chars=5000),
                      metrics={"precision": 0.1}),
            {**BASELINE, "snapshot": snapshot(),
             "metrics": {"precision": 0.95}},
            {"min_metrics": {"precision": 0.9}},
        )
        self.assertEqual(result["decision"], "discard")
        self.assertTrue(any("structure FAIL" in r for r in result["reasons"]))
        self.assertTrue(any("metric FAIL" in r for r in result["reasons"]))

    def test_the_reasons_survive_alongside_the_existing_ones(self):
        result = phase_6_gate_decision(
            improving(snapshot=snapshot(chars=5000)),
            {**BASELINE, "snapshot": snapshot()},
        )
        self.assertTrue(any(r.startswith("quality") for r in result["reasons"]))
        self.assertTrue(any("structure" in r for r in result["reasons"]))


class TriggerGateVisibilityTests(unittest.TestCase):
    def test_an_inactive_trigger_gate_says_so(self):
        """Otherwise the log looks the same as a gate that approved.

        With the defaults this check passes unconditionally, so a change
        that harmed trigger accuracy could be kept while the log appeared
        to show the guard working.
        """
        result = phase_6_gate_decision(improving(), BASELINE)
        self.assertTrue(
            any("trigger not evaluated" in r for r in result["reasons"])
        )

    def test_an_active_trigger_gate_does_not_claim_to_be_inactive(self):
        result = phase_6_gate_decision(
            improving(trigger_f1=0.95), {**BASELINE, "trigger_f1": 0.94}
        )
        self.assertFalse(
            any("trigger not evaluated" in r for r in result["reasons"])
        )

    def test_a_real_trigger_regression_still_fails(self):
        result = phase_6_gate_decision(
            improving(trigger_f1=0.40), {**BASELINE, "trigger_f1": 0.90}
        )
        self.assertEqual(result["decision"], "discard")
        self.assertTrue(any("trigger FAIL" in r for r in result["reasons"]))

    def test_the_verdict_is_unchanged_by_the_new_message(self):
        """Visibility only; the decision must be identical to before."""
        self.assertEqual(
            phase_6_gate_decision(improving(), BASELINE)["decision"], "keep"
        )


class PreexistingUncoveredPathTests(unittest.TestCase):
    """Decision branches that had no test before this step.

    Not strictly part of the structural gate, but they are live paths in the
    same function: an untested branch that decides keep-or-discard is a
    branch that can invert silently.
    """

    def test_saturated_dev_with_a_holdout_regression_reports_holdout(self):
        """Dev at the ceiling, holdout below its required improvement."""
        result = phase_6_gate_decision(
            {"pass_rate": 1.0, "holdout_pass_rate": 0.71, "l1_pass": True},
            {"pass_rate": 1.0, "holdout_pass_rate": 0.70,
             "tokens_mean": 100, "duration_mean": 10},
        )
        self.assertEqual(result["decision"], "discard")
        self.assertTrue(
            any("holdout" in r and "FAIL" in r for r in result["reasons"])
        )

    def test_saturated_dev_with_no_holdout_has_no_signal_to_improve(self):
        """The honest call is to refuse, not to guess."""
        result = phase_6_gate_decision(
            {"pass_rate": 1.0, "l1_pass": True},
            {"pass_rate": 1.0, "tokens_mean": 100, "duration_mean": 10},
        )
        self.assertEqual(result["decision"], "discard")
        self.assertTrue(
            any("no holdout" in r for r in result["reasons"])
        )

    def test_a_regression_suite_drop_discards(self):
        result = phase_6_gate_decision(
            improving(regression_pass=0.50),
            {**BASELINE, "regression_pass": 1.0},
        )
        self.assertEqual(result["decision"], "discard")
        self.assertTrue(any("regression FAIL" in r for r in result["reasons"]))

    def test_a_latency_blowout_discards(self):
        result = phase_6_gate_decision(
            improving(duration_mean=100), BASELINE
        )
        self.assertEqual(result["decision"], "discard")
        self.assertTrue(any("latency FAIL" in r for r in result["reasons"]))


    def test_both_saturated_with_a_dev_regression_reports_dev(self):
        """Both splits at the ceiling and dev slipped.

        The last untested branch: with nothing left to improve, the only bar
        is "did not regress", and the message must name which split broke it.
        """
        result = phase_6_gate_decision(
            {"pass_rate": 0.80, "holdout_pass_rate": 1.0, "l1_pass": True},
            {"pass_rate": 1.0, "holdout_pass_rate": 1.0,
             "tokens_mean": 100, "duration_mean": 10},
        )
        self.assertEqual(result["decision"], "discard")
        self.assertTrue(
            any("dev regressed" in r for r in result["reasons"]),
            result["reasons"],
        )

    def test_both_saturated_and_holding_is_kept(self):
        """Otherwise a saturated skill could never accept a safe change."""
        result = phase_6_gate_decision(
            {"pass_rate": 1.0, "holdout_pass_rate": 1.0, "l1_pass": True},
            {"pass_rate": 1.0, "holdout_pass_rate": 1.0,
             "tokens_mean": 100, "duration_mean": 10},
        )
        self.assertEqual(result["decision"], "keep")


    def test_saturated_dev_regressing_reports_dev_not_holdout(self):
        """Dev was at the ceiling and slipped, while holdout still has room.

        The diagnosis must name dev: reporting the holdout shortfall instead
        would send the next iteration after the wrong problem.
        """
        result = phase_6_gate_decision(
            {"pass_rate": 0.80, "holdout_pass_rate": 0.75, "l1_pass": True},
            {"pass_rate": 1.0, "holdout_pass_rate": 0.70,
             "tokens_mean": 100, "duration_mean": 10},
        )
        self.assertEqual(result["decision"], "discard")
        self.assertTrue(
            any("dev saturated): dev regressed" in r for r in result["reasons"]),
            result["reasons"],
        )


if __name__ == "__main__":
    unittest.main()
