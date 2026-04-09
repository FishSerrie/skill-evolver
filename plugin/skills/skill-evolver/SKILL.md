---
name: skill-evolver
description: "Automatic skill evolution engine: skill-creator (eval) + AutoResearch (iteration) + multi-gate + memory. Modes: evolve/eval/create/benchmark/improve. Triggers on natural-language requests to optimize, improve, tune, evaluate, benchmark, or create a skill. EN: '/skill-evolver', '/evolve', 'optimize this skill', 'optimize my skill', 'improve this skill', 'make this skill better', 'tune this skill', 'use skill-evolver', 'use skill-evolver to optimize', 'run skill-evolver on', 'evaluate this skill', 'benchmark skills', 'create a new skill', 'auto-optimize'. ZH: '优化这个 skill', '优化 skill', '帮我优化', '帮我优化这个 skill', '帮我调一下 skill', '用 skill-evolver 优化', '用 skill-evolver 调一下', '让这个 skill 变强', '改进 skill', '改进这个 skill', '创建 skill', '新建 skill', '自动优化', 'skill 评测'."
---

# Skill Evolver

A unified skill optimizer centered on ground-truth data, powered by Creator for evaluation and AutoResearch for search.

## How the user invokes it

Users invoke skill-evolver with natural-language requests — Claude
recognizes the intent from the description triggers above and runs the
8-Phase loop on the skill they asked about. The user does NOT think
about CLI flags, subprocess modes, or script paths; Claude handles all
the mechanics internally. Common user asks that should activate this
skill:

| What the user says                              | What Claude does                  |
|---|---|
| "Help me optimize the skill at `./my-pdf-skill`" | Run evolve mode on that path      |
| "帮我优化一下这个 skill" (with a path)            | Run evolve mode on that path      |
| "Use skill-evolver to tune `./foo`"              | Run evolve mode on that path      |
| "/skill-evolver evolve ./my-skill"               | Run evolve mode on that path      |
| "/evolve ./my-skill"                             | Run evolve mode on that path      |
| "Evaluate this skill, don't change anything"     | Run eval mode only                |
| "Compare `./v1` and `./v2`"                      | Run benchmark mode                |
| "Create a new skill for X"                       | Run create mode (Creator workflow)|
| "Show me what the first iteration would change" | Run evolve with `--dry-run`       |

Once triggered, Claude takes over and **executes the 8-Phase loop
directly in the conversation** (reading memory, diagnosing failures,
making atomic edits, committing, gating, logging) without asking the
user to run any commands. The user watches the progress in the
conversation and can audit every step.

## Quick Start (for Claude — the executor)

This section is Claude's internal recipe. End users don't run these
commands directly; Claude runs them when handling a user request.

```bash
# Phase 0 — workspace bootstrap (deterministic, runs once)
python3 scripts/setup_workspace.py <skill-path>

# Phase 0 — baseline eval (auto-persists per-case JSON for Phase 1 diagnosis)
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from evaluators import LocalEvaluator
from pathlib import Path
r = LocalEvaluator().full_eval(
    Path('<skill-path>'),
    Path('<workspace>/evals/evals.json'),
    split='dev',
    cases_dir=Path('<workspace>/evolve/iteration-E0/cases'),
)
print(r['total_passed'], '/', r['total_assertions'])
"
```

After Phase 0, follow `references/evolve_protocol.md` to run Phases
1–8 directly in the conversation: read memory (`results.tsv` +
`experiments.jsonl` + most-recent `iteration-E*/meta.json` + the
specific failing `iteration-E*/cases/case_{id}.json` files that
Phase 1 lists in `failed_case_paths`), diagnose failures, make ONE
atomic change with the Edit tool, `git commit`, re-eval, gate, log,
loop.

### Unattended / background runs

For CI runs, scheduled sweeps, or any run without a human/agent in the
conversation, there is a CLI fallback that spawns `claude -p`
subprocesses for LLM reasoning:

```bash
python3 scripts/evolve_loop.py ./my-skill/ --gt ./evals.json --run --max-iterations 20
python3 scripts/evolve_loop.py ./my-skill/ --gt ./evals.json --dry-run   # preview only
python3 scripts/evolve_loop.py ./my-skill/ --cleanup                     # prune eval artifacts
python3 scripts/evolve_loop.py ./my-skill/ --cleanup-versions            # prune best_versions
```

CLI mode is the **fallback**, not the primary path — the primary path
is triggered by the natural-language user asks in the table above.
**Meta-optimization (optimizing skill-evolver itself) only works in
conversation**, because the CLI's subprocess starts with empty context
and can't audit its own protocol against the code it's running.

## Prerequisites

Everything skill-evolver depends on, in two groups — what's needed for
the natural-language conversation path (the primary path), and what's
additionally needed for the CLI `--run` fallback.

### Hard dependencies (both paths)

| Dependency | Why | How to install / check |
|---|---|---|
| **Python 3.10+** | Uses PEP 604 union type hints (`X \| None`) without `from __future__ import annotations` in `evolve_loop.py`, `common.py`, `run_l1_gate.py`, `run_l2_eval.py`, `setup_workspace.py`, `aggregate_results.py`. Runtime type evaluation fails on 3.9 or older. | `python3 --version` → must be ≥ 3.10 |
| **git** | `phase_0_setup` requires git (auto-inits if skill dir isn't a repo, refuses if `git` is not on PATH); `phase_4_commit` uses `git add -u` + `git commit`; `git_revert_last` uses `git revert`; `phase_1_review` reads `git log` for Phase 2 diagnosis. No fallback — see `references/evolve_protocol.md` Phase 4 Step 3. | `git --version` or install per platform: `brew install git` / `apt install git` / [git-scm.com](https://git-scm.com/download) |
| **skill-creator** (plugin) | Hard dependency. `require_creator()` in `scripts/common.py` raises `CreatorNotFoundError` with install instructions if absent. Needed for L1 gate validation (`quick_validate.py`), `grader.md` / `comparator.md` / `analyzer.md` agent pointers, trigger-f1 eval (`run_eval.py`), and optional `eval-viewer/generate_review.py` post-run HTML report. | See "Installing skill-creator" below |

### Soft dependencies (CLI `--run` mode only — primary path doesn't need them)

| Dependency | Why | Fallback |
|---|---|---|
| **LLM CLI on PATH** — one of `claude`, `codex`, `opencode` | CLI `--run` mode's Phase 2+3 (`phase_2_3_ideate_and_modify`) shells out via `_call_llm()` in `scripts/llm.py` to invoke LLM reasoning in a subprocess. Auto-detected in that order; override with `LLM_BACKEND=<name>`. | HTTP endpoint via `EVOLVER_LLM_URL` env var; or use the primary in-conversation path where Claude IS the LLM and no subprocess is needed. |
| **GT data** (`<workspace>/evals/evals.json`) | Supplies the test cases + assertions every iteration is scored against. | `auto_construct_gt` (in `scripts/llm.py`) generates a starter GT from the skill's SKILL.md when missing — requires an LLM CLI, so only works in CLI mode. In the conversation path, Claude constructs GT interactively with the user. |

### What the primary (conversation) path does NOT need

- **No LLM CLI subprocess** — Claude (the conversation itself) is the LLM. The in-conversation executor uses the Edit tool for mutations and a few Python one-liners for deterministic helpers; there is zero `claude -p` shell-out.
- **No pre-existing GT** — if evals.json is missing, Claude interviews the user or infers cases from the skill's SKILL.md inside the conversation, using Creator's test-case methodology by reference.

### Installing skill-creator

skill-creator is a hard dependency. The lookup is performed by `require_creator()` in `scripts/common.py`, which raises with these install instructions if no install is found. Install in one of three ways:

1. **Plugin marketplace (recommended):** In Claude Code, run `/install skill-creator`. Lookup searches `~/.claude/plugins/marketplaces/*/plugins/skill-creator/` first.

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

---

## Core Principles

- **Outer loop searches, inner loop evaluates**: AutoResearch-style iteration decides *what to change*; Creator-style evaluation measures *how well the change worked*
- **GT First**: No optimization starts without ground-truth data
- **One atomic change per iteration**: Each round makes exactly one attributable modification
- **Multi-gate, not single-metric**: Quality, trigger accuracy, cost, latency, and regression are each gated independently
- **Call Creator, don't copy it**: Evaluation, grading, and comparison capabilities come from skill-creator; when Creator updates, Evolver picks up the changes automatically
- **LLM Binary + Program Scoring**: The LLM only makes atomic YES/NO judgments; all numeric scoring, aggregation, and threshold logic is handled by deterministic program code

---

## Relationship with Skill Creator

**Evolver is a superset of Creator.** Creator provides a *single evaluation cycle (human-in-the-loop)*. Evolver adds an *automated outer loop + gates + memory (human-out-of-the-loop)* on top.

- Evolver **references** Creator's capabilities — it does not duplicate code
- When Creator updates, Evolver benefits automatically
- See `references/creator_integration.md` for details

**Creator path discovery order:** See Section 3 of `references/creator_integration.md`. Multiple locations are searched in priority order. If none are found, Evolver errors out with installation instructions — there is no silent degradation.

---

## Five Modes

| Mode | Trigger | Responsibility | Calls Creator? |
|---|---|---|---|
| **Create** | `/skill-evolver create` | Generate an initial skill from requirements + GT | Yes: reads Creator's creation workflow |
| **Eval** | `/skill-evolver eval` | Single evaluation pass, produces a benchmark | Default: `LocalEvaluator` (deterministic, no LLM); opt-in to `CreatorEvaluator` for additional trigger-F1 via Creator's `run_eval.py` |
| **Improve** | `/skill-evolver improve` | Human-directed targeted improvement | Yes: follows Creator's iteration workflow |
| **Benchmark** | `/skill-evolver benchmark` | Systematic comparison (A/B, blind review) | Yes: calls Creator's comparator/analyzer agents |
| **Evolve** | `/skill-evolver evolve` | Automated iterative optimization (core) | Partial: default eval path is `LocalEvaluator` (in `scripts/evaluators.py`); `CreatorEvaluator` / `ScriptEvaluator` / `PytestEvaluator` are opt-in via evolve_plan.md. Search/gating/memory are Evolver's own. |

To run multiple modes in sequence (e.g. create then eval then evolve), invoke them one after another — each mode is idempotent and reuses the same workspace, so chaining them is a conversational concern, not a separate CLI command.

---

## Workspace Mechanism

Evolver **stores no skill-specific data in its own directory**. It reuses Creator's existing workspace directory.

### Workspace = Creator's Workspace + Evolver Extensions

Creator creates `<skill-name>-workspace/` alongside the target skill. Evolver reuses that directory and adds evolve-specific subdirectories.

```
some-project/
├── my-skill/                       ← target skill (user-owned, under git)
│   ├── SKILL.md
│   ├── references/
│   └── scripts/
└── my-skill-workspace/             ← shared workspace (Creator + Evolver)
    ├── evals/                      ← Creator's evaluation data
    │   ├── evals.json
    │   └── checks/                 ← GT-referenced script_check helpers
    │       └── check_*.py          ← (belongs here, NOT under evolve/)
    ├── iteration-1/                ← Creator's eval iterations (pre-existing)
    ├── iteration-2/
    └── evolve/                     ← Evolver-specific subdirectory
        ├── evolve_plan.md          ← adaptive optimization plan
        ├── results.tsv             ← experiment log
        ├── experiments.jsonl       ← fine-grained memory
        ├── best_versions/          ← best skill snapshots
        ├── iteration-E1/           ← Evolve per-iteration artifacts (E-prefix distinguishes from Creator)
        │   ├── meta.json           ← iteration metadata + aggregate snapshot (evolve_loop.write_meta_json)
        │   └── cases/              ← per-case structured traces (paper §2 grep/cat model)
        │       ├── case_001.json   ← one file per GT case, zero-padded ids
        │       ├── case_002.json
        │       └── ...
        └── summary.md              ← final report
```

The per-case JSON files hold the paper's four trace components (prompts / tool calls / model outputs / state updates) in structured form. Grep-friendly for cross-iteration pattern detection:

```bash
grep -l '"pass": false' <workspace>/evolve/iteration-E*/cases/*.json
```

See `references/memory_schema.md` for the full schema and the Meta-Harness (arXiv 2603.28052) alignment rationale.

**Why a shared workspace:**
- The workspace is a sibling directory, not inside the skill — it is naturally excluded when packaging
- Creator's evaluation data (evals/, iteration-N/) can be reused directly by Evolver
- All optimization history for a skill lives in one place

### Workspace Discovery

Evolver looks for the workspace in this order:
1. `<skill-path>/../<skill-name>-workspace/` (Creator's standard location)
2. User-specified via `--workspace`
3. If none exists, Evolver creates one (following Creator's naming convention)

---

## Adaptive Optimization Plan

Evolver **does not hardcode evaluation strategy**. Before optimization begins, it analyzes the target skill and generates `evolve_plan.md`:

### Plan Generation Process

1. Read the target skill's SKILL.md (identify skill type and complexity)
2. Read the GT data (identify assertion type distribution, data volume, split distribution)
3. Generate `evolve_plan.md` based on this analysis — see `references/eval_strategy.md` for templates and examples

---

## Mode Details

Each mode is triggered by the natural-language patterns in the
description field and the "How the user invokes it" table above.
Detailed protocols live in `references/`.

### Create Mode

Invokes Creator's "Capture Intent → Interview → Write SKILL.md" workflow,
then additionally bootstraps the evolve workspace, a GT template, and
an initial `evolve_plan.md`. Output: ready-to-iterate skill + workspace.

### Eval Mode

Single evaluation pass against GT — produces a benchmark, does NOT
enter the iteration loop. Defaults to `LocalEvaluator` (deterministic,
no LLM subprocess); opt in to `CreatorEvaluator` in `evolve_plan.md`
for additional trigger-F1 via Creator's `run_eval.py`. Improvement
suggestions are printed but the user decides whether to proceed.

### Improve Mode

Human-directed targeted fix. Claude reads the latest `iteration-E{N}/
cases/case_{id}.json` files (selectively, via the Read tool — not all
of them, only the ones in `phase_1_review`'s `failed_case_paths`) to
diagnose WHY specific cases fail, proposes changes citing case IDs +
per-assertion evidence, applies approved edits with the Edit tool
(one atomic change at a time), re-runs one eval round, and reports
before/after. **Human decides WHAT to change; Claude provides
diagnostic evidence.** Unlike Evolve mode (which decides autonomously).

### Benchmark Mode

A/B compares two skill versions against the same GT. Example usage:

```
/skill-evolver benchmark ./skill-v1/ ./skill-v2/ --gt ./evals.json
```

Optional blind comparison via `agents/comparator_agent.md` + attribution
analysis via `agents/analyzer_agent.md`. Uses Creator's
`scripts/aggregate_benchmark.py` for the numeric roll-up.

### Evolve Mode (core)

Automated 8-Phase iterative optimization — the core value of Evolver.
Full protocol: `references/evolve_protocol.md`. Uses **layered
mutation**: **Layer 1** (description / trigger) → **Layer 2** (SKILL.md
body) → **Layer 3** (scripts / references) — only advance to the next
layer when the current plateaus, cross-layer changes forbidden. See
`references/mutation_policy.md`.

Entry condition: user says something like "optimize this skill" with a
path. GT data is auto-sourced: if `<workspace>/evals/evals.json` exists
it's used as-is; otherwise Claude interviews the user inside the
conversation (using Creator's test-case methodology by reference) or,
in CLI `--run` mode, `scripts/llm.py::auto_construct_gt` generates
starter cases via the configured LLM CLI.

Claude executes the loop directly in conversation by default — see the
**Quick Start** section at the top of this file for the concrete
recipe. Helper scripts in `scripts/` handle deterministic steps
(`setup_workspace`, `run_l1_gate`, `cleanup_best_versions`) but
**Claude reasons about what to change and how**. After the loop
terminates `orchestrator.run_evolve_loop` auto-launches Creator's
`eval-viewer/generate_review.py` (if available) to render a static
HTML review at `<workspace>/evolve/review.html`.

---

## GT Data Format

The GT schema has a universal layer and scenario-specific extension layers, ensuring skill-evolver works with all skill types.

### Universal Layer (mandatory)

```json
{
  "id": 1,
  "prompt": "The user's input",
  "assertions": [
    {"type": "contains", "value": "key content", "description": "Must include X"}
  ],
  "facts": [
    "Fact point 1 that must be covered",
    "Fact point 2 that must be covered"
  ],
  "split": "dev",
  "metadata": {}
}
```

The `facts` field is used with `fact_coverage` assertions in preset mode. During fact decomposition, each fact point is extracted as an atomic, independently verifiable statement. The grader checks coverage by performing binary YES/NO judgments per fact point, and the program computes the coverage score.

### Universal Assertion Types

| type | Description |
|---|---|
| `contains` | Output contains the specified text |
| `not_contains` | Output must not contain the specified text |
| `regex` | Output matches the regular expression |
| `path_hit` | Output references the correct document path |
| `fact_coverage` | Output covers specified fact points (uses the `facts` field) |
| `script_check` | Run a script to check the output |
| `json_schema` | Output conforms to a JSON schema |
| `file_exists` | A specified file was generated |

### Split Field

Must be labeled `"dev"` / `"holdout"` / `"regression"`. Split strategy is defined in evolve_plan.md.

---

## Gate Rules

See `references/gate_rules.md` for details.

Core principle: **All keep conditions must be satisfied simultaneously (AND logic)**. Default thresholds (`min_delta=0.02`, `trigger_tolerance=0.05`, `max_token_increase=0.20`, `regression_tolerance=0.05`) are overridable per-skill in `evolve_plan.md`.

---

## Memory Structure

See `references/memory_schema.md` for details.

Memory is stored in the target skill's workspace under the `evolve/` subdirectory — not in Evolver's own directory:
- `<workspace>/evolve/results.tsv`: experiment log
- `<workspace>/evolve/experiments.jsonl`: fine-grained memory
- `<workspace>/evolve/best_versions/`: historical best snapshots

---

## Code Organization

`scripts/` is split across 13 single-purpose files, every one ≤ 610 lines.
`from evolve_loop import X` still works for all the symbols listed
below via top-level re-exports and PEP 562 module `__getattr__`, so
external callers don't need to know where a symbol physically lives.

| File | Owns | Lines |
|---|---|---:|
| `scripts/evolve_loop.py` | Phase functions 0/1/4/5/7/8 + `git_revert_last` + `save_best_version` + `persist_cases` + `write_cases_to_dir` + `write_meta_json` + `_list_untracked` + PEP 562 `__getattr__` re-export of orchestrator symbols + `python scripts/evolve_loop.py` CLI entry (delegates to `orchestrator.main`) | 763 |
| `scripts/orchestrator.py` | `run_evolve_loop` (the 8-Phase driver) + `main` (argparse + subcommand dispatch) + `_eval_holdout_or_none` + empty-dev-GT guard + revert-fail abort | 544 |
| `scripts/gate.py` | `phase_6_gate_decision` (pure function, stdlib only) | 134 |
| `scripts/llm.py` | `LLM_BACKENDS` registry + `_call_llm` / `_call_llm_http` / `_call_claude` + `phase_2_3_ideate_and_modify` (with safe-default JSON shape normalization) + `run_l2_eval_via_claude` + `_local_eval` + `auto_construct_gt` + `_validate_gt_schema` | 455 |
| `scripts/cleanup.py` | `_iter_num` (shared numeric-sort helper) + `cleanup_best_versions` + `cleanup_eval_outputs` + `_try_launch_eval_viewer` | 153 |
| `scripts/evaluators.py` | `Evaluator` ABC + `BinaryLLMJudge` + `LocalEvaluator` + `_basic_schema_check` + `get_evaluator` factory (lazy-imports backends) + `parse_evaluator_from_plan` + `EVALUATOR_NAMES` | 608 |
| `scripts/evaluator_backends.py` | `CreatorEvaluator` + `ScriptEvaluator` + `PytestEvaluator` (lazy-loaded by factory) | 321 |
| `scripts/common.py` | Python 3.10+ version gate + Creator path discovery + `find_workspace` + `parse_skill_md` + `validate_frontmatter` + `require_creator` / `CreatorNotFoundError` | 428 |
| `scripts/aggregate_results.py` | `parse_results_tsv` + `calculate_summary` + `format_markdown` + `run_benchmark` A/B + `format_benchmark_markdown` | 389 |
| `scripts/run_l1_gate.py` | L1 quick-gate CLI helper + `run_l1_gate` library function | 193 |
| `scripts/run_l2_eval.py` | L2 eval library helpers: `load_gt` + `aggregate_grades` + `write_benchmark` + `write_grading` | 155 |
| `scripts/setup_workspace.py` | `setup_workspace` library + CLI entry — creates workspace/evals/checks/ layout + evolve_plan.md template | 172 |
| `scripts/__init__.py` | (empty marker file) | 1 |

**Import graph** (DAG, no cycles):

```
              common.py ← (everyone imports Creator discovery + paths)
                 ↑
     ┌───────────┴────────────┐
     │                        │
 evaluators.py            aggregate_results.py
 ├─ lazy→ evaluator_backends.py
 │        └─→ evaluators (ABC)
 │
 ├── gate.py (stdlib only)
 ├── llm.py (Evaluator → evaluators for type hints only)
 └── cleanup.py → aggregate_results (parse_results_tsv)
                ↓
         evolve_loop.py  ← imports gate / llm / cleanup / evaluators
                ↓                           ↑ PEP 562 __getattr__
         orchestrator.py ──────────────────┘
           ← imports phase_* from evolve_loop
           ← delegates CLI back to itself when invoked as
             `python scripts/evolve_loop.py`
```

Two deliberate cycle-breakers:

1. **`evolve_loop.py` lazy re-exports from `orchestrator.py`** via PEP 562
   `__getattr__`. `orchestrator.py` imports phase functions from
   `evolve_loop.py` at load time; back-compat callers doing
   `from evolve_loop import run_evolve_loop` trigger the lazy import
   only on attribute access, keeping the top-level graph a DAG.

2. **`get_evaluator` in `evaluators.py` lazy-imports the three backends**
   (`CreatorEvaluator` / `ScriptEvaluator` / `PytestEvaluator`) from
   `evaluator_backends.py` only when the corresponding config key is
   requested. `import evaluators` leaves `evaluator_backends` absent
   from `sys.modules`, so the default path has zero load-time
   dependency on the alternative backends.

---

## Reference File Index

| File | Contents | When to Read |
|---|---|---|
| `references/evolve_protocol.md` | Full 8-phase Evolve protocol | On entering Evolve mode |
| `references/eval_strategy.md` | Adaptive evaluation strategy templates | When generating evolve_plan |
| `references/gate_rules.md` | Multi-gate rules + pseudocode | During gate decisions |
| `references/mutation_policy.md` | Layered mutation strategy | When deciding what to change |
| `references/memory_schema.md` | results.tsv + experiments.jsonl schema | When reading/writing memory |
| `references/creator_integration.md` | Integration protocol with Creator | When invoking Creator capabilities |
| `agents/search_agent.md` | Variant generation protocol | During Phase 2 (Ideate) |
| `agents/grader_agent.md` | Grading protocol (quick ref; full version in Creator) | During evaluation grading |
| `agents/comparator_agent.md` | Blind A/B comparison (quick ref; full version in Creator) | During Benchmark mode |
| `agents/analyzer_agent.md` | Attribution analysis protocol | When analyzing change effects |
