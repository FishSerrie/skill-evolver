<div align="center">

# Skill Evolver

**Point it at a skill. Wake up to a better skill.**

*Autonomous evolution engine for AI coding agent skills — search, verify, gate, keep, repeat.*

[![Claude Code Skill](https://img.shields.io/badge/Claude_Code-Skill-blue?logo=anthropic&logoColor=white)](https://docs.anthropic.com/en/docs/claude-code)
[![OpenCode](https://img.shields.io/badge/OpenCode-Skill-purple)](https://opencode.ai)
[![Codex](https://img.shields.io/badge/Codex-Skill-green?logo=openai&logoColor=white)](https://developers.openai.com/codex)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)

**English** · [中文](docs/README_CN.md) · [Architecture](docs/architecture.en.md)

</div>

---

```
            ┌──────────────────┐
            │    Your Skill    │
            └────────┬─────────┘
                     │
                     ▼
    ┌──────────────────────────────────┐
    │          Skill Evolver           │
    │                                  │
    │    search → modify → evaluate    │
    │      → gate → keep/discard       │
    │             → repeat             │
    └────────────────┬─────────────────┘
                     │
                     ▼
            ┌──────────────────┐
            │  A Better Skill  │
            └──────────────────┘
```

---

## In one sentence: train your Skill the way you'd train a model

If you've done machine learning, you already understand what Skill Evolver does.

| Training a model | Training a Skill (Skill Evolver) |
|---|---|
| Training data | **GT (Ground Truth)** — test cases + assertions in evals.json |
| Define loss function | **8 assertion types + 5-way AND gate** — multi-dimensional definition of "is this skill actually good?" |
| Train (SGD / iteration) | **8-phase loop** — search → modify → evaluate → gate → keep/discard → repeat |
| Pick a checkpoint | **best_versions/** — snapshot on every keep, select the best |
| Overfitting detection | **holdout split** (Anti-Goodhart) — never shown to the proposer during iteration |
| Regression test | **regression split** — make sure improving A doesn't break B |
| Learning rate / step size | **Layered mutation** (description → body → scripts) — small changes first, big ones later |
| Early stopping | **Stuck detection + convergence** — N consecutive discards triggers layer promotion or halt |

**The key insight**: this is NOT about making a skill's "syntax check" pass rate higher. Just like training a model isn't about making the code compile — it's about **fitting the skill to your data**. Your GT defines "what a good skill should produce given these inputs", and Skill Evolver's loop drives the skill toward that target, the same way SGD drives a model toward the loss minimum.

**What you get is not just a skill that "works" — it's a skill that has been systematically trained on your evaluation dimensions.**

---

## Why Skill Evolver?

**Skills** are the next standard abstraction across Claude Code, Codex CLI, and OpenCode — but **skill iteration is still entirely manual today**. Hand-edit, hand-test, repeat. Not reproducible, not scalable, not auditable.

Three public SOTA projects each solve part of this, but nobody has fused them specifically for skill optimization:

| Pillar | Source | What it gives Evolver |
|---|---|---|
| **Evaluation engine** | [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator) (official Anthropic skill) | Grading, comparison, HTML viewer, test-case design, full eval protocol. **Hard dependency, called by reference** — Creator updates auto-propagate |
| **Autonomous outer loop** | [Karpathy autoresearch](https://github.com/karpathy/autoresearch) → [uditgoenka/autoresearch](https://github.com/uditgoenka/autoresearch) (skill-ified) | The `modify → verify → keep/discard → repeat` 8-phase loop and its 5 principles: one metric / constrained scope / fast verification / automatic rollback / git as memory. **Karpathy originally built it for autonomous optimization of nanochat LLM training code** (700 experiments in 2 days); uditgoenka generalized it into a Claude Code skill |
| **Failure diagnosis philosophy** (inspired by Meta-Harness) | [Stanford Meta-Harness](https://arxiv.org/pdf/2603.28052) (Lee et al. 2026) | Paper quote: *"access to raw execution traces is the key ingredient for enabling harness search."* Table 3 ablation: Scores Only **34.6** → Scores+Summary **34.9** → Full traces **50.0**. **What we borrow**: don't hand the proposer just scores — expose the raw evaluation record (per-case prompt + skill output + per-assertion PASS/FAIL) so diagnosis is grounded in "seeing the scene", not guessing from numbers |

### What Skill Evolver adds on top

1. **5-way AND gate** — quality / trigger F1 / cost / latency / regression must *all* pass to keep. Any fail triggers a real `git revert`
2. **Workspace git isolation** — experiment commits land in an independent git, zero pollution of the project git
3. **Meta-evolution self-proof** — used on itself for 21+ iterations across two sessions (v1: 88.9% → 100%; v2: 126/126 all-green across 49 GT cases, 0 crashes), every iteration surfacing a bug the author couldn't see

---

## 🌍 Run it anywhere

**One source of truth, three platform variants — generated on demand, never out of sync.**

skill-evolver isn't locked to a single agent platform. The full skill lives in [`plugin/skills/skill-evolver/`](plugin/skills/skill-evolver/) — that is the **single source of truth**. Sync scripts (`scripts/sync-codex.sh`, `scripts/sync-opencode.sh`) project that source into platform-specific variants for [Codex](https://developers.openai.com/codex) (`.agents/`) and [OpenCode](https://opencode.ai) (`.opencode/`), applying each platform's quirks (CLI command names, mention syntax, etc.) automatically. The unified [`scripts/install.sh`](scripts/install.sh) wraps both steps into a single command per platform.

| Platform | Command |
|---|---|
| 🤖 **Claude Code** | `bash scripts/install.sh --claude --global` |
| 💻 **Codex CLI** | `bash scripts/install.sh --codex --global` |
| 🌌 **OpenCode** | `bash scripts/install.sh --opencode --global` |
| 🌐 **All three** | `bash scripts/install.sh --all --global` |

The platform mirrors (`.agents/`, `.opencode/`) are **not committed to git** — they are build artifacts, generated by the sync scripts at install time. This keeps the repo lean, eliminates mirror drift, and ensures every install pulls fresh content from `plugin/`. One source. Three platforms. Zero drift.

---

## Quick Start

### 0. Install skill-creator first (hard dependency)

In Claude Code:
```
/install skill-creator
```
Or see [Installing skill-creator](#installing-skill-creator-hard-dependency) below for manual options. skill-evolver refuses to start without it — no silent degradation.

### Claude Code

**Option A — Plugin install (recommended):**
```
/plugin marketplace add serriezhang/skill-evolver
/plugin install skill-evolver
```
Restart Claude Code. `/skill-evolver` is available immediately.

**Option B — Guided installer:**
```bash
git clone https://github.com/serriezhang/skill-evolver.git
cd skill-evolver
bash scripts/install.sh --claude --global
```
Installs to `~/.claude/skills/skill-evolver/`.

**Option C — Manual copy:**
```bash
git clone https://github.com/serriezhang/skill-evolver.git
mkdir -p ~/.claude/skills
cp -R skill-evolver/plugin/skills/skill-evolver ~/.claude/skills/skill-evolver
```

### Codex Quick Start

> **Codex invocation:** use `$skill-evolver` mention syntax. The skill is auto-discovered from `.agents/skills/` directories. The sync step handles Codex-specific replacements (`claude` → `codex`, `/skill-evolver` → `$skill-evolver`, etc.) automatically.

**Option A — Guided installer (recommended):**
```bash
git clone https://github.com/serriezhang/skill-evolver.git
cd skill-evolver
bash scripts/install.sh --codex --global
```
Installs to `~/.agents/skills/skill-evolver/`.

**Option B — Project-local:**
```bash
git clone https://github.com/serriezhang/skill-evolver.git
cd your-project
bash ../skill-evolver/scripts/install.sh --codex --project
```
Installs to `./.agents/skills/skill-evolver/` in the current directory.

### OpenCode Quick Start

> **OpenCode invocation:** use the standard skill loading; command names follow the same conventions as other OpenCode skills. The sync step handles OpenCode-specific adjustments automatically.

**Option A — Guided installer (recommended):**
```bash
git clone https://github.com/serriezhang/skill-evolver.git
cd skill-evolver
bash scripts/install.sh --opencode --global
```
Installs to `~/.config/opencode/skills/skill-evolver/`.

**Option B — Project-local:**
```bash
git clone https://github.com/serriezhang/skill-evolver.git
cd your-project
bash ../skill-evolver/scripts/install.sh --opencode --project
```
Installs to `./.opencode/skills/skill-evolver/` in the current directory.

### Install for all three at once

```bash
bash scripts/install.sh --all --global
```

> **Dry-run first if you want to see exactly what will happen:**
> ```bash
> bash scripts/install.sh --all --global --dry-run
> ```

### Verify the install

```bash
python3 -c "
import sys; sys.path.insert(0, 'plugin/skills/skill-evolver/scripts')
from common import require_creator
print(f'skill-creator: {require_creator()}')
print('skill-evolver: ready')
"
```
If skill-creator is missing, you'll see a clear `CreatorNotFoundError` with three install options.

### Try the Demo

```bash
cd examples/hello-skill && git init && git add -A && git commit -m "init"
python3 ../../plugin/skills/skill-evolver/scripts/evolve_loop.py . --gt evals.json --run --max-iterations 5 --evaluator local
```

See [examples/README.md](examples/README.md) for the full 5-minute walkthrough.

---

## Prerequisites

| Requirement | Required? | Purpose |
|---|---|---|
| **Python 3.10+** | Yes | Runs evaluation scripts |
| **Git** | Yes | Tracks changes in the workspace, enables keep/discard/revert |
| **skill-creator** | **Yes (hard dependency)** | Provides quick_validate, eval-viewer, grader/comparator protocols |
| **Claude Code CLI** | For semantic assertions | LLM binary classification for `path_hit` / `fact_coverage` (program-only assertions work without it) |

### Installing skill-creator (Hard Dependency)

skill-creator is **required**. Without it, Evolver errors out at startup with installation instructions. Install in one of three ways:

1. **Plugin marketplace (recommended):** In Claude Code, run `/install skill-creator`

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

**Path discovery order** (`scripts/common.py:find_creator_path`):
1. `$SKILL_CREATOR_PATH` env var
2. `~/.claude/plugins/marketplaces/*/plugins/skill-creator/skills/skill-creator/`
3. `~/.claude/skills/skill-creator/`
4. `.claude/skills/skill-creator/`
5. `/tmp/anthropic-skills-latest/skills/skill-creator/`

If none of these resolve, `require_creator()` raises `CreatorNotFoundError` with the install message above. There is no silent fallback.

### Without Claude Code CLI

The 6 program-only assertion types (`contains`, `not_contains`, `regex`, `json_schema`, `script_check`, `file_exists`) work without any LLM. Use `--evaluator local`. Only `path_hit` and `fact_coverage` need an LLM backend.

### Multi-Platform Support

| Platform | Install | Notes |
|---|---|---|
| 🤖 **Claude Code** | `bash scripts/install.sh --claude --global` | Source of truth lives in `plugin/skills/skill-evolver/`. Or use `/plugin install`. |
| 💻 **Codex CLI** | `bash scripts/install.sh --codex --global` | Generates `.agents/skills/skill-evolver/` via sync; uses `$skill-evolver` mention syntax. |
| 🌌 **OpenCode** | `bash scripts/install.sh --opencode --global` | Generates `.opencode/skills/skill-evolver/` via sync. |
| 🌐 **All three at once** | `bash scripts/install.sh --all --global` | Runs all three installers in sequence. |
| **HTTP endpoint** | Set `EVOLVER_LLM_URL` env var | Backend-only override. |

> See [Quick Start](#quick-start) above for full per-platform instructions including project-local install (`--project`) and `--dry-run` mode.

Set `LLM_BACKEND=codex` (or `opencode`, `http`) to override auto-detection.

---

## Five Modes

> **Evolve is the reason this tool exists. The other four modes all exist to serve it.**

### ⭐ Evolve (the core)
```bash
/skill-evolver evolve ./my-skill/
```
**Autonomous optimization loop, unattended.** Runs the 8-phase loop until convergence or `max_iterations`. `keep / discard / revert` decisions are actually executed — not just logged. This is the whole point of Skill Evolver; every other mode below exists as scaffolding for this one.

### Eval
```bash
/skill-evolver eval ./my-skill/ --gt ./evals.json
```
One-shot evaluation. Outputs pass rates, per-case breakdown, and optional HTML viewer.

### Create
```bash
/skill-evolver create
```
Generate a new skill from requirements. Creates SKILL.md + workspace + initial GT.

### Improve
```bash
/skill-evolver improve ./my-skill/
```
Human-directed improvement. You decide what to change; Evolver provides trace-based diagnostic evidence and executes.

### Benchmark
```bash
/skill-evolver benchmark ./skill-v1/ ./skill-v2/ --gt ./evals.json
```
A/B comparison of two skill versions. Per-case pass/fail matrix, winner determination.

---

## How It Works

### The 8-Phase Loop

```
Phase 0: Setup    → Create workspace + eval plan + baseline
Phase 1: Review   → Read memory + execution traces (Meta-Harness)
Phase 2: Ideate   → Diagnose failures from traces, propose atomic change
Phase 3: Modify   → Apply ONE change to the skill
Phase 4: Commit   → Git commit (before verification — preserves audit trail)
Phase 5: Verify   → Three-tier evaluation (Quick Gate + Dev Eval + Strict Eval)
Phase 6: Gate     → 5-way AND: quality + trigger + cost + latency + regression
Phase 7: Log      → Write results.tsv + experiments.jsonl + traces
Phase 8: Loop     → Continue, promote layer, or stop
```

### Three-Tier Evaluation (Phase 5 in detail)

Phase 5 is not a single eval pass. It is a three-tier pipeline — cheaper tiers run every iteration, expensive tiers run only when warranted.

**Naming map — L1 / L2 / L3 are the same thing as Quick Gate / Dev Eval / Strict Eval.** The `L*` labels come from the script filenames (`run_l1_gate.py`, `run_l2_eval.py`) and still appear throughout the codebase and protocol. The `Quick Gate / Dev Eval / Strict Eval` names are the conceptual names used in docs. Both names refer to the exact same thing — we just use whichever reads more clearly in context.

| Label | Also called | What it checks | Speed | When it runs | Script |
|---|---|---|---|---|---|
| **L1** | **Quick Gate** | YAML frontmatter syntax, SKILL.md body non-empty, directory structure, Creator's `quick_validate.py`, GT file structure (prompt + assertions present) | **Seconds** | **Every iteration** — the gatekeeper. If it fails, Phase 5 skips directly to discard; Dev Eval is not run. | `scripts/run_l1_gate.py` |
| **L2** | **Dev Eval** | All `dev`-split GT cases graded assertion-by-assertion. 6 program-only types (`contains` / `not_contains` / `regex` / `file_exists` / `json_schema` / `script_check`) are scored by Python code. 2 semantic types (`path_hit` / `fact_coverage`) are scored by `BinaryLLMJudge` — the LLM answers YES/NO only, the program sums up. Result: `pass_rate = passed_assertions / total_assertions`. | **Minutes** | **Every iteration** (or every N, configured by `evolve_plan.md`) — the main signal driving the Phase 6 gate decision. | `scripts/run_l2_eval.py` + `scripts/evaluators.py` |
| **L3** | **Strict Eval** | `holdout` split (overfitting detection — **never shown to the proposer**, Anti-Goodhart principle) + `regression` split (ensures no existing capability was broken) + optional blind A/B comparison via Creator's `agents/comparator.md`. | **~10 minutes** | **Conditional** — triggered by `evolve_plan.md` rules: every N iterations / when dev pass_rate exceeds a threshold / before a layer promotion. Not every iteration. | No dedicated script — Claude orchestrates `run_l2_eval.py` with a different split (`holdout` / `regression`). |

**Fail-fast principle:** if L1 (Quick Gate) fails, the iteration is a discard and Phase 6 runs immediately — L2 (Dev Eval) never runs for a broken skill. This keeps bad iterations cheap.

**Adaptive thresholds:** sample sizes, tier frequencies, focus areas, and pass thresholds are all **per-skill**, not hardcoded. They live in `<workspace>/evolve/evolve_plan.md`, which Claude generates during Phase 0 by analyzing skill type, GT volume, and assertion distribution. A customer-service QA skill gets different thresholds than a code-generation skill. See `references/eval_strategy.md` for the templates.

### Layered Mutation Strategy

| Layer | What changes | Cost | When |
|---|---|---|---|
| Layer 1: Description | Trigger keywords | Low | Default start |
| Layer 2: Body | SKILL.md instructions | Medium | After Layer 1 plateaus |
| Layer 3: Scripts | Helper code, references | High | After Layer 2 plateaus |

**Rule:** One layer at a time. No cross-layer changes. Promote only when stuck.

### Multi-Gate Decision (AND Logic)

Every iteration must pass ALL five gates to be kept:

| Gate | What it checks | Default threshold |
|---|---|---|
| Quality | pass_rate improved | +2% min delta |
| Trigger | trigger F1 not degraded | 5% tolerance |
| Cost | tokens not exploded | 20% max increase |
| Latency | duration not exploded | 20% max increase |
| Regression | existing cases not broken | 5% tolerance |

---

## GT (Ground Truth) Format

GT is the fuel of evolution. No GT = no optimization. (If GT is missing, Evolver auto-generates it from SKILL.md.)

```json
{
  "evals": [
    {
      "id": 1,
      "prompt": "User's input to the skill",
      "assertions": [
        {"type": "contains", "value": "expected text", "description": "what this checks"}
      ],
      "split": "dev",
      "metadata": {}
    }
  ]
}
```

**GT can be in any language** — Chinese, English, Japanese, etc. Assertion matching is language-agnostic.

### 8 Assertion Types

| Type | Who judges | How |
|---|---|---|
| `contains` | Program | Case-insensitive substring match |
| `not_contains` | Program | Must NOT contain text |
| `regex` | Program | Regex pattern match |
| `file_exists` | Program | File exists on disk |
| `json_schema` | Program | Validates against JSON schema |
| `script_check` | Program | External script returns exit code 0 |
| `path_hit` | LLM (YES/NO) | "Does this text reference path X?" |
| `fact_coverage` | LLM (YES/NO per fact) | Checks coverage of key fact points |

**`fact_coverage` supports two modes:**
- **Preset:** GT includes `"facts": ["fact1", "fact2"]` → LLM checks each fact → program counts
- **Online:** No facts array → keyword matching (no LLM needed)

### Data Splits

| Split | Purpose | When used |
|---|---|---|
| `dev` | Optimization target | Every iteration |
| `holdout` | Overfitting detection | Before convergence declaration |
| `regression` | Prevent capability loss | Every gate check |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Layer 3: Meta-Harness                                   │
│  Trace storage · Active diagnosis · Anti-Goodhart        │
├─────────────────────────────────────────────────────────┤
│  Layer 2: Creator                                        │
│  Binary LLM + program scoring · 8 assertion types        │
│  GT auto-construction · Eval viewer · Trigger eval       │
├─────────────────────────────────────────────────────────┤
│  Layer 1: AutoResearch                                   │
│  Autonomous loop · Multi-gate AND · Structured memory    │
│  Stuck detection · Layer promotion · Git-based rollback  │
└─────────────────────────────────────────────────────────┘
```

### Universal Evaluator System

Not tied to any specific creator. Four built-in evaluators:

| Evaluator | Use case | Command |
|---|---|---|
| `local` | Built-in assertion matching — deterministic, no subprocess, no LLM | `--evaluator local` |
| `creator` | Wraps `local` + additionally calls Creator's `run_eval.py` for trigger F1 (default when no `--evaluator` is passed) | `--evaluator creator` |
| `script` | Your own eval script | `--evaluator script --evaluator-script ./my_eval.py` |
| `pytest` | Standard test framework | `--evaluator pytest --evaluator-test-cmd "pytest tests/"` |

**Note on "lazy imports"**: `import evaluators` itself never loads the
three non-default backends (`evaluator_backends.py` stays absent from
`sys.modules`). The factory `get_evaluator` lazy-imports the specific
backend class only when that backend is actually requested. Because
the factory's built-in default is `creator`, the first call to
`get_evaluator()` with no `evaluator` key will lazy-load
`CreatorEvaluator` (which in turn holds a `LocalEvaluator` fallback
internally). To skip the lazy load entirely, pass `--evaluator local`
on the CLI or set `evaluator: local` in `evolve_plan.md` — that path
touches only `LocalEvaluator` and never enters `evaluator_backends.py`.

---

## Workspace Structure

Evolver stores zero skill-specific data in its own directory. Everything lives alongside the target skill — at the **same level**, not nested inside:

```
your-skill/                     # Your skill (git-managed)
├── SKILL.md
├── references/
└── scripts/

your-skill-workspace/           # Sibling directory, shared by Creator + Evolver
├── evals/
│   ├── evals.json              # GT data (test cases + assertions)
│   └── checks/                 # GT-referenced script_check helpers
│       └── check_*.py          # canonical home per eval_strategy.md
└── evolve/                     # Evolver-specific subdirectory
    ├── evolve_plan.md          # Adaptive eval strategy (auto-generated)
    ├── results.tsv             # Experiment log (1 row per iteration)
    ├── experiments.jsonl       # Fine-grained per-case memory + diagnoses
    ├── best_versions/          # Snapshots of best skill versions (top 3)
    ├── iteration-E*/           # Per-iteration artifacts (meta.json + cases/)
    └── review.html             # HTML eval viewer (if Creator available)
```

---

## Configuration

Gate thresholds are defined per-skill in `evolve_plan.md`:

```
min_delta: 0.02
trigger_tolerance: 0.05
max_token_increase: 0.20
max_latency_increase: 0.20
regression_tolerance: 0.05
max_iterations: 20
stuck_threshold: 5
```

### CLI Reference

```bash
# Full evolve loop
python3 scripts/evolve_loop.py <skill-path> --gt <evals.json> --run [--max-iterations N]

# Preview the first-iteration mutation without committing (safe dry-run)
python3 scripts/evolve_loop.py <skill-path> --gt <evals.json> --dry-run

# Setup only (no loop)
python3 scripts/evolve_loop.py <skill-path> --gt <evals.json>

# A/B benchmark
python3 scripts/aggregate_results.py --benchmark <skill-a> <skill-b> --gt <evals.json>

# Cleanup
python3 scripts/evolve_loop.py <skill-path> --cleanup
python3 scripts/evolve_loop.py <skill-path> --cleanup-versions
```

### Conversation interface (primary mode — no CLI needed)

The CLI is a fallback for unattended / CI runs. The **primary** user
interface is natural-language requests inside Claude Code (or Codex,
OpenCode). The skill's `description` field triggers on phrases like:

| User says                                         | What Claude does                  |
|---|---|
| "Optimize this skill" or "optimize my skill at X" | Enter evolve mode on that path    |
| "帮我优化这个 skill" / "帮我优化 xxx skill"        | Enter evolve mode (Chinese)       |
| "Use skill-evolver to tune `./foo`"               | Enter evolve mode on that path    |
| "/skill-evolver evolve ./my-skill" or "/evolve"   | Enter evolve mode explicitly      |
| "Evaluate this skill, don't change anything"      | Run eval mode only                |
| "Compare `./v1` and `./v2`"                       | Run benchmark mode                |
| "Show me what the first iteration would change"   | Run evolve with `--dry-run`       |

Claude then executes the 8-Phase loop **directly in the conversation**
— reading memory, diagnosing failures with Meta-Harness traces,
making atomic edits with the Edit tool, committing, gating, logging.
End users never see a CLI command; Claude handles all the mechanics
internally. See `plugin/skills/skill-evolver/SKILL.md` **How the user
invokes it** section for the full pattern list.

### Auto-persisting per-case traces (in-conversation mode)

Meta-Harness diagnosis requires per-case JSON files at
`<workspace>/evolve/iteration-E{N}/cases/case_*.json`. For
in-conversation callers, `LocalEvaluator.full_eval` accepts an
optional `cases_dir` kwarg that auto-writes the structured traces so
the next iteration's Phase 1/2 has evidence to grep/cat without
calling `persist_cases` separately:

```python
from evaluators import LocalEvaluator
from pathlib import Path
r = LocalEvaluator().full_eval(
    skill_path,
    gt_path,
    split='dev',
    cases_dir=str(workspace / 'evolve' / f'iteration-E{N}' / 'cases'),
)
# case_001.json, case_002.json, ... auto-written to cases_dir
```

---

## Repo Structure

```
skill-evolver/
├── plugin/skills/skill-evolver/       # The actual skill (source of truth)
│   ├── SKILL.md                       # Main entry point
│   ├── references/                    # Protocol documents
│   ├── agents/                        # Agent protocols
│   └── scripts/                       # 13 single-purpose Python files
├── examples/hello-skill/              # 5-minute demo
├── docs/
│   ├── architecture.md                # Technical architecture (Chinese)
│   ├── architecture.en.md             # Technical architecture (English)
│   └── README_CN.md                   # Chinese README
│   # Note: docs/private/ exists locally (self-iteration reports, presentation
│   #       drafts, WeChat article drafts) but is gitignored and not shipped.
├── scripts/                           # Build, sync, and install scripts
│   ├── install.sh                     # Unified installer (--claude/--codex/--opencode/--all)
│   ├── sync-codex.sh                  # Generates .agents/ from plugin/
│   ├── sync-opencode.sh               # Generates .opencode/ from plugin/
│   └── sync-all.sh                    # Runs both sync scripts
├── README.md
└── LICENSE                            # MIT
```

### scripts/ layout (15 single-purpose files)

After the iter-15 through iter-19 refactor, `plugin/skills/skill-evolver/scripts/` is split by concern. Every file owns one thing; `from evolve_loop import X` still works for back-compat via top-level re-exports and PEP 562 `__getattr__`.

| File | Owns |
|---|---|
| `evolve_loop.py` | Phase functions 0 / 1 / 4 / 5 / 7 / 8 + git helpers + `persist_cases` / `write_cases_to_dir` + CLI entry (delegates to `orchestrator.main`) |
| `orchestrator.py` | `run_evolve_loop` (the 8-Phase driver) + `main` (argparse + dispatch) + `_eval_holdout_or_none` |
| `gate.py` | `phase_6_gate_decision` — pure function, stdlib-only |
| `llm.py` | `LLM_BACKENDS` registry + `_call_llm` / `_call_llm_http` + `phase_2_3_ideate_and_modify` + `run_l2_eval_via_claude` + `auto_construct_gt` |
| `cleanup.py` | `_iter_num` + `cleanup_best_versions` + `cleanup_eval_outputs` + `_try_launch_eval_viewer` |
| `evaluators.py` | `Evaluator` ABC + `LocalEvaluator` + `get_evaluator` factory (lazy-imports backends) + back-compat re-exports |
| `evaluator_backends.py` | `CreatorEvaluator` + `ScriptEvaluator` + `PytestEvaluator` (lazy-loaded only when config requests them) |
| `trace_enrichment.py` | Per-assertion rich field helpers: `locate_in_corpus` / `nearest_match` / `check_script_rich` / `check_fact_coverage_rich` / `check_json_schema_rich` |
| `binary_judge.py` | `BinaryLLMJudge` — atomic YES/NO LLM calls with `judge_with_reasoning` rationale capture |
| `common.py` | Python version gate + Creator path discovery + `find_workspace` + `parse_skill_md` |
| `aggregate_results.py` | `parse_results_tsv` + `run_benchmark` A/B + markdown report formatter |
| `run_l1_gate.py` | L1 quick-gate helper + P0 quality rules (SEC001-006, S003+, TD011, C001, C005) with code-markup stripping |
| `run_l2_eval.py` | L2 eval library helpers |
| `setup_workspace.py` | Workspace bootstrap + evolve_plan.md template generation |
| `__init__.py` | Package marker |

See `plugin/skills/skill-evolver/SKILL.md` **Code Organization** section for the full import-graph and cycle-breaker design (PEP 562 `__getattr__` + lazy factory imports).

---

## Technical Documentation

| Document | Language | Content |
|---|---|---|
| [Architecture](docs/architecture.en.md) | English | Full technical design, 4-layer architecture, Creator hard-dependency model, scripts/ layout |
| [Architecture](docs/architecture.md) | Chinese | Same content, Chinese version |
| [README_CN](docs/README_CN.md) | Chinese | Full Chinese README |

> **Private notes**: self-iteration reports, design-decision comparison, presentation drafts,
> and WeChat article drafts live in `docs/private/` locally. They are `.gitignore`-excluded and
> not published to GitHub — available on request for collaborators.

---

## Contributing

1. Fork the repo
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Run the self-eval to verify nothing breaks:
   ```bash
   python3 -c "
   import sys; sys.path.insert(0, 'plugin/skills/skill-evolver/scripts')
   from evaluators import LocalEvaluator; from pathlib import Path
   e = LocalEvaluator()
   for s in ['dev','holdout','regression']:
       r = e.full_eval(Path('plugin/skills/skill-evolver'), Path('.claude/skills/skill-evolver-workspace/evals/evals.json'), s)
       print(f'{s}: {r[\"pass_rate\"]:.0%}')
   "
   ```
4. Commit your changes
5. Open a Pull Request

---

## License

[MIT](LICENSE)

---

## Acknowledgments

- **[skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator)** by Anthropic — the evaluation engine. Hard dependency, called by reference, never copied. Creator updates flow into Evolver automatically.
- **[AutoResearch](https://github.com/uditgoenka/autoresearch)** — Karpathy-inspired autonomous iteration loop that became Evolver's 8-phase outer loop with real keep/discard/revert (not "edit and forget")
- **Meta-Harness** — execution-trace-based active diagnosis pattern from Meta's agent-optimization work. Every iteration must cite specific trace evidence from `iteration-E{N}/cases/case_*.json` before proposing a change. Auto-persisted by `LocalEvaluator.full_eval(..., cases_dir=...)`.
- **ServiceClaw QA V2** — "LLM classifies, program scores" evaluation philosophy
- Built for the [Claude Code](https://claude.com/claude-code) ecosystem
