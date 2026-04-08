[English](README.md) | [中文](README_CN.md) | [Architecture](docs/architecture.en.md)

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
| **Claude Code** | Full support | `plugin/skills/skill-evolver/` |
| **OpenCode** | Full support | `.opencode/skills/skill-evolver/` (auto-synced) |
| **Codex** | Full support | `.agents/skills/skill-evolver/` (auto-synced) |
| **HTTP endpoint** | Supported | Set `EVOLVER_LLM_URL` env var |

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
Phase 5: Verify   → Quick gate (seconds) + full eval (minutes)
Phase 6: Gate     → 5-way AND: quality + trigger + cost + latency + regression
Phase 7: Log      → Write results.tsv + experiments.jsonl + traces
Phase 8: Loop     → Continue, promote layer, or stop
```

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
├── plugin/skills/skill-evolver/    # The actual skill (Claude Code loads this)
│   ├── SKILL.md                    # Main entry point
│   ├── references/                 # 6 protocol documents
│   ├── agents/                     # 4 agent protocols
│   └── scripts/                    # 8 Python scripts
├── .opencode/skills/skill-evolver/ # OpenCode variant (auto-synced)
├── .agents/skills/skill-evolver/   # Codex variant (auto-synced)
├── examples/hello-skill/           # 5-minute demo
├── docs/
│   ├── architecture.md             # Technical architecture (Chinese)
│   └── architecture.en.md          # Technical architecture (English)
├── scripts/                        # Build & sync scripts
├── README.md
├── README_CN.md
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
