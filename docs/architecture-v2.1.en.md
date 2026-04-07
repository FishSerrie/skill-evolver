# Skill Evolver -- Technical Architecture v2.1

> Supersedes v1.1. Reflects all refactoring and implementation as of 2026-04-07.
> Changelog at the end covers v1.1 -> v2.1 deltas.

---

## 1. Goal

Build a single-entry-point Skill Evolver that:

- Bootstraps an initial skill from a task description and ground-truth (GT) data
- Measures skill quality through an adaptive, multi-tier evaluation pipeline
- Iterates the skill autonomously via an AutoResearch-style **outer loop**
- Yields the current-best skill snapshot under strict multi-**gate** constraints

**External surface**: one skill, one entry point.
**Internal structure**: 5 modes, pipeline orchestration, 4-layer architecture.

---

## 2. Product Shape

### 2.1 Five Modes

| Mode | Responsibility | Typical Use |
|---|---|---|
| **Create** | Generate an initial skill from requirements + GT | New skill from scratch |
| **Eval** | Standalone evaluation pass; produce a benchmark | Understand current quality |
| **Improve** | Human-directed, targeted fix | Semi-automated optimization |
| **Benchmark** | Systematic comparison (A/B, blind eval) | Informed version-selection decisions |
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

The Evolver invokes Creator capabilities by reference. When Creator ships an update, the Evolver inherits it automatically -- zero duplication, zero drift.

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

**Fallback**: when Creator is unavailable the Evolve core loop is unaffected. Evaluation falls back to the built-in `LocalEvaluator` (see Section 4c).

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

### 4a. Scoring Philosophy: LLM Binary + Program Scoring

Every assertion falls into exactly one of two categories:

| Category | Examples | Execution | Variance |
|---|---|---|---|
| **Program-only checks** | `regex`, `script_check`, `json_schema`, `file_exists` | Deterministic; no LLM call | Zero |
| **LLM binary classification** | `contains`, `not_contains`, `path_hit`, `fact_coverage` | Single YES/NO prompt via `BinaryLLMJudge` | Bounded to one binary decision |

By splitting assertions this way, the system minimizes scoring noise (no open-ended numeric scales, no verbosity bias, no anchor drift) while retaining semantic evaluation wherever deterministic checks cannot reach.

### 4b. BinaryLLMJudge

`BinaryLLMJudge` (defined in `scripts/evaluators.py`) is the low-level primitive behind every LLM-based assertion.

**Interface**:

- `judge(question: str, context: str) -> bool` -- Sends a YES/NO prompt to the model; parses and returns the boolean result.
- `judge_batch(questions: list[tuple[str, str]]) -> list[bool]` -- Sequential convenience wrapper for multiple questions.
- `reset_stats()` -- Resets cumulative token-usage and wall-clock counters (used for cost attribution).

Constraining every semantic check to **binary classification** sidesteps the well-documented reliability problems of numeric LLM scoring: scale drift, anchoring effects, and length bias.

### 4c. Universal Evaluator Architecture

The evaluation layer is **not tied to Skill Creator**. It is built on an abstract `Evaluator` base class (ABC) with a pluggable factory.

| Evaluator | When to Use |
|---|---|
| `LocalEvaluator` | **Default.** Runs GT assertions locally using `BinaryLLMJudge` + program checks. No external dependency. |
| `CreatorEvaluator` | Delegates to Skill Creator's subagent-based eval. Best for behavioral (spawn-and-observe) evaluation of complex skills. |
| `ScriptEvaluator` | Wraps a user-supplied evaluation script (any language, any framework). |
| `PytestEvaluator` | Runs a `pytest` suite against the skill under test. |

The factory function `get_evaluator(config)` reads the evaluator type from `evolve_plan.md` and instantiates the appropriate class. This means the Evolver can evaluate **anything with a measurable output** -- Claude Code skills, standalone scripts, API endpoints, or arbitrary programs. The evaluation engine is a general-purpose harness, not a Skill-Creator accessory.

**v2.1 layer changes**:
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

**The evolver directory stores no skill-specific data.** All artifacts live in the target skill's workspace.

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

### 5a. Meta-Harness Integration: Execution Traces

Each evaluation round persists per-case **execution traces** under `evolve/iteration-E{N}/traces/`. A trace captures the complete skill invocation for a single GT case:

- The prompt sent to the skill
- The skill's full response (including intermediate tool calls)
- Per-assertion pass/fail results with the raw content that was judged

These traces are the primary diagnostic input for the active diagnosis protocol in Phases 1 and 2 (Section 6). The Search Agent must read specific trace files and cite evidence before proposing any mutation. Without traces, diagnosis degenerates into blind guessing -- the single largest source of wasted iterations in early experiments.

**Design decisions**:
- Reuses Creator's `<skill-name>-workspace/` -- no separate directory
- Evolver data is isolated under `evolve/`, so it does not conflict with Creator's `iteration-N/`
- When packaging a skill for distribution, the workspace is naturally excluded

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

### 6a. Active Diagnosis Protocol (Phases 1 and 2)

Phase 2 enforces a strict **active diagnosis** protocol, inspired by the Meta-Harness pattern. Before the Search Agent may propose any mutation, it must complete these steps:

1. **Read execution traces** -- Open the trace files from the most recent failed iteration (`iteration-E{N}/traces/`). Identify the specific assertion that failed and the content that caused the failure.
2. **Cite evidence** -- State the trace file path and section. Example:
   > `iteration-E3/case-15/trace.md`, section "Stage 1: Path Retrieval" -- agent queried index with term "cache policy" but the GT document is indexed under "caching-strategy". Root cause: synonym mismatch in retrieval prompt.
3. **Formulate a counterfactual diagnosis** -- *"Case X failed because of Y. If we change Z, the output would instead do W."* This forces the agent to commit to a causal hypothesis before proposing a fix.
4. **Propose exactly one atomic change** that directly addresses the diagnosed root cause.

If no trace evidence points to a clear cause, the agent must state that explicitly rather than guess. This prevents the outer loop from burning iterations on speculative mutations.

The protocol is enforced in code: `phase_2_prepare_ideation()` injects trace context and a mandatory-diagnosis prompt into the agent's system message. The agent's output is checked for trace citations before proceeding to Phase 3.

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

v1.1 hardcoded three tiers: L1 quick gate, L2 dev set, L3 strict eval. v2.1 replaces this with a fully parameterized system:

1. **evolve_plan.md is generated by Claude** after analyzing the skill's characteristics and GT data
2. Three evaluation tiers (Quick Gate / Dev Eval / Strict Eval) are configured per skill -- sample sizes, timeout budgets, assertion weights, and pass thresholds are all adjustable
3. Different skill types receive different default strategies (see `plugin/skills/skill-evolver/references/eval_strategy.md`)

---

## 8. Layered Mutation (unchanged)

Same as v1.1. See `plugin/skills/skill-evolver/references/mutation_policy.md`.

Each **atomic change** targets exactly one mutation layer:

```
Layer 1: Description -> trigger F1 optimization, low cost
Layer 2: SKILL.md Body -> behavioral quality optimization, medium cost
Layer 3: Scripts/References -> deep capability optimization, high cost
```

The outer loop escalates from Layer 1 to Layer 3 only when lower layers plateau.

---

## 9. Gate Rules (unchanged)

Same as v1.1. See `plugin/skills/skill-evolver/references/gate_rules.md`.

Core invariant: **AND logic -- all conditions must pass simultaneously.** A mutation that improves pass_rate but causes a **regression** on any existing case is discarded. Thresholds are configured per-skill in evolve_plan.md.

---

## 10. Memory Schema (paths changed)

Same schema as v1.1, but paths relocated from the evolver directory to the target workspace:
- `<workspace>/evolve/results.tsv`
- `<workspace>/evolve/experiments.jsonl`
- `<workspace>/evolve/best_versions/`

See `plugin/skills/skill-evolver/references/memory_schema.md`.

---

## 11. Artifact Cleanup (new in v2.1)

### Automatic Cleanup

| Artifact | Retention Rule | Command |
|---|---|---|
| best_versions/ | Keep latest 3 **snapshots** | `evolve_loop.py --cleanup-versions` |
| iteration-EN/ | Keep latest 5 + all "keep" rounds | `evolve_loop.py --cleanup` |
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
│   ├── architecture-v2.1.md            <- this document (Chinese)
│   ├── architecture-v2.1.en.md         <- this document (English)
│   └── bootstrap-report.md             <- bootstrap test report
├── README.md
└── LICENSE
```

Total: 18 skill files, ~2700 lines

---

## 13. Changelog: v1.1 -> v2.1

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
| evaluator architecture | Tied to Creator | Universal (abstract `Evaluator` ABC + factory) | Works with any skill type or eval engine |
| diagnosis method | Ad-hoc | Active diagnosis with mandatory trace citation + counterfactual diagnosis | Prevents blind guessing in the outer loop |

---

## 14. Bootstrap Test Results

Executed 2026-04-07 (bootstrapping: evolving the evolver itself):

- **5 manual iterations** (not the automated loop -- the evolver itself was not yet git-managed)
- **Improvements landed**: quick start, self-contained eval, CLI execution guide, plan-example streamlining
- **316 lines** (down from 344)
- **Key finding**: protocol-type skills require behavioral evaluation (spawn subagent). Static string matching hits a hard ceiling.
- **All scripts verified**: L1 gate PASS, all functions independently callable

---

*Document version: v2.1*
*Date: 2026-04-07*
*Status: Architecture refactoring complete + scripts implemented + bootstrap test passed + GitHub repo structure finalized*
*Previous version: v1.1 (2026-04-03)*
