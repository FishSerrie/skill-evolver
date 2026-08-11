import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "plugin" / "skills" / "skill-evolver" / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from gate import phase_6_gate_decision


def _metrics(pass_rate=1.0, holdout_pass_rate=None, tokens_mean=0,
            duration_mean=0.0, trigger_f1=1.0, regression_pass=1.0,
            l1_pass=True, status="pass"):
    return {"pass_rate": pass_rate, "holdout_pass_rate": holdout_pass_rate,
            "l1_pass": l1_pass, "trigger_f1": trigger_f1,
            "tokens_mean": tokens_mean, "duration_mean": duration_mean,
            "regression_pass": regression_pass, "status": status}


THRESHOLDS = {"min_delta": 0.02, "noise_threshold": 0.01}


class BothSurfacesSaturatedTests(unittest.TestCase):
    """Real bug found via a live evolve run: once dev AND holdout are
    both already at the ceiling (1.0), the old dev_saturated branch
    demanded holdout_pass_rate >= 1.0 + min_delta — mathematically
    impossible — making "keep" permanently unreachable for any future
    change, including a verified-correct, no-regression documentation
    fix. The fix degrades one more step: when holdout is ALSO
    saturated, the bar is "no regression on either split", not
    "improve the unimprovable"."""

    def test_flat_no_regression_at_full_saturation_is_keep(self):
        current = _metrics(pass_rate=1.0, holdout_pass_rate=1.0)
        baseline = _metrics(pass_rate=1.0, holdout_pass_rate=1.0)
        result = phase_6_gate_decision(current, baseline, THRESHOLDS)
        self.assertEqual(result["decision"], "keep")

    def test_holdout_regression_at_full_saturation_is_discard(self):
        current = _metrics(pass_rate=1.0, holdout_pass_rate=0.9)
        baseline = _metrics(pass_rate=1.0, holdout_pass_rate=1.0)
        result = phase_6_gate_decision(current, baseline, THRESHOLDS)
        self.assertEqual(result["decision"], "discard")
        self.assertTrue(any("holdout" in r for r in result["reasons"]))

    def test_dev_regression_at_full_saturation_is_discard(self):
        current = _metrics(pass_rate=0.9, holdout_pass_rate=1.0)
        baseline = _metrics(pass_rate=1.0, holdout_pass_rate=1.0)
        result = phase_6_gate_decision(current, baseline, THRESHOLDS)
        self.assertEqual(result["decision"], "discard")

    def test_dev_saturated_holdout_not_saturated_still_requires_improvement(self):
        # Preserve existing behavior for the (dev-only-saturated) case —
        # this is NOT the new branch, holdout still has to improve.
        current = _metrics(pass_rate=1.0, holdout_pass_rate=0.85)
        baseline = _metrics(pass_rate=1.0, holdout_pass_rate=0.85)
        result = phase_6_gate_decision(current, baseline, THRESHOLDS)
        self.assertEqual(result["decision"], "discard")

    def test_dev_saturated_holdout_not_saturated_improvement_still_keeps(self):
        current = _metrics(pass_rate=1.0, holdout_pass_rate=0.90)
        baseline = _metrics(pass_rate=1.0, holdout_pass_rate=0.85)
        result = phase_6_gate_decision(current, baseline, THRESHOLDS)
        self.assertEqual(result["decision"], "keep")


class ZeroBaselineCostLatencyTests(unittest.TestCase):
    """Real bug found via adversarial review: `base == 0 or ...` was
    meant as a divide-by-zero guard but actually disabled cost/latency
    gating entirely whenever the baseline reported zero — reachable in
    practice because behavioral_runner.py's CLI path always reports an
    honest zero token count, not an estimate."""

    def test_zero_baseline_and_zero_current_tokens_passes_cost_gate(self):
        # Non-saturated quality improvement so cost is the only
        # dimension under test (dev-saturated-without-holdout always
        # discards regardless of cost/latency — a separate, correct
        # branch, not what this test is checking).
        current = _metrics(pass_rate=0.9, tokens_mean=0)
        baseline = _metrics(pass_rate=0.85, tokens_mean=0)
        result = phase_6_gate_decision(current, baseline, THRESHOLDS)
        self.assertEqual(result["decision"], "keep")

    def test_zero_baseline_but_nonzero_current_tokens_fails_cost_gate(self):
        # This is the exact repro an adversarial reviewer found: a
        # quality improvement used to silently "keep" here because
        # `base_tokens == 0` short-circuited cost_ok to True regardless
        # of cur_tokens.
        current = _metrics(pass_rate=0.6, tokens_mean=999999)
        baseline = _metrics(pass_rate=0.5, tokens_mean=0)
        result = phase_6_gate_decision(current, baseline, THRESHOLDS)
        self.assertEqual(result["decision"], "discard")
        self.assertTrue(any("cost FAIL" in r for r in result["reasons"]))

    def test_zero_baseline_and_zero_current_duration_passes_latency_gate(self):
        current = _metrics(pass_rate=0.9, duration_mean=0.0)
        baseline = _metrics(pass_rate=0.85, duration_mean=0.0)
        result = phase_6_gate_decision(current, baseline, THRESHOLDS)
        self.assertEqual(result["decision"], "keep")

    def test_zero_baseline_but_nonzero_current_duration_fails_latency_gate(self):
        current = _metrics(pass_rate=0.6, duration_mean=500.0)
        baseline = _metrics(pass_rate=0.5, duration_mean=0.0)
        result = phase_6_gate_decision(current, baseline, THRESHOLDS)
        self.assertEqual(result["decision"], "discard")
        self.assertTrue(any("latency FAIL" in r for r in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
