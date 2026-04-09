# Skill Creator ↔ Skill Evolver Integration Protocol

## 1. Role Comparison

| Dimension | Skill Creator (Official) | Skill Evolver (This Skill) | Relationship |
|---|---|---|---|
| **Create** | Interview → write SKILL.md → generate evals | Create mode does the same | **Calls Creator** |
| **Eval** | Spawn subagent, run eval, human review viewer | Three-tier eval (quick gate / dev / strict), finer granularity | **Enhances** |
| **Improve** | Human reads feedback → manual edits → re-run eval | Improve mode = human-directed improvement | **Calls Creator** |
| **Benchmark** | Blind A/B comparison + analyzer | Benchmark mode + comparator agent | **Calls Creator** |
| **Evolve** | None. Human loops manually | **Core value**. AutoResearch-style automated outer loop | **New** |
| **Gate** | None. Human decides if satisfied | Multi-gate AND logic (quality / trigger / cost / latency / regression) | **New** |
| **Memory** | Only workspace directory | results.tsv + experiments.jsonl + git + best_versions + traces | **New** |
| **Description optimization** | `run_loop.py` for trigger optimization | Layer 1 description optimization calls Creator's run_loop directly | **Calls Creator** |

### One-Line Summary

**Evolver = Creator's superset. Creator handles "single evaluation cycle (human-in-the-loop)"; Evolver adds "automatic outer loop + gates + memory (human-out-of-the-loop)".**

```
Creator loop:  Human → Edit → Run eval → View results → Human judges → Edit → ... (human in the loop)
Evolver loop:  Search → Modify → Eval → Gate → Log → Loop → ... (human out of the loop)
```

---

## 2. Calling Pattern (Reference, Not Copy)

### Core Principle

**Evolver does not copy Creator's code or protocols. Evolver calls Creator's capabilities by reference. When Creator updates, Evolver benefits automatically.**

### Specific Call Patterns

#### 2.1 Create Mode

Evolver's Create mode does not implement creation logic itself:

```
1. Read skill-creator's SKILL.md (via Skill tool or direct file read)
2. Follow Creator's "Capture Intent → Interview → Write SKILL.md" flow
3. Additional steps: create evolve workspace + generate GT data template
```

#### 2.2 Eval Mode

Evolver's evaluation engine has two parts:
- **Trigger evaluation**: Calls Creator's `scripts/run_eval.py` directly
- **Behavior evaluation**: Binary LLM classification + program scoring (see eval_strategy.md)

```bash
# Trigger evaluation — call Creator directly
python -m scripts.run_eval \
  --eval-set <workspace>/evals/trigger/trigger_eval.json \
  --skill-path <target-skill> \
  --model <model>

# Behavior evaluation — binary LLM judge
# Per-assertion YES/NO calls for semantic assertions (path_hit, fact_coverage)
# Program aggregates all binary results into pass_rate
```

#### 2.3 Description Optimization (Layer 1)

Calls Creator's `scripts/run_loop.py` directly:

```bash
python -m scripts.run_loop \
  --eval-set <workspace>/evals/trigger/trigger_eval.json \
  --skill-path <target-skill> \
  --model <model> \
  --max-iterations 5 \
  --verbose
```

#### 2.4 Benchmark / Comparison

Calls Creator's scripts and agents:
- `scripts/aggregate_benchmark.py` — statistics aggregation
- `agents/comparator.md` — blind A/B comparison
- `agents/analyzer.md` — attribution analysis

#### 2.5 Grading

Evolver's `agents/grader_agent.md` and `agents/comparator_agent.md` are pointer files that contain no grading logic. At runtime they redirect to Creator's full versions:

```python
from common import get_creator_agent_path

# For grading, read Creator's grader
grader_content = get_creator_agent_path("grader.md").read_text()

# For comparison, read Creator's comparator
comparator_content = get_creator_agent_path("comparator.md").read_text()
```

There is no fallback path. Creator is a hard dependency.

---

## 3. Creator Path Discovery

Evolver searches for Creator's installation in this order:

```python
CREATOR_SEARCH_PATHS = [
    # 1. Marketplace plugins (official)
    "~/.claude/plugins/marketplaces/*/plugins/skill-creator/",
    # 2. Direct plugin install
    "~/.claude/plugins/skill-creator/skills/skill-creator/",
    # 3. Standalone plugin with plugin/ subdir
    "~/.claude/plugins/skill-creator/plugin/skills/skill-creator/",
    # 4. User skills directory
    "~/.claude/skills/skill-creator/",
    # 5. Project-level skills
    ".claude/skills/skill-creator/",
]
```

**If Creator is not found → Evolver errors out with installation instructions. There is no silent degradation.**

```python
from common import require_creator
creator = require_creator()  # Raises CreatorNotFoundError with install guidance if not found
```

Users can specify a custom path via:
- Environment variable: `export SKILL_CREATOR_PATH=/custom/path`
- CLI argument: `--creator-path /custom/path` (evolve_loop.py only)

---

## 4. Evolver's Own Capabilities (Not in Creator)

| Capability | File | Description |
|---|---|---|
| **Evolve outer loop** | `references/evolve_protocol.md` | 8-phase automatic iteration |
| **Search Agent** | `agents/search_agent.md` | Failure pattern analysis, mutation hypothesis generation |
| **Analyzer Agent** | `agents/analyzer_agent.md` | Attribution analysis (why did this change work or not) |
| **Multi-gate system** | `references/gate_rules.md` | AND-logic gate decisions |
| **Layered mutation** | `references/mutation_policy.md` | Description → Body → Scripts progressive optimization |
| **Structured memory** | `references/memory_schema.md` | results.tsv + experiments.jsonl + execution traces |
| **Adaptive eval plan** | `references/eval_strategy.md` | Per-skill evaluation strategy generation |
| **Workspace management** | `scripts/setup_workspace.py` | Per-skill isolated workspace |

---

## 5. Update Compatibility

### When Creator Updates, What Does Evolver Need to Do?

**In most cases: nothing.**

| Creator Change Type | Evolver Impact | Action Required |
|---|---|---|
| scripts/ internal changes | None (calling interface unchanged) | None |
| agents/ protocol updates | Auto-effective (reference, not copy) | None |
| SKILL.md flow changes | Create mode follows automatically | None |
| CLI argument changes (breaking) | Script calls may break | Update Evolver's script invocations |
| JSON schema changes (breaking) | Parsing may break | Update Evolver's schema references |

**Summary: Only breaking changes require Evolver updates.**
