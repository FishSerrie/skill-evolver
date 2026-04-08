[English](README.md) | [中文](README_CN.md) | [技术架构](docs/architecture-v2.1.md)

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

## Design Philosophy

Skill Evolver combines three state-of-the-art ideas:

| Layer | Idea | What it does |
|---|---|---|
| **AutoResearch** | Karpathy's autonomous iteration | Self-driving loop: modify → verify → keep/discard → repeat |
| **Creator** | Skill-creator's evaluation framework | GT-based testing with 8 assertion types. LLM does binary YES/NO classification; programs do all scoring |
| **Meta-Harness** | Full execution trace diagnosis | Store traces, grep before guessing, counterfactual analysis |

**Core evaluation principle:** LLM only makes atomic YES/NO judgments. Programs compute all scores. Same classification always produces the same score. ([Why?](docs/comparison-analysis.md))

---

## Quick Start

### 1. Install

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

### 2. Verify Installation

In Claude Code, type:
```
/skill-evolver
```
You should see the mode selection prompt.

### 3. Try the Demo

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
| **Git** | Yes | Tracks changes, enables keep/discard/revert |
| **Claude Code CLI** | For LLM features | Semantic assertions (path_hit, fact_coverage) and the evolve proposer |
| **skill-creator** | Optional | Adds trigger evaluation and HTML eval viewer |

### Without Claude Code CLI

The 6 program-only assertion types (contains, regex, json_schema, script_check, file_exists, not_contains) work without any LLM. Use `--evaluator local`.

### Without skill-creator

The full evolve loop works. You miss:
- Trigger F1 evaluation (`scripts/run_eval.py`)
- HTML eval viewer (`eval-viewer/generate_review.py`)

On first use, Skill Evolver auto-detects any installed creator-like tool by scanning SKILL.md descriptions for evaluation keywords. It finds skill-creator, claw-creator, or any custom evaluator automatically.

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
│   ├── architecture-v2.1.md        # Technical architecture (Chinese)
│   ├── architecture-v2.1.en.md     # Technical architecture (English)
│   ├── comparison-analysis.md      # vs AutoResearch, Meta-Harness
│   └── bootstrap-report.md         # Self-evolution test results
├── scripts/                        # Build & sync scripts
├── README.md
├── README_CN.md
└── LICENSE                         # MIT
```

---

## Technical Documentation

| Document | Language | Content |
|---|---|---|
| [Architecture v2.1](docs/architecture-v2.1.en.md) | English | Full technical design, 4-layer architecture |
| [Architecture v2.1](docs/architecture-v2.1.md) | Chinese | Same content, Chinese version |
| [Comparison Analysis](docs/comparison-analysis.md) | Chinese | vs AutoResearch, Meta-Harness, ServiceClaw |
| [Bootstrap Report](docs/bootstrap-report.md) | English | Self-evolution test: 50% → 100% |

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

- **[AutoResearch](https://github.com/uditgoenka/autoresearch)** — the autonomous iteration pattern (Karpathy's idea) that inspired the outer loop
- **[skill-creator](https://github.com/anthropics/claude-plugins-official/tree/main/plugins/skill-creator)** by Anthropic — evaluation engine that powers scoring
- **[Meta-Harness](https://arxiv.org/abs/2506.xxxxx)** — execution trace diagnosis that inspired the active diagnosis protocol
- **ServiceClaw QA V2** — "LLM classifies, program scores" evaluation philosophy
- Built for the [Claude Code](https://claude.com/claude-code) ecosystem
