---
name: skill-evolver
description: "Automatic skill evolution engine — powered by skill-creator's evaluation capabilities + autoresearch's autonomous iteration methodology. Core: Creator handles evaluation and grading, AutoResearch-style loop handles search and optimization, Evolver adds gating and memory for fully automatic evolution. Supports evolve/eval/create/benchmark/improve modes. Triggers on any natural-language user request to optimize / improve / tune / evolve / evaluate / benchmark a skill: '/skill-evolver', '/evolve', 'evolve skill', 'optimize skill', 'optimize this skill', 'optimize my skill', 'optimize the skill at <path>', 'improve skill', 'improve this skill', 'improve my xxx skill', 'tune skill', 'tune this skill', 'make skill better', 'make this skill better', 'make the skill stronger', 'auto-optimize', 'use skill-evolver', 'use skill-evolver to optimize', 'run skill-evolver on', 'skill eval', 'skill benchmark', 'evaluate this skill', 'benchmark these skills', 'create skill', 'create a new skill', 'skill evolver', '进化 skill', '优化 skill', '优化这个 skill', '帮我优化 skill', '帮我优化这个 skill', '帮我优化 xxx skill', '用 skill-evolver 优化', '用 skill-evolver 调一下', 'skill 评测', '让 skill 变强', '让这个 skill 变强', '自动优化', '改进 skill', '改进这个 skill', '调一下这个 skill', '创建 skill', '新建一个 skill'."
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

# Phase 0 — baseline eval (auto-persists traces for Phase 1 diagnosis)
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from evaluators import LocalEvaluator
from pathlib import Path
r = LocalEvaluator().full_eval(
    Path('<skill-path>'),
    Path('<workspace>/evals/evals.json'),
    split='dev',
    traces_dir=Path('<workspace>/evolve/iteration-E0/traces'),
)
print(r['total_passed'], '/', r['total_assertions'])
"
```

After Phase 0, follow `references/evolve_protocol.md` to run Phases
1–8 directly in the conversation: read memory (`results.tsv` +
`experiments.jsonl` + recent `iteration-E*/traces/`), diagnose failures,
make ONE atomic change with the Edit tool, `git commit`, re-eval, gate,
log, loop.

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

**Prerequisites:**
- **skill-creator installed (hard dependency)** — Evolver refuses to start without it. See installation guide below.
- GT data (test cases + assertions) should be prepared in advance; if unavailable, evolve mode auto-generates them via Creator
- The skill directory **must be under git** (if uninitialized, Phase 0 forces `git init`; if git is not installed, install it first)

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
| **Eval** | `/skill-evolver eval` | Single evaluation pass, produces a benchmark | Yes: calls Creator's run_eval |
| **Improve** | `/skill-evolver improve` | Human-directed targeted improvement | Yes: follows Creator's iteration workflow |
| **Benchmark** | `/skill-evolver benchmark` | Systematic comparison (A/B, blind review) | Yes: calls Creator's comparator/analyzer |
| **Evolve** | `/skill-evolver evolve` | Automated iterative optimization (core) | Partial: evaluation via Creator; search/gating/memory are Evolver's own |

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
        ├── iteration-E1/           ← Evolve eval artifacts (E-prefix distinguishes from Creator)
        │   ├── benchmark.json      ← aggregated stats (run_l2_eval.write_benchmark)
        │   ├── grading.json        ← per-case grades (run_l2_eval.write_grading)
        │   └── traces/             ← per-case execution traces (Meta-Harness diagnosis)
        └── summary.md              ← final report
```

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

### Create Mode

Invokes Creator's creation workflow + additionally generates GT and a workspace.

**Workflow:**
1. Read skill-creator's SKILL.md; follow its "Capture Intent -> Interview -> Write SKILL.md" flow
2. Generate the initial skill
3. **Additional steps (Evolver-specific):**
   - Create the evolve workspace
   - Generate an initial GT data template (trigger + behavior)
   - Generate evolve_plan.md
4. Output: complete skill + workspace + recommended next step (eval or evolve)

### Eval Mode

Run a single standalone evaluation against a skill, producing a quality report. Does not automatically enter the optimization loop.

**Usage:**
```
/skill-evolver eval <skill-path> [--gt <gt-data-path>]
```

**Workflow:**
1. Check if workspace exists; create it if not
2. If an evaluation plan exists, read the strategy; otherwise use defaults (run all dev cases)
3. Execute evaluation per the strategy:
   - **Trigger evaluation**: call skill-creator's `scripts/run_eval.py` (in Creator's install directory)
   - **Behavior evaluation**: spawn a subagent to run the skill, then grade with the grader
4. Aggregate results, produce benchmark
5. Call Creator's `eval-viewer/generate_review.py` to render results
6. Output improvement suggestions, but **do not start iteration automatically** — the user decides next steps

### Improve Mode

Human-directed targeted improvement. You (Claude) orchestrate the full cycle.

**Workflow:**
1. Read the user's improvement instructions
2. Read the current skill's SKILL.md + latest eval results + execution traces
3. **Diagnose**: Read traces from the most recent eval (`evolve/iteration-E{N}/traces/`) to understand WHY specific cases fail
4. **Plan**: Based on trace evidence, propose specific changes to the user (cite case IDs and trace evidence)
5. **Apply**: Make the approved changes using the Edit tool (one atomic change at a time)
6. **Verify**: Run one round of Eval (`python3 scripts/evolve_loop.py <skill> --gt <gt> --run --max-iterations 1`)
7. **Report**: Show before/after comparison with per-case breakdown

**Key difference from Evolve mode**: The human decides WHAT to change; Improve mode provides diagnostic evidence and executes the change. Evolve mode decides autonomously.

### Benchmark Mode

Systematic comparison of two versions.

**Usage:**
```
/skill-evolver benchmark <skill-v1> <skill-v2> --gt <gt-data>
```

**Workflow:**
1. Run eval on both versions
2. Call skill-creator's `scripts/aggregate_benchmark.py` to aggregate (in Creator's install directory)
3. Optional: blind A/B comparison (reads this skill's `agents/comparator_agent.md` or Creator's full version)
4. Optional: attribution analysis (reads Evolver's `agents/analyzer_agent.md`)
5. Output benchmark report

### Evolve Mode (Core)

Automated iterative optimization. The core value of Evolver.

**Usage:**
```
/skill-evolver evolve <skill-path>
```
The user might say "optimize this skill" and provide a path, or "here's some test data" with a file. GT data is not a required argument — if missing, Evolver calls Creator to generate it automatically.

**Full protocol:** see `references/evolve_protocol.md`.

**Summary of phases:**

```
Phase 0: Setup    → Create workspace + generate evolve_plan + establish baseline
Phase 1: Review   → Read memory (results.tsv + experiments.jsonl + git log)
Phase 2: Ideate   → Analyze failure modes, decide what to change (read agents/search_agent.md)
                     Uses active diagnosis with execution traces (Meta-Harness pattern):
                     replay failing cases, collect execution traces, then apply
                     counterfactual diagnosis to isolate root causes
                     Anti-patterns (do not guess, do not bundle unrelated changes,
                     do not repeat a discarded change) are enforced in agents/search_agent.md.
Phase 3: Modify   → Make one atomic change
Phase 4: Commit   → git commit (mandatory — skill must be under git; if not, git init first)
Phase 5: Verify   → Evaluate per evolve_plan strategy (calls Creator's evaluation capabilities)
Phase 6: Gate     → Multi-gate keep/discard/revert decision (read references/gate_rules.md)
Phase 7: Log      → Record to results.tsv + experiments.jsonl (read references/memory_schema.md)
Phase 8: Loop     → Continue or terminate
```

**Layered optimization strategy:**

```
Layer 1: Description (trigger optimization) → call Creator's run_loop.py
Layer 2: SKILL.md Body (behavior optimization) → Evolver's own capability
Layer 3: Scripts/References (deep capability) → Evolver's own capability
```

Hard constraint: only advance to the next layer when the current one plateaus. Cross-layer changes are not allowed. See `references/mutation_policy.md`.

**Once Evolve mode is entered, start executing the loop immediately. Do not wait for user instructions. Do not ask the user to run commands.** You (Claude) are the executor:

1. Call `python3 scripts/setup_workspace.py <skill-path>` to create the workspace
2. **Prepare GT data (using Creator's capabilities):**
   - Check if `<workspace>/evals/evals.json` already exists -> use it if so
   - Check if the user provided data in the conversation (file paths, QA pairs, samples) -> construct GT from it
   - If neither exists -> **invoke skill-creator's test case generation workflow**:
     - Read the "Test Cases" section of Creator's SKILL.md for methodology
     - Follow Creator's flow: understand skill -> write realistic test prompts -> run once -> draft assertions
     - Save to `<workspace>/evals/evals.json`
   - **Do not invent your own construction method** — Creator's workflow is battle-tested; reuse it directly
   - If the user provided partial data (e.g., a few QA pairs), first convert them to standard GT format via Creator's workflow, then generate additional cases
   - When new edge cases are discovered during iteration, supplement the GT using Creator's methodology
3. Read GT data, run baseline evaluation on the SKILL.md, record baseline to results.tsv
4. Begin the loop:
   - Read memory -> analyze failures -> decide what to change -> make atomic edit with Edit -> git commit
   - Run `python3 scripts/run_l1_gate.py <skill-path>` for verification
   - Grade each case and assertion individually (L2 eval)
   - Decide keep/discard -> if discard, git revert
   - Write results.tsv + experiments.jsonl
   - Decide whether to continue
5. Output summary when the loop terminates
6. **Launch the eval viewer for human review**: After the loop completes (and after holdout eval + cleanup), `evolve_loop.py` automatically calls Creator's `eval-viewer/generate_review.py` to render a static HTML review at `<workspace>/evolve/review.html`. The user opens this file to see the per-iteration trajectory, per-case grades, and best-version diff. This is the final hand-off to the human.

Helper scripts (in `scripts/`) handle deterministic steps, but **you reason about what to change and how**.

For unattended background execution (outside a conversation), use CLI mode:
```bash
python3 scripts/evolve_loop.py <skill-path> --gt <gt-json> --run --max-iterations 20
```
This uses `claude -p` subprocesses for LLM reasoning. But **the default scenario is you executing the loop directly in conversation**.

Cleanup intermediate artifacts:
```bash
python3 scripts/evolve_loop.py <skill-path> --cleanup
python3 scripts/evolve_loop.py <skill-path> --cleanup-versions
```

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
