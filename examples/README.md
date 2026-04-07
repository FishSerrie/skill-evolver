# 5-Minute Quickstart Demo

This demo shows Skill Evolver optimizing a simple code-review skill from ~70% pass rate to higher through automated iteration.

## What's Included

```
hello-skill/
  SKILL.md       # A deliberately imperfect code-review skill
  evals.json     # 6 test cases (4 dev + 2 holdout) with intentional baseline failures
```

The skill is intentionally missing "security" as a review category and doesn't mention "examples" — these are gaps the evolve loop should discover and fix.

## Prerequisites

- Python 3.10+
- Git

## Run the Demo

```bash
# 1. Initialize git (required by evolve loop)
cd examples/hello-skill
git init && git add -A && git commit -m "init"

# 2. Run evolve with local evaluator (no LLM needed for basic assertions)
python3 ../../plugin/skills/skill-evolver/scripts/evolve_loop.py \
  . --gt evals.json --run --max-iterations 5 --evaluator local

# 3. Check results
cat hello-skill-workspace/evolve/results.tsv
```

## What to Expect

- **Baseline**: ~70% (some assertions fail because SKILL.md lacks "security" and "example")
- **After evolve**: The loop should identify failing assertions, add missing concepts, and reach higher pass rates
- **Traces**: Check `hello-skill-workspace/evolve/iteration-E*/traces/` for per-case execution traces

## Try With LLM Evaluation

For semantic assertions (fact_coverage, path_hit), use the creator evaluator:

```bash
python3 ../../plugin/skills/skill-evolver/scripts/evolve_loop.py \
  . --gt evals.json --run --max-iterations 10 --evaluator creator
```

## Next Steps

- Try evolving your own skill: `/skill-evolver evolve <your-skill-path>`
- Add more test cases to `evals.json` (see GT format in main README)
- Explore the workspace artifacts to understand the 8-phase protocol
