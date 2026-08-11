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

### Optimizing a prompt against a dataset (the AutoPrompt path)

Use this instead of the above when the subject is a **prompt** rather than
a skill's documentation, and when the ground truth says what the *output*
should contain rather than what the document should say.

```bash
# Phase 0 — works on a skill directory, a bare prompt file, or one
# section of a file (--section "Rules")
python3 scripts/setup_workspace.py ./answer.md
```

```python
import sys; sys.path.insert(0, 'scripts')
from pathlib import Path
from evaluators import get_evaluator

ev = get_evaluator({
    "evaluator": "grader",       # run the artifact, grade its output
    "grader": "points",          # "points" | "assertions" | "rubric"
    "model": "<model-for-the-candidate>",
    "judge_model": "<model-for-judging>",   # keep these different
    "columns": {                 # every column name is a parameter
        "id": "id",
        "input": ("question", "context"),   # joined into case["input"]
        "points": "expected_points",        # JSON array per row
        "stratify": "category",             # balanced across splits
    },
    "splits": {"dev": 0.7, "holdout": 0.2, "regression": 0.1},
})
result = ev.full_eval(Path("./answer.md"), Path("./gt.csv"), split="dev")
print(result["pass_rate"], result["metrics"], result["primary"])
```

Choosing a grader:

| GT you have | `grader` | How it decides |
|---|---|---|
| Machine-checkable assertions | `assertions` | exact / contains / regex / json_schema / script. Most reliable — no model in the verdict |
| Expected content split into points | `points` | A model partitions the points; the program scores. Over-reporting breaks the conservation equation and the case is rejected |
| Only rules for what "good" means | `rubric` | Each rule is one YES/NO question. **Commit-first is on by default**: the judge answers the task before seeing the candidate, which is the only measure shown to stop plausibility-over-correctness gaming |

Gate thresholds worth setting for a prompt (see
`references/gate_rules.md` for all of them). Put them in
`<workspace>/evolve/evolve_plan.md` — the loop reads them from there, logs
which ones it picked up, and warns about any key it does not recognise:

```
min_metrics:           {"precision": 0.9}   # never trade precision away
max_metric_regression: {"recall": 0.05}     # per-dimension tolerance
max_structure_growth:  0.25                 # cap how much it may grow
max_structure:         {"lines": 200}       # absolute ceiling
```

Structured values are JSON, so quote the keys. A malformed value leaves the
default in place rather than overriding it with something meaningless.

**If a run reports every case as errored, read `errors` before touching the
prompt.** A model that could not be reached is recorded as
`runner: [ERROR: ...]` and excluded from the score, so `pass_rate` reads 0.0
with a non-zero `errored` count. That points at the harness, not the
candidate — check the CLI is installed, or that `EVOLVER_LLM_URL` is set
when using the `http` backend.


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
| **git** | `phase_0_setup` requires git (auto-inits if the artifact isn't in a repo, refuses if `git` is not on PATH). Every git command runs at `Target.vcs_root` and is scoped to `Target.vcs_pathspec`: `phase_4_commit` uses `git commit -m … -- <pathspec>` — a *partial* commit, because a plain `git commit` records the whole index including anything you staged yourself, and a discarded iteration would then revert your work away. `git_revert_last` uses `git restore --source=<commit>~1 -- <pathspec>` plus a scoped commit rather than `git revert`, which takes no pathspec and refuses to run at all when you have unrelated staged changes. `phase_1_review` reads `git log` for Phase 2 diagnosis. No fallback — see `references/evolve_protocol.md` Phase 4 Step 3. | `git --version` or install per platform: `brew install git` / `apt install git` / [git-scm.com](https://git-scm.com/download) |

Nothing else is required. In particular **skill-creator is optional** —
see below.

### Optional enhancements (degrade independently, never abort the loop)

| Dependency | What it adds when present | Behaviour when absent |
|---|---|---|
| **skill-creator** (plugin) | (a) a redundant frontmatter cross-check in the L1 gate via `scripts/quick_validate.py`; (b) opt-in trigger-F1 through `CreatorEvaluator` → `scripts/run_eval.py`; (c) the post-run HTML review via `eval-viewer/generate_review.py`. | Each degrades on its own. Frontmatter validation is handled by `common.validate_frontmatter`, which is authoritative and enforces the same rule set (allowed-key allow-list, kebab-case naming, name/description length limits, no angle brackets) using **only the stdlib — no PyYAML**. The L1 gate records `creator_validate` as `skipped: true` rather than counting the absence as a pass or a failure. Trigger-F1 is unavailable; `LocalEvaluator` (the default) is unaffected. `_try_launch_eval_viewer` returns `False` and the run's real artifacts (`results.tsv`, `experiments.jsonl`, per-case JSON) are written regardless. |

This matters for portability: skill-creator is distributed as a Claude
Code plugin, so on Codex / OpenCode / other hosts it is typically
absent. The evolve loop is designed to run standalone there.

### Soft dependencies (CLI `--run` mode only — primary path doesn't need them)

| Dependency | Why | Fallback |
|---|---|---|
| **LLM CLI on PATH** — one of `claude`, `codex`, `opencode` | CLI `--run` mode's Phase 2 (`phase_2_diagnose`) and Phase 3 (`phase_3_modify`) each shell out via `_call_llm()` in `scripts/llm.py` to invoke LLM reasoning in a subprocess — two separate calls, not one (Module B isolation). Auto-detected in that order; override with `LLM_BACKEND=<name>`. | HTTP endpoint via `EVOLVER_LLM_URL` env var; or use the primary in-conversation path where Claude IS the LLM and no subprocess is needed. |
| **GT data** (`<workspace>/evals/evals.json`) | Supplies the test cases + assertions every iteration is scored against. | `auto_construct_gt` (in `scripts/llm.py`) generates a starter GT from the skill's SKILL.md when missing — requires an LLM CLI, so only works in CLI mode. In the conversation path, Claude constructs GT interactively with the user. |

### What the primary (conversation) path does NOT need

- **No LLM CLI subprocess** — Claude (the conversation itself) is the LLM. The in-conversation executor uses the Edit tool for mutations and a few Python one-liners for deterministic helpers; there is zero `claude -p` shell-out.
- **No pre-existing GT** — if evals.json is missing, Claude interviews the user or infers cases from the skill's SKILL.md inside the conversation, using Creator's test-case methodology by reference.

### Installing skill-creator (optional)

skill-creator is optional — the loop runs without it. Install it only if
you want the optional enhancements listed above. Discovery is performed
by `find_creator_path()` in `scripts/common.py`, which returns `None`
when nothing is found; callers degrade rather than raise. Install in one
of three ways:

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
analysis via `agents/analyzer_agent.md`. Uses
`scripts/aggregate_results.py` for the numeric roll-up.

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

`scripts/` is split across 19 single-purpose files after the 2026-04-10 slim split extracted the trace-enrichment helpers and `BinaryLLMJudge` out of the 1053-line monolith `evaluators.py`, plus four files added by the multi-agent evolution architecture upgrade (Modules A/B/D): `behavioral_runner.py`, `migrate_to_behavioral.py`, `isolation.py`, and `verifier_panel.py`.

`from evolve_loop import X` still works for all the symbols listed
below via top-level re-exports and PEP 562 module `__getattr__`, so
external callers don't need to know where a symbol physically lives.
Likewise `from evaluators import BinaryLLMJudge` and
`from evaluators import <any trace helper>` keep working because
`evaluators.py` re-exports everything from the two new leaf modules.

| File | Owns | Lines |
|---|---|---:|
| `scripts/target.py` | `Target` ABC + `SkillTarget` / `PromptFileTarget` / `SectionTarget` + `resolve_target` factory + `SNAPSHOT_KEYS` structural contract — what is being optimized, behind polymorphism so no phase branches on artifact shape. `read()` yields the mutable text, `context()` the text an evaluation is scored against (they differ for a skill: references are read at run time but only SKILL.md is rewritten in one step) | 985 |
| `scripts/graders.py` | `BaseGrader` template method + `ProgrammaticGrader` (exact/contains/regex/json_schema/script via an extensible `CHECKS` registry) + `PointCoverageGrader` + `RubricGrader` (commit-first judging) + `_JudgeBackedGrader` shared base. Classifies only — contains **no division at all**, asserted on the parse tree; all arithmetic lives in `scoring` | 916 |
| `scripts/datasets.py` | `ColumnMap` + `CaseLoader` / `CsvCaseLoader` / `JsonCaseLoader` + `load_cases` + `split_cases` + `describe_splits` — external ground truth into generic cases, with **every column name a parameter**. Splits are deterministic (hashed from case id) so a score change cannot come from a shifted split | 571 |
| `scripts/grader_evaluator.py` | `GraderEvaluator` (runs the artifact, grades the output) + `PromptRunner` + `build_grader` factory. Sequences target/runner/grader without branching on any of them; opt-in as `evaluator: grader`, existing evaluators untouched | 392 |
| `scripts/gate.py` | `phase_6_gate_decision` + `check_structure` (size budget, reads only `SNAPSHOT_KEYS`) + `check_metric_thresholds` (per-dimension floors and regression tolerances) — pure functions, stdlib only | 325 |
| `scripts/scoring.py` | The single sets-to-numbers implementation: `Outcome` + `compute_prf` + `check_conservation` + `ConservationError`. Pure functions, stdlib only — no LLM, no IO. The conservation equation is what makes a classifier's over-reporting structurally impossible to hide | 216 |
| `scripts/judgment.py` | `Judgment` (frozen; multi-dimensional `metrics` + `primary` + `feedback` + `error`) + `aggregate` — the only type crossing between grading and the engine. Errored cases are excluded from the mean, never scored as zero | 164 |
| `scripts/json_extract.py` | `extract_json_object` — the one implementation of "pull the JSON object out of an LLM reply" (five copies collapsed into this) | 80 |
| `scripts/evolve_loop.py` | Phase functions 0/1/4/5/7/8 + `git_revert_last` + `save_best_version` + `persist_cases` + `write_cases_to_dir` + `write_meta_json` + `_list_untracked` + dynamic `suggested_greps` tailored to failing assertion types + PEP 562 `__getattr__` re-export of orchestrator symbols + `python scripts/evolve_loop.py` CLI entry (delegates to `orchestrator.main`) | 822 |
| `scripts/llm.py` | `LLM_BACKENDS` registry + `_call_llm` / `_call_llm_http` / `_call_claude` + `phase_2_diagnose` / `phase_3_modify` (Module B, two isolated calls) + `phase_2_3_ideate_and_modify` (deprecated wrapper, kept for back-compat) + `phase_6_5_review` (Module D, three isolated verifier calls) + `run_l2_eval_via_claude` + `_local_eval` + `auto_construct_gt` + `_validate_gt_schema` | 556 |
| `scripts/orchestrator.py` | `run_evolve_loop` (the 8-Phase driver) + `main` (argparse + subcommand dispatch) + `_eval_holdout_or_none` + empty-dev-GT guard + revert-fail abort | 548 |
| `scripts/evaluators.py` | `Evaluator` ABC + `LocalEvaluator` (thin `_evaluate_assertion` dispatcher that delegates to `trace_enrichment` module functions for all rich helpers) + `get_evaluator` factory (lazy-imports backends) + `parse_evaluator_from_plan` + `EVALUATOR_NAMES` + back-compat re-exports of `BinaryLLMJudge` (from `binary_judge`) and all trace helpers (from `trace_enrichment`) | 531 |
| `scripts/trace_enrichment.py` | Paper §3 four-component trace helpers as pure module functions: `locate_in_corpus` / `excerpt` / `nearest_match` (state updates) + `build_skill_snapshot` (state updates) + `check_script_rich` (tool calls) + `check_fact_coverage_rich` (model outputs, takes `judge` as explicit param) + `check_json_schema_rich` (state updates with failure path) + `basic_schema_check` / `basic_schema_check_with_path` | 478 |
| `scripts/common.py` | Python 3.10+ version gate + Creator path discovery + `find_workspace` + `parse_skill_md` + `validate_frontmatter` + skill-layout single definition (`SKILL_PROSE_DIRS` / `SKILL_CODE_DIRS` / `iter_skill_prose` / `build_skill_corpus`) + `require_creator` / `CreatorNotFoundError` | 578 |
| `scripts/aggregate_results.py` | `parse_results_tsv` + `calculate_summary` + `format_markdown` + `run_benchmark` A/B + `format_benchmark_markdown` | 389 |
| `scripts/evaluator_backends.py` | `CreatorEvaluator` + `ScriptEvaluator` + `PytestEvaluator` (lazy-loaded by factory; forwards `cases_dir` kwarg to LocalEvaluator.full_eval) + `BehavioralEvaluator(LocalEvaluator)` (Module A: routes each assertion to the real transcript or the static skill doc corpus based on the case's `target` field) | 619 |
| `scripts/run_l1_gate.py` | L1 quick-gate CLI helper + `run_l1_gate` library function + P0 quality rules (SEC001-006, S003+, S004+, S007, TD011, C001, C005) with code-markup stripping | 487 |
| `scripts/binary_judge.py` | `BinaryLLMJudge` class — atomic YES/NO LLM calls with `judge_with_reasoning` rationale capture (paper §3 "model outputs" trace component); lazy-imports `_call_llm` from `llm` module with stdlib-only fallback | 190 |
| `scripts/setup_workspace.py` | `setup_workspace` library + CLI entry — takes a `Target` (callers holding a path resolve it via `resolve_target` first), so a bare prompt file or one section of a file is a valid subject; creates workspace/evals/checks/ layout + evolve_plan.md template with a structural baseline | 214 |
| `scripts/isolation.py` | Module B proposer/evaluator isolation: `build_diagnoser_prompt` / `build_diagnoser_task_spec` + `build_mutator_prompt` / `build_mutator_task_spec` + `parse_diagnosis_response` / `parse_mutation_response` — narrow function signatures keep the diagnoser blind to holdout evidence and the mutator blind to raw diagnosis text | 310 |
| `scripts/behavioral_runner.py` | Module A behavioral runner: `build_behavioral_prompt` / `build_behavioral_task_spec` (conversation mode) + `run_case_behaviorally` (CLI subprocess mode via `llm.py`'s `LLM_BACKENDS`) + `build_transcript_from_text` — produces a real execution transcript instead of grading the static skill corpus | 264 |
| `scripts/verifier_panel.py` | Module D adversarial review panel: `CHECKERS` (overfit / assertion_gaming / structural) + per-checker suspicion-prompt builders + `build_verifier_task_spec` + `aggregate_verdicts` — independent post-gate re-check of a candidate before it's treated as a keep | 245 |
| `scripts/cleanup.py` | `_iter_num` (shared numeric-sort helper) + `cleanup_best_versions` + `cleanup_eval_outputs` + `_try_launch_eval_viewer` (reads latest meta.json) | 334 |
| `scripts/migrate_to_behavioral.py` | CLI script: back-fills `target: "output" \| "skill_doc"` onto an existing evals.json for `BehavioralEvaluator`; writes a sibling `.migrated.json` + `migration_report.md` for human review, never overwrites the input | 166 |
| `scripts/run_l2_eval.py` | L2 eval library helpers: `load_gt` + `aggregate_grades` + `calculate_stats` (write_benchmark / write_grading removed in the Meta-Harness refactor — now handled by evolve_loop.write_meta_json + persist_cases) | 139 |
| `scripts/__init__.py` | (empty marker file) | 1 |

**Import graph** (DAG, no cycles):

```
           common.py ← (everyone imports Creator discovery + paths)
               ↑
               │
    ┌──────────┴───────────────────────────┐
    │                                      │
    │        binary_judge.py               │
    │          (stdlib only;               │
    │           lazy → llm._call_llm)      │
    │                 ↓                    │
    │        trace_enrichment.py           │
    │          (stdlib + common;           │
    │           lazy → common.find_workspace)
    │                 ↓                    │
    └──→ evaluators.py ←────────────── aggregate_results.py
         ├─ re-exports BinaryLLMJudge (from binary_judge)
         ├─ re-exports trace helpers   (from trace_enrichment)
         └─ lazy → evaluator_backends.py
                   └─→ evaluators (ABC / LocalEvaluator)

    gate.py (stdlib only)
    llm.py
    cleanup.py → aggregate_results (parse_results_tsv)
         ↓
    evolve_loop.py  ← imports gate / llm / cleanup / evaluators
         ↓                           ↑ PEP 562 __getattr__
    orchestrator.py ──────────────────┘
      ← imports phase_* from evolve_loop
      ← delegates CLI back to itself when invoked as
        `python scripts/evolve_loop.py`
```

`binary_judge.py` and `trace_enrichment.py` are pure leaves in the
dependency tree (they only import from `common` and, lazily, from
`llm`). Nothing imports from `evaluators` into them, so the split
could not possibly introduce a cycle.

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
| `references/isolation_protocol.md` | Module B proposer/evaluator isolation protocol (narrow function signatures for diagnoser/mutator) | When wiring Phase 2/3 through `isolation.py` |
| `agents/search_agent.md` | Variant generation protocol | During Phase 2 (Ideate) |
| `agents/grader_agent.md` | Grading protocol (quick ref; full version in Creator) | During evaluation grading |
| `agents/comparator_agent.md` | Blind A/B comparison (quick ref; full version in Creator) | During Benchmark mode |
| `agents/analyzer_agent.md` | Attribution analysis protocol | When analyzing change effects |
