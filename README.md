# Skill Evolver

**Automatic evolution engine for Claude Code skills.** Give it a skill + test data, and it will iteratively optimize the skill through gated evaluation loops — no human in the loop required.

Built on top of [skill-creator](https://github.com/anthropics/claude-plugins-official/tree/main/plugins/skill-creator) for evaluation and [AutoResearch](https://github.com/thedotmack/claude-mem) for autonomous iteration patterns.

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

## Why Skill Evolver?

**skill-creator** lets you create and evaluate skills — but improvement is manual. You look at eval results, decide what to change, edit, re-evaluate, repeat.

**skill-evolver** automates this entire outer loop:

| | skill-creator | skill-evolver |
|---|---|---|
| Create a skill | Yes | Yes (calls creator) |
| Evaluate a skill | Yes | Yes (calls creator) |
| Improve a skill | Manual | **Automatic** |
| Multi-gate quality control | No | **Yes** |
| Experiment memory | No | **Yes** |
| A/B benchmark | Yes | Yes (calls creator) |
| Autonomous evolution loop | No | **Yes (core value)** |

**Evolver calls Creator, never copies it.** When Creator gets updated, Evolver benefits automatically.

### Universal Architecture

Skill Evolver is a **universal optimization shell** — it is not hard-coupled to any specific creator or LLM provider. The pluggable evaluator system supports:

| Evaluator | Use Case |
|---|---|
| `local` | Built-in assertion matching (no LLM needed for basic types) |
| `creator` | Enhances local eval with skill-creator's trigger testing |
| `script` | Your own evaluation script (any language, any logic) |
| `pytest` | Standard test framework integration |

If you use a different creator (e.g., `claw-creator`), just point the evaluator to your own script:
```bash
python3 scripts/evolve_loop.py ./my-skill/ --gt evals.json --run --evaluator script --evaluator-script ./my_eval.py
```

### Evaluation Philosophy

**LLM does binary classification; programs do scoring.** Deterministic assertions (contains, regex, json_schema) use zero LLM calls. Semantic assertions (fact_coverage, path_hit) use atomic YES/NO LLM calls. Programs aggregate all results. Same classification always produces the same score.

---

## Quick Start

> **Try the 5-minute demo:** See [examples/README.md](examples/README.md) for a self-contained walkthrough.

### 1. Auto-evolve an existing skill (core feature)

```bash
# One command, full loop — runs until convergence or max iterations
/skill-evolver evolve ./my-skill/
```

Or via CLI for unattended execution:

```bash
python3 scripts/evolve_loop.py ./my-skill/ --gt ./evals.json --run --max-iterations 20
```

### 2. Evaluate a skill

```bash
/skill-evolver eval ./my-skill/ --gt ./evals.json
```

### 3. Create a new skill from scratch

```bash
/skill-evolver create
```

### 4. Compare two versions

```bash
/skill-evolver benchmark ./skill-v1/ ./skill-v2/ --gt ./evals.json
```

---

## How It Works

### The Evolution Loop (8 Phases)

```
Phase 0: Setup    →  Create workspace + generate eval plan + establish baseline
Phase 1: Review   →  Read memory (results.tsv + experiments.jsonl + git log)
Phase 2: Ideate   →  Analyze failure patterns, decide what to change
Phase 3: Modify   →  Make ONE atomic change to the skill
Phase 4: Commit   →  Git commit the change
Phase 5: Verify   →  Run Quick Gate (seconds) + Dev Eval (minutes)
Phase 6: Gate     →  Multi-gate decision: keep / discard / revert
Phase 7: Log      →  Record results to memory
Phase 8: Loop     →  Continue, escalate layer, or stop
```

### Four-Layer Architecture

```
┌──────────────────────────────────────────────┐
│  Layer 4: Search (AutoResearch outer loop)    │
│  Decides WHAT to change and HOW              │
├──────────────────────────────────────────────┤
│  Layer 3: Gate (multi-gate decisions)         │
│  AND logic: quality + trigger + cost +        │
│  latency + regression must ALL pass           │
├──────────────────────────────────────────────┤
│  Layer 2: Eval (adaptive evaluation)          │
│  Quick Gate → Dev Eval → Strict Eval          │
│  Strategy defined per-skill in evolve_plan    │
├──────────────────────────────────────────────┤
│  Layer 1: Memory (structured experiment log)  │
│  results.tsv + experiments.jsonl +            │
│  git history + best_versions snapshots        │
└──────────────────────────────────────────────┘
```

### Layered Mutation Strategy

Evolver changes skills incrementally, one layer at a time:

| Layer | Target | Cost | Example |
|---|---|---|---|
| Layer 1 | `description` field | Low | Improve trigger accuracy (F1) |
| Layer 2 | SKILL.md body | Medium | Better instructions, examples, constraints |
| Layer 3 | scripts/ & references/ | High | New helper scripts, refined protocols |

**Rule: only escalate to the next layer when the current layer plateaus.** No cross-layer changes allowed in a single iteration.

---

## Five Modes

| Mode | Command | What it does | Calls Creator? |
|---|---|---|---|
| **Create** | `/skill-evolver create` | Generate a new skill from requirements + GT | Yes |
| **Eval** | `/skill-evolver eval` | One-shot evaluation with benchmark report | Yes |
| **Improve** | `/skill-evolver improve` | Human-directed targeted improvement | Yes |
| **Benchmark** | `/skill-evolver benchmark` | A/B comparison with blind grading | Yes |
| **Evolve** | `/skill-evolver evolve` | **Autonomous optimization loop** | Partially |

Chain modes with pipeline:

```bash
/skill-evolver pipeline ./my-skill/ --mode create+eval+evolve
```

---

## GT (Ground Truth) Data Format

GT is the fuel of evolution. Without GT, no optimization starts.

```json
{
  "id": 1,
  "prompt": "User's input to the skill",
  "assertions": [
    {"type": "contains", "value": "expected output", "description": "Must include X"}
  ],
  "split": "dev",
  "metadata": {}
}
```

### Assertion Types

| Type | Description |
|---|---|
| `contains` | Output must contain the text |
| `not_contains` | Output must NOT contain the text |
| `regex` | Output matches regex pattern |
| `path_hit` | References the correct doc path |
| `fact_coverage` | Covers specified key facts |
| `script_check` | Custom script returns pass/fail |
| `json_schema` | Output conforms to JSON schema |
| `file_exists` | Expected file was generated |

### Data Splits

Every GT case must have a `split` field:

| Split | Purpose | When used |
|---|---|---|
| `dev` | Primary optimization target | Every iteration |
| `holdout` | Overfitting detection | Periodically + final |
| `regression` | Prevent capability loss | Every gate check |

**No GT data?** Evolver will call skill-creator's test case generation to bootstrap GT automatically.

---

## Workspace Structure

Evolver stores **zero** skill-specific data in its own directory. Everything lives in the target skill's workspace:

```
your-project/
├── my-skill/                       # Your skill (git-managed)
│   ├── SKILL.md
│   ├── references/
│   └── scripts/
└── my-skill-workspace/             # Shared workspace (Creator + Evolver)
    ├── evals/
    │   └── evals.json              # GT data
    ├── iteration-1/                # Creator's eval iterations
    ├── iteration-2/
    └── evolve/                     # Evolver-specific data
        ├── evolve_plan.md          # Adaptive eval strategy
        ├── results.tsv             # Experiment log (1 row per iteration)
        ├── experiments.jsonl       # Fine-grained per-case memory
        ├── best_versions/          # Snapshots of best skill versions
        ├── iteration-E1/           # Evolve eval artifacts (E-prefix)
        │   ├── grading.json
        │   ├── benchmark.json
        │   ├── timing.json
        │   └── traces/             # Full execution traces (Meta-Harness)
        │       ├── case_1.md
        │       └── case_2.md
        └── summary.md              # Final evolution report
```

---

## Installation

### Prerequisites

- Python 3.10+
- Git
- [Claude Code](https://claude.com/claude-code) CLI installed (for LLM-based semantic assertions and the evolve proposer)

### Optional: skill-creator

skill-creator enhances evaluation with trigger testing and blind A/B comparison. The core evolve loop works without it (using `--evaluator local`).

Install from: [claude-plugins-official](https://github.com/anthropics/claude-plugins-official)

Without Creator, you can still use:
- All 6 program-only assertion types (contains, regex, json_schema, script_check, etc.)
- LLM binary assertions (path_hit, fact_coverage) via BinaryLLMJudge
- The full 8-phase evolve loop
- Script and pytest evaluators for custom evaluation logic
- [skill-creator](https://github.com/anthropics/claude-plugins-official/tree/main/plugins/skill-creator) plugin installed (comes with Claude Code by default)
- Python 3.10+
- Git (recommended for version tracking)

### Option A: Install as Claude Code Plugin (Recommended)

```bash
# Clone the repo directly into your plugins directory
cd ~/.claude/plugins
git clone https://github.com/serriezhang/skill-evolver.git
```

That's it. Restart Claude Code and `skill-evolver` will appear in the skill list.

**Why this works**: The repo contains `.claude-plugin/marketplace.json` at the root, which tells Claude Code to look inside `./plugin/` for the actual skill files. This is the same pattern used by [claude-mem](https://github.com/thedotmack/claude-mem).

### Option B: Install to a Custom Marketplace

If you maintain your own marketplace:

```bash
# Add as a git submodule or copy to your marketplace
cd ~/.claude/plugins/marketplaces/<your-marketplace>/plugins
git clone https://github.com/serriezhang/skill-evolver.git

# Then add to your marketplace's .claude-plugin/marketplace.json:
```

```json
{
  "name": "skill-evolver",
  "description": "Skill auto-evolution engine",
  "source": "./plugins/skill-evolver/plugin",
  "category": "development"
}
```

### Option C: Manual (Slash Command Only)

```bash
# Copy just the skill files
mkdir -p ~/.claude/skills/skill-evolver
cp -R plugin/skills/skill-evolver/* ~/.claude/skills/skill-evolver/
```

Then invoke via slash command: `/skill-evolver evolve ./my-skill/`

> **Note**: Option C registers as a slash command only, not as a Skill tool target.

---

## Repo Structure

```
skill-evolver/                              # GitHub repo root
├── .claude-plugin/
│   ├── marketplace.json                    # Plugin registry (source → ./plugin)
│   └── plugin.json                         # Root identity
├── plugin/                                 # What Claude Code actually loads
│   ├── .claude-plugin/
│   │   └── plugin.json                     # Plugin identity
│   └── skills/
│       └── skill-evolver/
│           ├── SKILL.md                    # Main skill entry point (~320 lines)
│           ├── references/
│           │   ├── evolve_protocol.md      # 8-phase evolution protocol
│           │   ├── eval_strategy.md        # Adaptive evaluation templates
│           │   ├── gate_rules.md           # Multi-gate rules & pseudocode
│           │   ├── mutation_policy.md      # Layered mutation strategy
│           │   ├── memory_schema.md        # results.tsv + experiments.jsonl schema
│           │   └── creator_integration.md  # How Evolver calls Creator
│           ├── agents/
│           │   ├── search_agent.md         # Variant generation protocol
│           │   ├── analyzer_agent.md       # Attribution analysis
│           │   ├── grader_agent.md         # Scoring (references Creator)
│           │   └── comparator_agent.md     # Blind A/B comparison
│           └── scripts/
│               ├── evolve_loop.py          # Main 8-phase orchestrator (~520 lines)
│               ├── setup_workspace.py      # Workspace initialization
│               ├── run_l1_gate.py          # Quick gate (YAML + trigger check)
│               ├── run_l2_eval.py          # Dev eval helper functions
│               ├── aggregate_results.py    # Statistical aggregation
│               └── common.py              # Shared utilities
├── docs/                                   # Human-readable documentation
│   ├── architecture-v2.1.md                # Full technical architecture
│   └── bootstrap-report.md                 # Self-evolution test results
├── .opencode/                              # OpenCode platform variant (auto-generated)
│   └── skills/skill-evolver/               # Same skill, platform-adapted
├── .agents/                                # Codex platform variant (auto-generated)
│   └── skills/skill-evolver/               # Same skill, platform-adapted
├── scripts/                                # Build & sync scripts
│   ├── sync-opencode.sh                    # Claude → OpenCode sync
│   ├── sync-codex.sh                       # Claude → Codex sync
│   └── sync-all.sh                         # Sync all platforms
├── README.md
├── README_CN.md                            # Chinese documentation
└── LICENSE
```

Total: **18 files, ~2700 lines**

---

## Example: Evolving a Skill from 50% to 100%

This is a real result from skill-evolver's bootstrap test (evolving itself):

```
Baseline: 50% (9/18 assertions passing)

Iteration 1:
  - Identified: Evolve execution instructions still referenced old manual bash workflow
  - Change: Replaced 27 lines of manual bash with 11 lines of auto-run instructions
  - Result: 100% (18/18) ← +50% improvement
  - Gate: KEEP ✅
  - Git: +11 -27 lines (net -16)

Iteration 2:
  - All assertions passing, no more failure patterns
  - Decision: STOP (exhausted)

Final: 50% → 100% in 2 iterations, net reduction of 16 lines
```

Full bootstrap report: [skill-evolver-bootstrap-report.md](./skill-evolver-bootstrap-report.md)

---

## Configuration

### Gate Thresholds

Gate thresholds are defined per-skill in `evolve_plan.md` (auto-generated during setup):

```yaml
gates:
  min_delta: 0.02          # Minimum improvement to keep
  max_regression: 0.05     # Maximum regression tolerance
  max_tokens: 50000        # Token budget per eval
  holdout_floor: 0.60      # Minimum holdout score
```

### Cleanup

```bash
# Clean old eval iterations (keep last 5 + all 'keep' iterations)
python3 scripts/evolve_loop.py ./my-skill/ --cleanup

# Clean old best_version snapshots (keep last 3)
python3 scripts/evolve_loop.py ./my-skill/ --cleanup-versions
```

Git history is auto-squashed after evolution completes.

---

## Technical Documentation

- **[Architecture v2.1](./docs/architecture-v2.1.md)** — Full technical design: 4-layer architecture, workspace design, protocol details, and v1.1 → v2.1 changelog
- **[Bootstrap Report](./docs/bootstrap-report.md)** — Self-evolution test results

---

## Relationship to Skill Creator

Skill Evolver is a **superset** of Skill Creator.

| Capability | Creator | Evolver | How |
|---|---|---|---|
| Create skill | Yes | Yes | Evolver calls Creator |
| Evaluate skill | Yes | Yes | Evolver calls Creator |
| Improve skill | Manual | **Auto** | Evolver's core loop |
| A/B benchmark | Yes | Yes | Evolver calls Creator |
| Multi-gate control | No | **Yes** | Evolver-only |
| Experiment memory | No | **Yes** | Evolver-only |
| Autonomous loop | No | **Yes** | Evolver-only |

**Creator path discovery order:**

1. `~/.claude/skills/skill-creator/`
2. `.claude/skills/skill-creator/` (project-level)
3. `~/.claude/commands/skill-creator.md` (single-file)

If Creator is unavailable, Evolver's core loop still works but evaluation capabilities degrade to a built-in simplified version.

---

## Contributing

1. Fork the repo
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Run the bootstrap test to verify nothing breaks:
   ```bash
   python3 scripts/evolve_loop.py ./skills/skill-evolver/ --gt ./evals.json --run --max-iterations 5
   ```
4. Commit your changes
5. Open a Pull Request

---

## License

MIT

---

## Acknowledgments

- **[skill-creator](https://github.com/anthropics/claude-plugins-official/tree/main/plugins/skill-creator)** by Anthropic — evaluation engine that powers Evolver's scoring
- **[AutoResearch](https://arxiv.org/abs/2404.00445)** — the autonomous iteration pattern that inspired the outer loop design
- Built for the [Claude Code](https://claude.com/claude-code) ecosystem
