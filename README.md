[English](README.md) | [中文](docs/README_CN.md) | [Architecture](docs/architecture.en.md)

# Skill Evolver

**Automatic evolution engine for AI coding agent skills.** Give it a skill + test data, and it iteratively optimizes the skill through gated evaluation loops — no human in the loop required.

```
          ┌─────────────┐
          │  Your Skill  │
          └──────┬───────┘
                 │
    ┌────────────▼────────────┐
    │     Skill Evolver       │
    │                         │
    │  Search → Modify →      │
    │  Evaluate → Gate →      │
    │  Keep/Discard → Repeat  │
    └────────────┬────────────┘
                 │
          ┌──────▼───────┐
          │  Better Skill │
          └──────────────┘
```

---

## Design Philosophy — Three Pillars

Skill Evolver fuses three state-of-the-art ideas. Each is a hard requirement, not an option:

| Pillar | Source | What it provides | How Evolver uses it |
|---|---|---|---|
| **Creator** (core capability) | [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator) — official Anthropic skill | Evaluation, grading, comparison protocols; HTML eval viewer; quick validation | **Hard dependency.** `require_creator()` resolves Creator at startup or errors out. Grading reads Creator's `agents/grader.md` at runtime — no copies kept in Evolver. Creator updates take effect automatically. |
| **AutoResearch** (loop methodology) | [AutoResearch](https://github.com/uditgoenka/autoresearch) — Karpathy's autonomous iteration pattern | The 8-phase outer loop: search → modify → verify → gate → keep/discard → repeat | Drives Evolver's `evolve_loop.py`. Each iteration is an atomic experiment with multi-gate AND decision and git-based rollback. Real keep/discard/revert — not "edit and forget". |
| **Meta-Harness** (diagnosis) | Meta's Trace pattern for LLM agent optimization | Store full execution traces per case; cite trace evidence before proposing any fix; counterfactual diagnosis | Every evaluation writes per-case traces to `iteration-E{N}/traces/`. Phase 1 reads them, Phase 2 enforces a mandatory active diagnosis protocol — the search agent must cite specific trace evidence before any mutation. |

**Core evaluation principle:** LLM only makes atomic YES/NO judgments. Programs compute all scores. Same classification always produces the same score — zero scoring drift.

**No silent degradation:** Evolver does not contain "fallback copies" of Creator's grader/comparator. The pointer files in `agents/` redirect to Creator's full versions at runtime. If Creator updates its protocol, Evolver picks up the change on the next run.

---

## Quick Start

### 1. Install skill-creator first (hard dependency)

In Claude Code:
```
/install skill-creator
```
Or see [Installing skill-creator](#installing-skill-creator-hard-dependency) below for manual options.

### 2. Install skill-evolver

**Option A: Claude Code Plugin (Recommended)**
```bash
cd ~/.claude/plugins
git clone https://github.com/serriezhang/skill-evolver.git
```
Restart Claude Code. `skill-evolver` appears in the skill list automatically.

**Option B: Manual**
```bash
mkdir -p ~/.claude/skills/skill-evolver
cp -R plugin/skills/skill-evolver/* ~/.claude/skills/skill-evolver/
```

**Option C: Codex or OpenCode users**

The `.agents/` (Codex) and `.opencode/` (OpenCode) platform variants are
not checked into git — they are generated on demand from `plugin/` by the
sync scripts. After cloning the repo, run once:
```bash
bash scripts/sync-codex.sh      # for Codex → creates .agents/skills/skill-evolver/
bash scripts/sync-opencode.sh   # for OpenCode → creates .opencode/skills/skill-evolver/
# or sync all at once:
bash scripts/sync-all.sh
```
Re-run the relevant sync script whenever you pull new changes. This keeps
each platform's SKILL.md specialized (Codex-specific CLI names, etc.)
without the repo carrying three duplicated copies of the same source.

### 3. Verify both are installed

```bash
python3 -c "
import sys; sys.path.insert(0, 'plugin/skills/skill-evolver/scripts')
from common import require_creator
print(f'skill-creator: {require_creator()}')
print('skill-evolver: ready')
"
```
If skill-creator is missing, you'll see a clear `CreatorNotFoundError` with three install options.

### 4. Try the Demo

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

| Platform | Status | How |
|---|---|---|
| **Claude Code** | Full support | `plugin/skills/skill-evolver/` (source of truth) |
| **OpenCode** | Full support | `.opencode/skills/skill-evolver/` — run `bash scripts/sync-opencode.sh` |
| **Codex** | Full support | `.agents/skills/skill-evolver/` — run `bash scripts/sync-codex.sh` |
| **HTTP endpoint** | Supported | Set `EVOLVER_LLM_URL` env var |

> The Codex and OpenCode variants are **generated on demand** from `plugin/`
> by the sync scripts, not checked into git. Run the relevant sync script
> once after cloning (and again after pulling updates).

Set `LLM_BACKEND=codex` (or `opencode`, `http`) to override auto-detection.

---

## Five Modes

### Evolve (Core)
```bash
/skill-evolver evolve ./my-skill/
```
Autonomous optimization loop. Runs 8 phases per iteration until convergence or max iterations. No human intervention needed.

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
| `local` | Built-in assertion matching | `--evaluator local` |
| `creator` | Enhances with trigger eval | `--evaluator creator` (default) |
| `script` | Your own eval script | `--evaluator script --evaluator-script ./my_eval.py` |
| `pytest` | Standard test framework | `--evaluator pytest --evaluator-test-cmd "pytest tests/"` |

---

## Workspace Structure

Evolver stores zero skill-specific data in its own directory. Everything lives in the target skill's workspace:

```
my-skill-workspace/
├── evals/
│   └── evals.json              # GT data
└── evolve/
    ├── evolve_plan.md          # Adaptive eval strategy (auto-generated)
    ├── results.tsv             # Experiment log (1 row per iteration)
    ├── experiments.jsonl       # Fine-grained per-case memory + diagnoses
    ├── best_versions/          # Snapshots of best skill versions
    ├── iteration-E1/
    │   ├── grading.json        # Evaluation results
    │   └── traces/             # Full execution traces (Meta-Harness)
    │       ├── case_1.md
    │       └── case_2.md
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

# Setup only (no loop)
python3 scripts/evolve_loop.py <skill-path> --gt <evals.json>

# A/B benchmark
python3 scripts/aggregate_results.py --benchmark <skill-a> <skill-b> --gt <evals.json>

# Cleanup
python3 scripts/evolve_loop.py <skill-path> --cleanup
python3 scripts/evolve_loop.py <skill-path> --cleanup-versions
```

---

## Repo Structure

```
skill-evolver/
├── plugin/skills/skill-evolver/    # The actual skill (source of truth)
│   ├── SKILL.md                    # Main entry point
│   ├── references/                 # Protocol documents
│   ├── agents/                     # Agent protocols
│   └── scripts/                    # Python scripts
├── examples/hello-skill/           # 5-minute demo
├── docs/
│   ├── architecture.md             # Technical architecture (Chinese)
│   ├── architecture.en.md          # Technical architecture (English)
│   └── README_CN.md                # Chinese README
├── scripts/                        # Build & sync scripts
│   ├── sync-codex.sh               # Generates .agents/ from plugin/
│   ├── sync-opencode.sh            # Generates .opencode/ from plugin/
│   └── sync-all.sh                 # Runs both
├── README.md
└── LICENSE                         # MIT
```

---

## Technical Documentation

| Document | Language | Content |
|---|---|---|
| [Architecture](docs/architecture.en.md) | English | Full technical design, 4-layer architecture, Creator hard-dependency model |
| [Architecture](docs/architecture.md) | Chinese | Same content, Chinese version |

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
- **[Meta-Harness](https://arxiv.org/abs/2506.xxxxx)** — execution-trace-based active diagnosis. Every iteration must cite specific trace evidence before proposing a change.
- **ServiceClaw QA V2** — "LLM classifies, program scores" evaluation philosophy
- Built for the [Claude Code](https://claude.com/claude-code) ecosystem
