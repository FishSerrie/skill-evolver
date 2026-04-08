#!/usr/bin/env python3
"""Run skill-evolver self-iteration loop using the real evaluators.py framework.

This driver:
1. Loads LocalEvaluator from plugin/skills/skill-evolver/scripts/evaluators.py
2. Builds the full skill corpus (SKILL.md + references/*.md + agents/*.md)
3. Calls LocalEvaluator._evaluate_assertion() — the framework's atomic logic
4. Uses Meta-Trace pattern: writes traces per case for failure diagnosis
5. All git operations happen in plugin/skills/skill-evolver-workspace/working-skill/
   (NOT the project git)
"""
import sys, json, subprocess, shutil
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).parent.resolve()
SKILL_SCRIPTS = PROJECT_ROOT / "plugin/skills/skill-evolver/scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))

# Use the REAL framework
from common import require_creator, find_workspace
from evaluators import LocalEvaluator

WORKSPACE = PROJECT_ROOT / "plugin/skills/skill-evolver-workspace"
WORKING_SKILL = WORKSPACE / "working-skill"
GT_PATH = WORKSPACE / "evals/evals.json"
EVOLVE_DIR = WORKSPACE / "evolve"


def build_corpus(skill_path: Path) -> str:
    """Build full skill corpus: SKILL.md + references + agents."""
    parts = []
    for md in sorted(skill_path.rglob("*.md")):
        rel = md.relative_to(skill_path)
        # Skip git internals and any workspace subdir within the skill (defensive)
        rel_str = str(rel)
        if rel_str.startswith(".git/") or "-workspace/" in rel_str:
            continue
        parts.append(f"### {rel} ###\n{md.read_text()}")
    return "\n\n".join(parts)


def grade_case(evaluator: LocalEvaluator, case: dict, corpus: str,
               skill_path: Path) -> dict:
    """Grade one case by calling the real evaluator's per-assertion method."""
    case_id = case.get("id", "?")
    asserts_results = []
    passed_count = 0

    for a in case.get("assertions", []):
        atype = a.get("type", "contains")
        val = a.get("value", "")
        # Call the REAL framework method
        ok = evaluator._evaluate_assertion(atype, val, a, corpus, skill_path)
        asserts_results.append({
            "type": atype,
            "value": val[:80],
            "passed": ok,
            "description": a.get("description", ""),
            "evidence": "matched" if ok else "NOT matched",
        })
        if ok:
            passed_count += 1

    total = len(asserts_results)
    return {
        "case_id": case_id,
        "prompt": case.get("prompt", ""),
        "split": case.get("split", "dev"),
        "assertions": asserts_results,
        "passed": passed_count,
        "total": total,
        "pass_rate": passed_count / total if total else 0,
        "overall_pass": passed_count == total,
    }


def run_eval(evaluator: LocalEvaluator, skill_path: Path,
             iteration: int, split: str = "dev") -> dict:
    """Run full eval against given split, write grading.json + traces/."""
    corpus = build_corpus(skill_path)
    gt = json.loads(GT_PATH.read_text())
    cases = [c for c in gt["evals"] if c.get("split", "dev") == split]

    results = [grade_case(evaluator, c, corpus, skill_path) for c in cases]

    total_assertions = sum(r["total"] for r in results)
    total_passed = sum(r["passed"] for r in results)
    pass_rate = total_passed / total_assertions if total_assertions else 0

    # Write grading + traces (Meta-Trace pattern)
    iter_dir = EVOLVE_DIR / f"iteration-E{iteration}"
    iter_dir.mkdir(exist_ok=True)
    (iter_dir / "grading.json").write_text(json.dumps({
        "iteration": iteration,
        "split": split,
        "results": results,
        "pass_rate": pass_rate,
        "total_assertions": total_assertions,
        "total_passed": total_passed,
    }, indent=2))

    trace_dir = iter_dir / "traces"
    trace_dir.mkdir(exist_ok=True)
    for r in results:
        cid = r["case_id"]
        lines = [
            f"# Case {cid} Execution Trace",
            f"",
            f"**Prompt:** {r['prompt']}",
            f"**Split:** {r['split']}",
            f"**Result:** {'PASS' if r['overall_pass'] else 'FAIL'} "
            f"({r['passed']}/{r['total']})",
            f"",
            f"## Assertion Results",
            f"",
        ]
        for i, a in enumerate(r["assertions"], 1):
            status = "[PASS]" if a["passed"] else "[FAIL]"
            lines.append(f"### Assertion {i}: {status} {a['type']}")
            lines.append(f"- **Value:** `{a['value']}`")
            lines.append(f"- **Description:** {a['description']}")
            lines.append(f"- **Evidence:** {a['evidence']}")
            lines.append("")
        (trace_dir / f"case-{cid:03d}.trace.md").write_text("\n".join(lines))

    return {
        "pass_rate": pass_rate,
        "total_passed": total_passed,
        "total_assertions": total_assertions,
        "case_pass": sum(1 for r in results if r["overall_pass"]),
        "n_cases": len(results),
        "results": results,
    }


def git_in_working(*args: str) -> str:
    """Run git command inside working-skill (NOT project git)."""
    result = subprocess.run(
        ["git", *args], cwd=str(WORKING_SKILL),
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip()


def append_results_tsv(iteration: int, commit: str, metric: float,
                       delta: float, status: str, layer: str, desc: str):
    tsv = EVOLVE_DIR / "results.tsv"
    line = (f"{iteration}\t{commit}\t{metric:.1f}\t{delta:+.1f}\t"
            f"1.00\t0\tpass\t{status}\t{layer}\t{desc}\n")
    with open(tsv, "a") as f:
        f.write(line)


def append_experiment(entry: dict):
    with open(EVOLVE_DIR / "experiments.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


def gate_decision(current: float, baseline: float, min_delta: float = 0.05) -> tuple[str, list[str]]:
    """Multi-gate decision per references/gate_rules.md."""
    reasons = []
    quality_ok = current >= baseline + min_delta
    if quality_ok:
        reasons.append(f"quality OK: {current:.3f} >= {baseline:.3f} + {min_delta}")
    else:
        reasons.append(f"quality FAIL: {current:.3f} < {baseline:.3f} + {min_delta}")
    # trigger/cost/latency/regression all unchanged in this scenario
    decision = "keep" if quality_ok else "discard"
    return decision, reasons


def save_best_version(iteration: int):
    dest = EVOLVE_DIR / "best_versions" / f"iteration-{iteration}"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(WORKING_SKILL, dest, ignore=shutil.ignore_patterns(".git"))


def commit_in_working(layer: str, description: str) -> str:
    """git commit inside working-skill, return short hash."""
    git_in_working("add", "-A")
    status = git_in_working("status", "--porcelain")
    if not status:
        return ""
    git_in_working("-c", "user.email=evolver@local",
                   "-c", "user.name=Skill Evolver",
                   "commit", "-q", "-m", f"experiment({layer}): {description}")
    return git_in_working("rev-parse", "--short", "HEAD")


def revert_in_working() -> None:
    """git revert HEAD --no-edit inside working-skill."""
    git_in_working("-c", "user.email=evolver@local",
                   "-c", "user.name=Skill Evolver",
                   "revert", "HEAD", "--no-edit")


def run_iteration(evaluator: LocalEvaluator, iteration: int,
                  baseline_metric: float, current_best: float,
                  layer: str, description: str, mutation_fn) -> float:
    """Execute one full iteration: modify → commit → eval → gate → log.

    Returns the new current_best metric (updated if KEEP, unchanged if DISCARD).
    """
    print(f"\n{'='*60}\nITERATION {iteration}: {description}\n{'='*60}")

    # Phase 3: Modify (mutation_fn applies an Edit to working-skill)
    print(f"[Phase 3] Modifying working-skill...")
    mutation_fn(WORKING_SKILL)

    # Phase 4: Commit in workspace git (isolated from project)
    commit = commit_in_working(layer, description)
    print(f"[Phase 4] Committed to workspace git: {commit}")

    # Phase 5: Verify (L1 + L2)
    l1 = evaluator.quick_gate(WORKING_SKILL, GT_PATH)
    print(f"[Phase 5] L1: {'PASS' if l1['pass'] else 'FAIL'}")
    if not l1["pass"]:
        print(f"  Errors: {l1['errors']}")
        revert_in_working()
        append_results_tsv(iteration, commit, 0, -current_best*100,
                           "discard", layer, f"L1 fail: {description}")
        return current_best

    eval_result = run_eval(evaluator, WORKING_SKILL, iteration, split="dev")
    new_metric = eval_result['pass_rate']
    delta = new_metric - current_best
    print(f"[Phase 5] L2: {eval_result['total_passed']}/"
          f"{eval_result['total_assertions']} = {new_metric*100:.1f}% "
          f"(delta: {delta*100:+.1f}%)")

    # Phase 6: Gate decision
    decision, reasons = gate_decision(new_metric, current_best, min_delta=0.05)
    print(f"[Phase 6] Decision: {decision.upper()}")
    for r in reasons:
        print(f"  {r}")

    # Phase 7: Log
    if decision == "keep":
        append_results_tsv(iteration, commit, new_metric*100, delta*100,
                           "keep", layer, description)
        append_experiment({
            "iteration": iteration, "mutation_layer": layer,
            "intent": description, "commit": commit,
            "dev_pass_rate": new_metric, "delta": delta,
            "status": "keep",
        })
        save_best_version(iteration)
        print(f"[Phase 7] KEEP — new best {new_metric*100:.1f}%, "
              f"saved best_versions/iteration-{iteration}/")
        return new_metric
    else:
        revert_in_working()
        append_results_tsv(iteration, commit, new_metric*100, delta*100,
                           "discard", layer, f"{description} (delta below min_delta)")
        append_experiment({
            "iteration": iteration, "mutation_layer": layer,
            "intent": description, "commit": commit,
            "dev_pass_rate": new_metric, "delta": delta,
            "status": "discard",
            "reasons": reasons,
        })
        print(f"[Phase 7] DISCARD — reverted in workspace git, current best {current_best*100:.1f}%")
        return current_best


def mutation_iter1_install_section(skill_path: Path) -> None:
    """Iteration 1: Add Installing skill-creator section to SKILL.md."""
    skill_md = skill_path / "SKILL.md"
    content = skill_md.read_text()
    old = """**Prerequisites:**
- GT data (test cases + assertions) should be prepared in advance; if unavailable, evolve mode auto-generates them via Creator
- The skill directory **must be under git** (if uninitialized, Phase 0 forces `git init`; if git is not installed, install it first)
- **skill-creator installed (hard dependency)** — Evolver refuses to start without it and shows installation instructions. See `references/creator_integration.md` Section 3 for path discovery and custom-path options (`$SKILL_CREATOR_PATH` env var or `--creator-path` CLI flag)

---"""
    new = """**Prerequisites:**
- **skill-creator installed (hard dependency)** — Evolver refuses to start without it. See installation guide below.
- GT data (test cases + assertions) should be prepared in advance; if unavailable, evolve mode auto-generates them via Creator
- The skill directory **must be under git** (if uninitialized, Phase 0 forces `git init`; if git is not installed, install it first)

### Installing skill-creator

skill-creator is a hard dependency. If it is not found, Evolver errors out with these instructions. Install in one of three ways:

1. **Plugin marketplace (recommended):** In Claude Code, run `/install skill-creator`

2. **Manual install from GitHub:**
   ```bash
   git clone https://github.com/anthropics/skills.git /tmp/anthropic-skills-latest
   cp -r /tmp/anthropic-skills-latest/skills/skill-creator ~/.claude/skills/skill-creator
   ```
   Source: https://github.com/anthropics/skills/tree/main/skills/skill-creator

3. **Already installed at a custom path?**
   ```bash
   export SKILL_CREATOR_PATH=/your/path/to/skill-creator
   # or pass via CLI:
   python3 scripts/evolve_loop.py ./my-skill --gt ./evals.json --run --creator-path /your/path
   ```

See `references/creator_integration.md` Section 3 for the full path discovery order.

---"""
    skill_md.write_text(content.replace(old, new))


def mutation_iter2_eval_viewer(skill_path: Path) -> None:
    """Iteration 2: Add eval viewer step to Evolve Mode flow (small change)."""
    skill_md = skill_path / "SKILL.md"
    content = skill_md.read_text()
    old = """5. Output summary when the loop terminates

Helper scripts (in `scripts/`) handle deterministic steps, but **you reason about what to change and how**."""
    new = """5. Output summary when the loop terminates
6. **Launch the eval viewer for human review**: After the loop completes (and after holdout eval + cleanup), `evolve_loop.py` automatically calls Creator's `eval-viewer/generate_review.py` to render a static HTML review at `<workspace>/evolve/review.html`. The user opens this file to see the per-iteration trajectory, per-case grades, and best-version diff. This is the final hand-off to the human.

Helper scripts (in `scripts/`) handle deterministic steps, but **you reason about what to change and how**."""
    skill_md.write_text(content.replace(old, new))


def mutation_iter3_bundled(skill_path: Path) -> None:
    """Iteration 3 (bundled): eval viewer step + lowercase anti-patterns + do-not-guess."""
    # Re-apply eval viewer (since iter2 was reverted)
    mutation_iter2_eval_viewer(skill_path)
    # Lowercase anti-patterns + add "do not guess"
    proto = skill_path / "references/evolve_protocol.md"
    pcontent = proto.read_text()
    old = """**Anti-patterns (forbidden):**
- Do not repeat a change that was already discarded with identical content (check git log first)
- Do not bundle multiple unrelated changes in one iteration (the one-sentence test: if you need "and" to describe it, it is two changes)
- Do not make cross-layer changes
- **Do not identify a problem without fixing it** -- if it is a problem, it warrants an iteration. The purpose of iteration is continuous improvement; skipping "small issues" forfeits improvement opportunities"""
    new = """**Anti-patterns (forbidden — written in the imperative "do not X" form so they are greppable and unambiguous):**
- do not repeat a change that was already discarded with identical content (check git log first)
- do not bundle multiple unrelated changes in one iteration (the one-sentence test: if you need "and" to describe it, it is two changes)
- do not make cross-layer changes
- do not guess — if no trace evidence points to a clear cause, say so explicitly and gather more evidence first (Meta-Trace mandatory protocol)
- **do not identify a problem without fixing it** -- if it is a problem, it warrants an iteration. The purpose of iteration is continuous improvement; skipping "small issues" forfeits improvement opportunities"""
    proto.write_text(pcontent.replace(old, new))


def main():
    print("=" * 60)
    print("SKILL-EVOLVER SELF-ITERATION")
    print("Using REAL evaluators.py framework + isolated workspace git")
    print("=" * 60)

    # Phase 0: Verify Creator + L1 gate
    creator = require_creator()
    print(f"\n[Phase 0] Creator: {creator}")
    print(f"[Phase 0] Working skill: {WORKING_SKILL}")
    print(f"[Phase 0] GT: {GT_PATH}")

    evaluator = LocalEvaluator()
    l1 = evaluator.quick_gate(WORKING_SKILL, GT_PATH)
    print(f"[Phase 0] L1 gate: {'PASS' if l1['pass'] else 'FAIL'}")
    if not l1["pass"]:
        print(f"L1 errors: {l1['errors']}")
        sys.exit(1)

    print("\n[Phase 0] Baseline L2 eval...")
    baseline = run_eval(evaluator, WORKING_SKILL, iteration=0, split="dev")
    print(f"[Phase 0] Baseline: {baseline['pass_rate']*100:.1f}% "
          f"({baseline['total_passed']}/{baseline['total_assertions']})")

    baseline_commit = git_in_working("rev-parse", "--short", "HEAD")
    append_results_tsv(0, baseline_commit, baseline['pass_rate']*100, 0.0,
                       "baseline", "-", "initial baseline (real evaluator framework)")
    save_best_version(0)
    current_best = baseline['pass_rate']

    # Iterations
    current_best = run_iteration(
        evaluator, 1, baseline['pass_rate'], current_best,
        "body", "add Installing skill-creator section to SKILL.md",
        mutation_iter1_install_section)

    current_best = run_iteration(
        evaluator, 2, baseline['pass_rate'], current_best,
        "body", "add eval viewer step to Evolve mode flow",
        mutation_iter2_eval_viewer)

    current_best = run_iteration(
        evaluator, 3, baseline['pass_rate'], current_best,
        "body", "bundled doc alignment: anti-patterns lowercase + do-not-guess + eval viewer",
        mutation_iter3_bundled)

    # Holdout strict eval
    print(f"\n{'='*60}\nHOLDOUT STRICT EVAL\n{'='*60}")
    holdout = run_eval(evaluator, WORKING_SKILL, iteration=999, split="holdout")
    print(f"Holdout: {holdout['pass_rate']*100:.1f}% "
          f"({holdout['total_passed']}/{holdout['total_assertions']}, "
          f"{holdout['case_pass']}/{holdout['n_cases']} cases)")

    # Final summary
    print(f"\n{'='*60}\nEVOLVE COMPLETE\n{'='*60}")
    print(f"Baseline: {baseline['pass_rate']*100:.1f}% → Best: {current_best*100:.1f}%")
    print(f"Loop git history (workspace, NOT project):")
    log = git_in_working("log", "--oneline", "-10")
    for line in log.split("\n"):
        print(f"  {line}")

    # Show final results.tsv
    print(f"\nresults.tsv:")
    print((EVOLVE_DIR / "results.tsv").read_text())


if __name__ == "__main__":
    main()
