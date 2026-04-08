# Skill Evolver — Technical Architecture

> Release version: 1.0
> Last updated: 2026-04-08

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

## 3. Relationship with Skill Creator (hard dependency)

### Core Principle: Reference, Don't Copy — Creator is a Hard Dependency

The Evolver invokes Creator capabilities by reference. When Creator ships an update, the Evolver inherits it automatically -- zero duplication, zero drift. **There is no fallback path.** If Creator is missing, `require_creator()` raises `CreatorNotFoundError` at startup with installation instructions.

| Dimension | Skill Creator | Skill Evolver | Relationship |
|---|---|---|---|
| Create | Yes: interview, write SKILL.md, generate evals | Yes (same) | Calls Creator |
| Eval | Yes: spawn subagent + viewer | Yes: 3-tier adaptive eval | Augments |
| Improve | Yes: human reads feedback, edits manually | Yes (same) | Calls Creator |
| Benchmark | Yes: blind A/B | Yes (same) | Calls Creator |
| Evolve | No | Yes (core) | **New** |
| Gate | No | Yes: multi-gate AND logic | **New** |
| Memory | No | Yes: results.tsv + experiments.jsonl + traces | **New** |

### Creator Path Discovery (`scripts/common.py:find_creator_path`)

```python
# Priority 0: user override via environment variable
os.environ.get("SKILL_CREATOR_PATH")

CREATOR_SEARCH_PATHS = [
    "~/.claude/plugins/marketplaces/*/plugins/skill-creator/skills/skill-creator/",
    "~/.claude/skills/skill-creator/",
    ".claude/skills/skill-creator/",
    "/tmp/anthropic-skills-latest/skills/skill-creator/",
]
```

**No fallback. No silent degradation.** When Creator is not found:

```python
from common import require_creator, CreatorNotFoundError
try:
    creator = require_creator()  # caches the result after first resolution
except CreatorNotFoundError as e:
    # Error message includes:
    # - The GitHub URL: https://github.com/anthropics/skills/tree/main/skills/skill-creator
    # - Three install methods (plugin marketplace, manual git clone, env var)
    # - The complete list of paths that were searched
    print(e); sys.exit(2)
```

Users with Creator installed at a non-standard location can specify the path via:
- Environment variable: `export SKILL_CREATOR_PATH=/custom/path`
- CLI argument on `evolve_loop.py`: `--creator-path /custom/path`

**Why hard dependency rather than fallback**: keeping a copy of Creator's grader / comparator inside Evolver causes version drift the moment Creator updates its protocol. By requiring Creator at runtime, every Evolver run uses Creator's latest grading/comparison logic. Evolver's `agents/grader_agent.md` and `agents/comparator_agent.md` are now pointer files that read Creator's full versions via `get_creator_agent_path()`.

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

Layer 2 uses **adaptive tiers** (Quick Gate / Dev Eval / Strict Eval) configured via `evolve_plan.md`, not fixed L1/L2/L3. Layer 3 thresholds are also per-skill, configured in `evolve_plan.md`.

---

## 5. Workspace Layout

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

## 7. Adaptive Evaluation

The evaluation strategy is not hardcoded. It is fully parameterized through `evolve_plan.md`:

1. **evolve_plan.md is generated by Claude** after analyzing the skill's characteristics and GT data
2. Three evaluation tiers (Quick Gate / Dev Eval / Strict Eval) are configured per skill — sample sizes, timeout budgets, assertion weights, and pass thresholds are all adjustable
3. Different skill types receive different default strategies (see `plugin/skills/skill-evolver/references/eval_strategy.md`)

---

## 8. Layered Mutation

See `plugin/skills/skill-evolver/references/mutation_policy.md`.

Each **atomic change** targets exactly one mutation layer:

```
Layer 1: Description -> trigger F1 optimization, low cost
Layer 2: SKILL.md Body -> behavioral quality optimization, medium cost
Layer 3: Scripts/References -> deep capability optimization, high cost
```

The outer loop escalates from Layer 1 to Layer 3 only when lower layers plateau.

---

## 9. Gate Rules

See `plugin/skills/skill-evolver/references/gate_rules.md`.

Core invariant: **AND logic — all conditions must pass simultaneously.** A mutation that improves pass_rate but causes a **regression** on any existing case is discarded. Thresholds are configured per-skill in `evolve_plan.md`.

---

## 10. Memory Schema

All memory artifacts live in the target skill's workspace (never in the evolver directory):
- `<workspace>/evolve/results.tsv` — one row per iteration
- `<workspace>/evolve/experiments.jsonl` — fine-grained per-case records with diagnoses
- `<workspace>/evolve/best_versions/` — snapshots of historical best versions
- `<workspace>/evolve/iteration-E{N}/traces/` — per-case execution traces (Meta-Trace)

See `plugin/skills/skill-evolver/references/memory_schema.md`.

---

## 11. Artifact Cleanup

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

## 12. Directory Structure

```
skill-evolver/                          ← GitHub repo root
├── .claude-plugin/
│   ├── marketplace.json                ← marketplace listing (source → ./plugin)
│   └── plugin.json                     ← root manifest
├── plugin/                             ← subset loaded by Claude Code
│   ├── .claude-plugin/
│   │   └── plugin.json                 ← plugin manifest
│   └── skills/
│       └── skill-evolver/
│           ├── SKILL.md                ← main entry + quick start
│           ├── references/
│           │   ├── evolve_protocol.md       ← 8-phase full protocol
│           │   ├── eval_strategy.md         ← adaptive eval strategy templates
│           │   ├── creator_integration.md   ← Creator integration protocol (hard dependency)
│           │   ├── gate_rules.md            ← gate rules
│           │   ├── mutation_policy.md       ← layered mutation strategy
│           │   └── memory_schema.md         ← memory schema
│           ├── agents/
│           │   ├── search_agent.md          ← variant generation + active diagnosis
│           │   ├── analyzer_agent.md        ← attribution analysis
│           │   ├── grader_agent.md          ← pointer file → Creator's grader.md
│           │   └── comparator_agent.md      ← pointer file → Creator's comparator.md
│           └── scripts/
│               ├── __init__.py
│               ├── common.py                ← shared utilities + require_creator()
│               ├── setup_workspace.py       ← workspace initialization
│               ├── run_l1_gate.py           ← L1 quick gate (calls Creator's quick_validate)
│               ├── run_l2_eval.py           ← L2 eval helpers
│               ├── evaluators.py            ← LocalEvaluator framework + BinaryLLMJudge
│               ├── aggregate_results.py     ← statistical aggregation
│               └── evolve_loop.py           ← 8-phase orchestration + eval viewer launch
├── .agents/skills/skill-evolver/       ← Codex platform variant (auto-synced)
├── .opencode/skills/skill-evolver/     ← OpenCode platform variant (auto-synced)
├── docs/
│   ├── architecture.md                 ← this document (Chinese)
│   └── architecture.en.md              ← this document (English)
├── README.md
├── README_CN.md
└── LICENSE
```

---

*Release: v1.0*
*Date: 2026-04-08*
