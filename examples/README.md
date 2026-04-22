# 5-Minute Quickstart Demo

This demo shows Skill Evolver optimizing a deliberately incomplete code-review
skill from a red baseline to 100% pass rate through automated iteration — and
proves the regression split catches destructive rewrites along the way.

## What's Included

```
hello-skill/
  SKILL.md       # A deliberately imperfect code-review skill
  evals.json     # 8 test cases across 3 splits:
                 #   4 dev       (2 fail at baseline on purpose)
                 #   2 holdout   (overfitting guard)
                 #   2 regression (structural guards: Output Format + anti-hallucination Rules)
```

The skill intentionally omits `security` as a review category and doesn't
mention providing `example`s — these are the gaps the evolve loop should
discover and fix. The regression cases guard against the loop accidentally
deleting structural pieces (`Output Format` section, `do not invent issues`
rule) while making those fixes.

## Prerequisites

- Python 3.10+
- Git

## Run the Demo

All commands assume you are inside `examples/hello-skill/`. One variable at
the top keeps the rest of the commands readable:

```bash
cd examples/hello-skill
export EVOLVER_ROOT="$(cd ../.. && pwd)"   # the skill-evolver-plugin/ repo root

# 1. Initialize git — the evolve loop requires a clean repo to commit experiments into
git init && git add -A && git commit -m "init"

# 2. Run evolve with the local evaluator (no LLM needed — every assertion is program-only)
python3 "$EVOLVER_ROOT/plugin/skills/skill-evolver/scripts/evolve_loop.py" \
  . --gt evals.json --run --max-iterations 5 --evaluator local

# 3. Check the trajectory
cat hello-skill-workspace/evolve/results.tsv
```

## What to Expect

Actual baseline measured with `LocalEvaluator`:

| Split       | Pass Rate | Notes |
|---|---|---|
| dev         | **8/10 = 80%** | case 3 (`security`) and case 4 (`example`) fail at baseline |
| holdout     | 5/5 = 100%     | already clean — used to catch overfitting |
| regression  | 4/4 = 100%     | structural guards — must stay green every iteration |

A likely trajectory the loop will follow:

| Iteration | dev      | holdout | regression | status | atomic change |
|---|---|---|---|---|---|
| 0 baseline | 8/10 = 80%  | 5/5 | 4/4 | — | — |
| 1          | 9/10 = 90%  | 5/5 | 4/4 | keep | add `security` to the review categories |
| 2          | 10/10 = 100%| 5/5 | 4/4 | keep | mention providing `example` fixes in the output guidance |

The exact intents are LLM-proposed, so your run may phrase them differently —
but the gate rejects anything that regresses holdout or regression, so the
shape of the trajectory will match. Inspect per-case traces to see exactly
what each eval round saw:

```bash
ls hello-skill-workspace/evolve/iteration-E*/traces/
```

## Try With LLM Evaluation

For semantic assertions (`fact_coverage`, `path_hit`), switch to the creator
evaluator, which calls `skill-creator`'s LLM-backed grading:

```bash
python3 "$EVOLVER_ROOT/plugin/skills/skill-evolver/scripts/evolve_loop.py" \
  . --gt evals.json --run --max-iterations 10 --evaluator creator
```

## Advanced Assertion Types

`hello-skill` uses only `contains` / `not_contains` / `regex` because those
are the quickest to demonstrate and need zero external dependencies. Real
skills usually need richer assertions:

| Type | When to use | Cost |
|---|---|---|
| `contains` / `not_contains` | Canonical names, file formats, API names that must not drift | free, deterministic |
| `regex` | Patterns with acceptable variations (e.g. `iteration-E\d+/traces/case_\d+`) | free, deterministic |
| `json_schema` | Structural validation of JSON output | free, deterministic |
| `script_check` | Behavioral/AST-level checks (run a script, use its exit code) | free, deterministic |
| `fact_coverage` | Semantic coverage — "does the answer cover these facts" | one LLM binary call per fact |
| `path_hit` | Semantic match — "does the output reference this path" | one LLM binary call |

For a production-grade GT that mixes several of these, see
`plugin/skills/skill-evolver-workspace/evals/evals.json` in the top-level
workspace — that is the GT `skill-evolver` uses to self-evolve, and it is the
best reference for real-world GT design.

## Your Turn: Add a GT Case

Once the demo loop finishes at 100%, extending it is the fastest way to feel
the loop working end-to-end:

1. Open `evals.json` and add a new dev case probing a gap the current
   SKILL.md doesn't cover:
   ```json
   {
     "id": 9,
     "prompt": "review this class, check for type hints",
     "assertions": [
       {"type": "contains", "value": "type hint", "description": "SKILL.md mentions type hints"}
     ],
     "split": "dev"
   }
   ```
2. Re-run the evolve loop:
   ```bash
   python3 "$EVOLVER_ROOT/plugin/skills/skill-evolver/scripts/evolve_loop.py" \
     . --gt evals.json --run --max-iterations 3 --evaluator local
   ```
3. Watch the new case fail at baseline, get picked up by Phase 2 ideation,
   and flip to pass in the next iteration — without the regression guards
   going red.

This is the core workflow: **add a failing case → let the loop find and apply
a fix → verify it passes without breaking existing cases**. The regression
split is your safety net for the last step.

## Next Steps

- Evolve your own skill: `/skill-evolver evolve <your-skill-path>`
- Read `plugin/skills/skill-evolver/SKILL.md` for the full 8-phase protocol
- Explore the workspace artifacts (`results.tsv`, `experiments.jsonl`,
  `best_versions/`, `iteration-E*/traces/`) to understand how iteration
  memory is persisted and how Phase 1 Review replays prior runs
