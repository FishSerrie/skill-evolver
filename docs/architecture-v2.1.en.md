# Skill Evolver Technical Architecture v2.1

> Based on the v1.1 architecture. Reflects all refactoring and implementation as of 2026-04-07.
> See the changelog at the end for v1.1 to v2.1 deltas.

---

## 1. Project Goal

Build a unified-entry Skill Evolver that:

- Creates an initial skill from a task description + ground-truth data
- Evaluates skill quality through adaptive multi-tier scoring
- Iterates the skill automatically via an AutoResearch-style outer loop
- Produces the current best skill version under multi-gate constraints

**External surface**: one skill, one entry point.
**Internal structure**: 5 modes + pipeline orchestration + 4-layer architecture.

---

## 2. Product Shape

### 2.1 Five Modes

| Mode | Responsibility | Use Case |
|---|---|---|
| **Create** | Generate initial skill from requirements + GT | New skill |
| **Eval** | Single independent evaluation, produce benchmark | Understand current quality |
| **Improve** | Human-directed targeted improvement | Semi-automated optimization |
| **Benchmark** | Systematic comparison (A/B, blind eval) | Version comparison decisions |
| **Evolve** | Autonomous optimization loop (core value) | Unattended continuous improvement |

### 2.2 Quick Start

```bash
/skill-evolver eval ./my-skill/ --gt ./evals.json
/skill-evolver evolve ./my-skill/ --gt ./evals.json --iterations 20
/skill-evolver create
/skill-evolver benchmark ./skill-v1/ ./skill-v2/ --gt ./evals.json
```

### 2.3 Pipeline

```
/skill-evolver pipeline ./my-skill/ --mode create+eval+evolve
```

---

## 3. Relationship with Skill Creator (new in v2.1)

### Core Principle: Reference, Don't Copy

Evolver invokes Creator's capabilities by reference. When Creator updates, Evolver inherits the changes automatically.

| Dimension | Skill Creator | Skill Evolver | Relationship |
|---|---|---|---|
| Create | Yes: interview, write SKILL.md, generate evals | Yes (same) | Calls Creator |
| Eval | Yes: spawn subagent + viewer | Yes: 3-tier adaptive eval | Augments |
| Improve | Yes: human reads feedback, edits manually | Yes (same) | Calls Creator |
| Benchmark | Yes: blind A/B | Yes (same) | Calls Creator |
| Evolve | No | Yes (core) | **New** |
| Gate | No | Yes: multi-gate AND logic | **New** |
| Memory | No | Yes: results.tsv + experiments.jsonl | **New** |

### Creator Path Discovery

```python
SEARCH_ORDER = [
    "~/.claude/plugins/marketplaces/*/plugins/skill-creator/",
    "~/.claude/plugins/skill-creator/plugin/skills/skill-creator/",
    "~/.claude/skills/skill-creator/",
    ".claude/skills/skill-creator/",
]
```

When Creator is unavailable: the Evolve core loop is unaffected. Evaluation falls back to a built-in simplified grader.

---

## 4. Core Architecture (4 Layers)

```
+------------------------------------------------------+
|  Layer 4: Search (AutoResearch Outer Loop)            |
|  Role: Decide what to change, how to change it,      |
|        and whether to keep the result                 |
|  - Read memory (results.tsv + experiments.jsonl)      |
|  - Diagnose failure modes from execution traces       |
|  - Generate one atomic mutation                       |
|  - Apply mutation -> git commit                       |
|  - Invoke Layer 3 eval -> return to gate decision     |
+------------------------------------------------------+
|  Layer 3: Gate (Multi-Gate Decision Layer)            |
|  Role: Hard keep / discard / revert judgment          |
|  - Quality improvement (pass_rate)                    |
|  - Trigger regression (F1)                            |
|  - Token/latency within threshold                     |
|  - Regression: no existing capability broken          |
|  - Thresholds configured per-skill in evolve_plan.md  |
+------------------------------------------------------+
|  Layer 2: Eval (Adaptive Evaluation Engine)           |
|  Role: Measure skill thoroughly, produce comparable   |
|        metrics. Scoring uses LLM binary               |
|        classification + program checks.               |
|  - Quick Gate (YAML + trigger sampling, seconds)      |
|  - Dev Eval (behavioral GT, minutes)                  |
|  - Strict Eval (holdout + regression, ~10 min)        |
|  - Strategy defined in evolve_plan.md, not hardcoded  |
+------------------------------------------------------+
|  Layer 1: Memory (Structured Experiment Memory)       |
|  Role: Prevent repeated failures, exploit winning     |
|        patterns                                       |
|  - results.tsv (one row per experiment round)         |
|  - experiments.jsonl (per-case granular records)       |
|  - git history (version snapshots + commit messages)   |
|  - best_versions/ (snapshots of historical bests)     |
+------------------------------------------------------+
```

**Scoring philosophy (v2.1)**: Assertions are split into program-only checks (regex, script_check, json_schema, file_exists -- deterministic, no LLM) and LLM binary classification (contains, not_contains, path_hit, fact_coverage -- YES/NO prompt templates). This minimizes scoring variance while keeping semantic evaluation where it matters.

**v2.1 changes**:
- Layer 2 moved from fixed L1/L2/L3 tiers to adaptive tiers (Quick Gate / Dev Eval / Strict Eval), configured via evolve_plan.md
- Layer 3 thresholds moved from global defaults to per-skill configuration in evolve_plan.md

---

## 5. Workspace Layout (redesigned in v2.1)

### v1.1 Design (deprecated)

```
skill-evolver/
├── evals/       <- stored inside evolver directory
├── memory/      <- stored inside evolver directory
└── outputs/     <- stored inside evolver directory
```

### v2.1 Design (current)

**The evolver directory stores no skill-specific data.** All data lives in the target skill's workspace.

```
some-project/
├── my-skill/                       <- target skill (git-managed)
└── my-skill-workspace/             <- shared with Creator
    ├── evals/evals.json            <- GT data in Creator format
    ├── iteration-1/                <- Creator evaluation iterations
    └── evolve/                     <- Evolver-specific subdirectory
        ├── evolve_plan.md          <- adaptive optimization plan
        ├── results.tsv             <- experiment log
        ├── experiments.jsonl       <- granular memory
        ├── best_versions/          <- best skill snapshots
        ├── iteration-E1/           <- Evolve eval artifacts (E-prefix)
        │   └── case-<id>/
        │       └── trace.md        <- execution trace (Meta-Harness output)
        └── summary.md              <- final report
```

**Trace storage**: Each evaluation round stores per-case execution traces under `iteration-EN/case-<id>/trace.md`. These traces are consumed by the Search Agent's active diagnosis protocol -- the agent must read specific trace files and cite evidence before proposing mutations.

**Design decisions**:
- Reuses Creator's `<skill-name>-workspace/` -- no separate directory
- Evolver data is isolated under `evolve/`, so it does not conflict with Creator's `iteration-N/`
- When packaging a skill, the workspace is naturally excluded

---

## 6. Evolve Mode Core Protocol (8 Phases)

Full protocol: `plugin/skills/skill-evolver/references/evolve_protocol.md`.

### Flow Overview

```
Phase 0: Setup    -> Create workspace + generate evolve_plan + establish baseline
Phase 1: Review   -> Read memory                        [auto: phase_1_review()]
Phase 2: Ideate   -> Diagnose failures, decide mutation  [Claude reasoning]
Phase 3: Modify   -> Apply one atomic change             [Claude execution]
Phase 4: Commit   -> git commit                          [auto: phase_4_commit()]
Phase 5: Verify   -> Quick Gate + Dev Eval               [auto L1 + Claude-orchestrated L2]
Phase 6: Gate     -> Multi-gate keep/discard/revert      [auto: phase_6_gate_decision()]
Phase 7: Log      -> Write results.tsv + experiments.jsonl [auto: phase_7_log()]
Phase 8: Loop     -> Continue / escalate layer / stop    [auto: phase_8_loop_control()]
```

**Active diagnosis protocol**: Phase 2 now requires the Search Agent to read execution traces from the previous round's `iteration-EN/case-<id>/trace.md` files. The agent must cite specific trace evidence (file path + section) before proposing any mutation. This prevents blind guessing and ensures mutations are grounded in observed failure modes.

### Automation Level

| Phase | Automated | Implementation |
|---|---|---|
| 0 Setup | Fully auto | `setup_workspace.py` |
| 1 Review | Fully auto | `evolve_loop.phase_1_review()` |
| 2 Ideate | Claude reasoning | `evolve_loop.phase_2_prepare_ideation()` prepares context |
| 3 Modify | Claude execution | -- |
| 4 Commit | Fully auto | `evolve_loop.phase_4_commit()` |
| 5 L1 Gate | Fully auto | `run_l1_gate.py` |
| 5 L2 Eval | Claude-orchestrated | `run_l2_eval.py` provides helper functions |
| 6 Gate | Fully auto | `evolve_loop.phase_6_gate_decision()` |
| 7 Log | Fully auto | `evolve_loop.phase_7_log()` |
| 8 Loop | Fully auto | `evolve_loop.phase_8_loop_control()` |

---

## 7. Adaptive Evaluation (replaces fixed L1/L2/L3 in v2.1)

v1.1 hardcoded L1 quick gate / L2 dev set / L3 strict eval. v2.1 replaces this with:

1. **evolve_plan.md is generated by Claude after analyzing the skill's characteristics**
2. Three evaluation tiers (Quick Gate / Dev Eval / Strict Eval) are fully parameterized
3. Different skill types get different default strategies (see `plugin/skills/skill-evolver/references/eval_strategy.md`)

---

## 8. Layered Mutation (unchanged)

Same as v1.1. See `plugin/skills/skill-evolver/references/mutation_policy.md`.

```
Layer 1: Description -> trigger F1 optimization, low cost
Layer 2: SKILL.md Body -> behavioral quality optimization, medium cost
Layer 3: Scripts/References -> deep capability optimization, high cost
```

---

## 9. Gate Rules (unchanged)

Same as v1.1. See `plugin/skills/skill-evolver/references/gate_rules.md`.

Core: **AND logic -- all conditions must be satisfied simultaneously.** Thresholds are configured per-skill in evolve_plan.md.

---

## 10. Memory Schema (paths changed)

Same schema as v1.1, but paths moved from inside evolver to the workspace:
- `<workspace>/evolve/results.tsv`
- `<workspace>/evolve/experiments.jsonl`
- `<workspace>/evolve/best_versions/`

See `plugin/skills/skill-evolver/references/memory_schema.md`.

---

## 11. Artifact Cleanup (new in v2.1)

### Automatic Cleanup

| Artifact | Retention Rule | Command |
|---|---|---|
| best_versions/ | Keep latest 3 | `evolve_loop.py --cleanup-versions` |
| iteration-EN/ | Keep latest 5 + all keep rounds | `evolve_loop.py --cleanup` |
| git history | Squash into one summary commit | `cleanup_git_history()` |

### Git Bloat Prevention

After evolve completes, `cleanup_git_history()` runs:
```bash
# Auto-squash all experiment + revert commits
# "evolve: 65% -> 78%, 6 keeps in 20 iterations"
```

---

## 12. Directory Structure (v2.1 final)

```
skill-evolver/                          <- GitHub repo root
├── .claude-plugin/
│   ├── marketplace.json                <- marketplace listing (source -> ./plugin)
│   └── plugin.json                     <- root manifest
├── plugin/                             <- subset loaded by Claude Code
│   ├── .claude-plugin/
│   │   └── plugin.json                 <- plugin manifest
│   └── skills/
│       └── skill-evolver/
│           ├── SKILL.md (~320 lines)   <- main entry + quick start
│           ├── references/
│           │   ├── evolve_protocol.md  <- 8-phase full protocol
│           │   ├── eval_strategy.md    <- adaptive eval strategy templates
│           │   ├── creator_integration.md <- Creator integration protocol
│           │   ├── gate_rules.md       <- gate rules
│           │   ├── mutation_policy.md  <- layered mutation strategy
│           │   └── memory_schema.md    <- memory schema
│           ├── agents/
│           │   ├── search_agent.md     <- variant generation + diagnosis
│           │   ├── analyzer_agent.md   <- attribution analysis
│           │   ├── grader_agent.md     <- grading (LLM binary + program)
│           │   └── comparator_agent.md <- blind A/B comparison
│           └── scripts/
│               ├── __init__.py
│               ├── common.py           <- shared utilities
│               ├── setup_workspace.py  <- workspace initialization
│               ├── run_l1_gate.py      <- L1 quick gate
│               ├── run_l2_eval.py      <- L2 eval helpers
│               ├── aggregate_results.py <- statistical aggregation
│               └── evolve_loop.py      <- 8-phase orchestration + cleanup
├── docs/
│   ├── architecture-v2.1.md            <- this document
│   └── bootstrap-report.md             <- bootstrap test report
├── README.md
└── LICENSE
```

Total: 18 skill files, ~2700 lines

---

## 13. Changelog: v1.1 to v2.1

| Change | v1.1 | v2.1 | Rationale |
|---|---|---|---|
| evals/outputs/memory | Inside evolver | In per-skill workspace | A generic optimizer should not carry skill-specific data |
| workspace | Created independently | Reuses Creator's | No duplication; shared data |
| eval strategy | Fixed L1/L2/L3 | Adaptive via evolve_plan.md | Different skills need different strategies |
| Creator relationship | Undefined | Reference-based invocation | Creator updates propagate automatically |
| scripts/ | Empty directory | 7 executable scripts | Full automation |
| quick start | None | 4-line examples | 10-second onboarding |
| cleanup | None | 3 cleanup strategies + git squash | Prevent artifact bloat |
| eval_levels.md | Fixed L1/L2/L3 definitions | eval_strategy.md (adaptive) | More flexible |
| agents | Independent implementation | Reference Creator + fallback | No duplication |
| directory structure | Flat | Two-tier (root + plugin/) | Separate human-facing from Claude-loaded |
| Creator path | 3 hardcoded paths | Searches plugins/marketplaces/skills | Supports multiple installation methods |
| scoring | Undefined | LLM binary + program checks | Minimizes variance, maximizes reliability |
| trace storage | None | Per-case trace files in iteration-EN/ | Enables active diagnosis protocol |

---

## 14. Bootstrap Test Results

Executed 2026-04-07 (bootstrapping: evolving the evolver itself):

- **5 manual iterations** (not automated loop -- evolver itself was not git-managed at the time)
- **Improvements landed**: quick start, self-contained eval, CLI execution guide, plan example streamlining
- **316 lines** (down from 344)
- **Key finding**: protocol-type skills require behavioral evaluation (spawn subagent). Static string matching has a hard ceiling.
- **All scripts verified**: L1 gate PASS, all functions independently callable

---

*Document version: v2.1*
*Date: 2026-04-07*
*Status: Architecture refactoring complete + scripts implemented + bootstrap test passed + GitHub repo structure finalized*
*Previous version: v1.1 (2026-04-03)*
