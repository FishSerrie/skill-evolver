# Evolve Plan for: skill-evolver

> Generated: 2026-04-08 03:08 UTC
> Skill: skill-evolver
> Description: Skill 自动进化引擎 — 基于 skill-creator 评测能力 + autoresearch 自主迭代思想，自动创建、评测、迭代优化 skill。内核：Creator 做评测打分，AutoR...

## Evaluation Philosophy

LLM does binary classification only; programs do all scoring.
Same classification always produces the same score.

Assertion types:
- Program-only: contains, not_contains, regex, file_exists, json_schema, script_check
- LLM binary (YES/NO): path_hit, fact_coverage

## Skill Analysis
- Type: TODO — analyze SKILL.md to determine
- Complexity: TODO
- GT data: No GT data found yet.
- Key assertion types: TODO

## Evaluation Strategy

### Quick Gate (every iteration)
- YAML frontmatter syntax check
- Trigger sampling: 3 cases
- Hard assertion sampling: 2 core dev cases

### Dev Eval (every iteration)
- Run all dev split cases
- Focus areas: TODO
- Use binary LLM judge for semantic assertions

### Strict Eval (triggered conditionally)
- Auto-trigger every 5 iterations
- Or when dev pass_rate exceeds baseline + 10%
- Run holdout + regression sets
- Anti-Goodhart: holdout cases never exposed to proposer

evaluator: local
model:

## Optimization Priority
1. Layer 2 (Body): TODO
2. TODO

## Gate Thresholds
- min_delta: 0.02
- trigger_tolerance: 0.05
- max_token_increase: 0.20
- regression_tolerance: 0.05

## Termination Conditions
- max_iterations: 20
- stuck_threshold: 5 consecutive discards
- exhaustion: all 3 layers attempted with no improvement

---
*This is a template. Claude should analyze the skill and GT data to fill in TODOs before starting evolve.*
