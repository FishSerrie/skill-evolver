# Adaptive Evaluation Strategy

> This document replaces the former `eval_levels.md`. Instead of fixed L1/L2/L3 tiers, the evolver generates a per-skill adaptive `evolve_plan.md`.

---

## Evaluation Philosophy

**Core principle: LLMs perform binary classification only; programs compute scores.**

Given the same binary classification, the resulting score is deterministic. This separation eliminates scoring variance from LLM non-determinism.

### Assertion Type Mapping

| Category | Assertion Types | Evaluator | Notes |
|---|---|---|---|
| Program-only | `contains`, `not_contains`, `regex`, `file_exists`, `json_schema`, `script_check` | Deterministic program | Zero LLM involvement; exact match or script exit code |
| LLM binary | `path_hit`, `fact_coverage` | LLM yes/no, then program scores | LLM answers "present or not?"; program tallies |

### fact_coverage Modes

| Mode | Condition | Behavior |
|---|---|---|
| **Preset** | GT includes an explicit `facts` array | LLM checks each fact: present (1) or absent (0). Score = sum / total. |
| **Online** | No `facts` array in GT | Extract keywords from the reference answer; match against output. Score = matched / total keywords. |

---

## Design Rationale

Every skill has different characteristics (type, GT data volume, assertion type distribution). Evaluation strategy should not be hard-coded. Before optimization begins, the evolver analyzes the target skill and generates `evolve_plan.md`.

---

## evolve_plan.md Generation Process

### Inputs

1. **Target skill's SKILL.md**: Identify skill type (customer service / code generation / document processing / ...) and complexity
2. **GT data**: Count assertion type distribution, data volume, dev/holdout/regression split ratios
3. **Existing evaluation results** (if available): Identify current bottlenecks

### Analysis Dimensions

| Dimension | Analysis Method | Decision Impact |
|---|---|---|
| Skill type | Read SKILL.md description + body | Evaluation focus (which assertion types matter most) |
| GT data volume | Count dev/holdout/regression entries | Gate thresholds (small data -- relax min_delta) |
| Assertion distribution | Count contains/fact_coverage/script_check ratios | Optimization priorities |
| Current trigger F1 | From trigger eval history if available | Whether to skip Layer 1 |
| Current pass_rate | From behavior eval history if available | Starting layer and strategy |

---

## evolve_plan.md Template

```markdown
# Evolve Plan for: <skill-name>

## Skill Analysis
- Type: <customer service / code generation / document processing / ...>
- Complexity: <low / medium / high>
- GT data volume: dev <N> cases, holdout <N> cases, regression <N> cases
- Key assertion types: <fact_coverage, path_hit, ...>

## Evaluation Strategy

### Quick Gate (every iteration)
- YAML frontmatter syntax check
- Trigger sample: <N> cases (should_trigger + should_not_trigger)
- Hard assertion sample: <N> core dev cases

### Dev Eval (frequency: <every iteration / every N iterations>)
- Run full dev split: <N> cases
- Focus areas: <assertion types>
- Invoke Creator's grader protocol for scoring

### Strict Eval (trigger conditions)
- Auto-trigger every <N> iterations
- Or when dev pass_rate exceeds baseline + <X>%
- Run holdout <N> cases + regression <N> cases

## Optimization Priorities
1. Layer <1/2/3>: <reason>
2. Focus area: <specific direction>
3. ...

## Gate Thresholds
- min_delta: <value> (reason)
- trigger_tolerance: <value>
- max_token_increase: <value>
- regression_tolerance: <value>

## Termination Conditions
- max_iterations: <N>
- stuck_threshold: <N consecutive discards>
- exhaustion: <no improvement after all 3 layers>
```

---

## Tuning Heuristics

Concrete thresholds depend on the skill and the GT; the proposer should
tune them using these rules of thumb:

- **Small GT (≤15 dev cases)**: relax `min_delta` to 0.03–0.05 so noise
  doesn't flood the signal.
- **Binary-correctness skills** (code generation, schema validation):
  tighten `min_delta` to 0.05 and cap `max_token_increase` around 0.15.
- **Token-heavy skills** (long-form writing, document processing): relax
  `max_token_increase` to 0.25–0.30.
- **Trigger already strong** (F1 ≥ 0.90): start at Layer 2, skip Layer 1.
- **Helper-script skills**: start at Layer 2, expect Layer 3 promotion
  early — the real capability lives in scripts.

---

## Plan Refresh Triggers

- **First evolve**: Must generate
- **Layer promotion**: Refresh optimization priorities and evaluation strategy
- **5 consecutive discards (stuck)**: Re-analyze failure patterns, refresh strategy
- **Manual intervention**: User explicitly requests adjustment
