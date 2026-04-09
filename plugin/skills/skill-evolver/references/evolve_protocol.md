# Evolve Core Protocol (8 Phases)

This document defines the complete execution protocol for Evolve mode. Each iteration strictly follows Phases 0 through 8.

---

## Phase 0: Pre-flight Checks

Before starting any iteration, verify:

1. **Skill directory integrity**: SKILL.md exists, directory structure is valid
2. **Ground truth ready**: Assertions exist with dev/holdout split
3. **Clean git state**: `git status` shows no uncommitted changes
4. **Workspace ready**: Reuse Creator's `<skill-name>-workspace/`, confirm `evolve/` subdirectory exists
   - Can invoke `python3 scripts/setup_workspace.py <skill-path>`
5. **Generate evolve_plan.md**: Analyze skill and GT data, produce an adaptive optimization plan (see `references/eval_strategy.md`)
6. **Determine current mutation layer**:
   - Starting layer is determined by the optimization priorities in `evolve_plan.md`
   - K consecutive iterations (K specified in plan, default 5) with no improvement at the current layer triggers promotion
7. **Establish baseline** (first iteration only):
   - Run one evaluation round per the evolve_plan.md strategy
   - Record baseline in `<workspace>/evolve/results.tsv` (iteration 0)
   - Snapshot the current skill to `<workspace>/evolve/best_versions/`

---

## Phase 1: Review (Read Memory, Complete Within 30 Seconds)

At the start of each iteration, read:

```bash
# 1. Recent experiment history
git log --oneline -20

# 2. Results log
tail -20 <workspace>/evolve/results.tsv

# 3. Fine-grained memory (if exists)
tail -10 <workspace>/evolve/experiments.jsonl

# 4. Execution traces from the most recent failed iteration
ls <workspace>/evolve/iteration-E{N}/traces/
```

**Extract from memory:**
- Which mutation types succeeded (status=keep) -- exploit these
- Which mutation types failed (status=discard) -- avoid repeating
- Which cases consistently fail -- prioritize these
- Which cases are fragile (easily regressed) -- use as regression guards
- Whether stuck (5+ consecutive discards) -- switch to radical strategy

**Read execution traces from `evolve/iteration-E{N}/traces/` for failed cases. Diagnose WHY failures happened, not just THAT they happened.**

---

## Phase 2: Ideate (Decide What to Change)

Based on Phase 1 analysis, select a mutation direction by priority:

**Priority ranking:**

1. **Fix crashes**: Cases that crashed last iteration -- fix first
2. **Exploit successful patterns**: Mutation types that were kept last iteration -- try similar variants
3. **Attack persistent failures**: Cases that fail across multiple iterations -- targeted improvement
4. **Explore new directions**: Cross-reference results + git log -- find untried approaches
5. **Simplify**: Remove ineffective parts of the skill while maintaining metrics
6. **Radical**: When stuck (5+ consecutive discards), attempt bold changes

**MANDATORY: Before proposing any change, cite specific trace evidence. State a counterfactual diagnosis: "Case X failed because of Y. If we change Z, the output would instead do W."**

**Output:**
- One-sentence description of the intended change
- mutation_type (e.g., body_rewrite / body_simplify / rule_reorder / template_change)
- Scope of change (which files)

**Anti-patterns (forbidden — written in the imperative "do not X" form so they are greppable and unambiguous):**
- do not repeat a change that was already discarded with identical content (check git log first)
- do not bundle multiple unrelated changes in one iteration (the one-sentence test: if you need "and" to describe it, it is two changes)
- do not make cross-layer changes
- do not guess — if no trace evidence points to a clear cause, say so explicitly and gather more evidence first (Meta-Trace mandatory protocol)
- **do not identify a problem without fixing it** -- if it is a problem, it warrants an iteration. The purpose of iteration is continuous improvement; skipping "small issues" forfeits improvement opportunities

---

## Phase 3: Modify (One Atomic Change)

Execute the change determined in Phase 2.

**Rules:**
- Only modify files in the current layer
- The change must be explainable in one sentence
- Post-modification self-check:
  - `git diff --stat` to inspect scope
  - More than 5 files changed -- likely not atomic, split it

**Modification principles:**
- Prefer explaining "why" over hard-coding MUST/NEVER
- Prefer structural/flow changes over adding more text
- If multiple cases independently duplicate the same helper logic, extract it into a script

---

## Phase 4: Commit

```bash
git add <changed-files>
git commit -m "experiment(<layer>): <one-sentence description>"
```

Examples:
```
experiment(body): add path-merging rules for cross-category retrieval
experiment(body): simplify node selection prompt in Stage 2
experiment(description): expand trigger coverage for edge-case scenarios
```

**Git-first strategy (four-step decision tree):**

Check in order; use git whenever possible, degrade only as a last resort:

**Step 1: Check whether the directory is under git control**
```bash
git -C <skill-path> rev-parse --is-inside-work-tree 2>/dev/null
```
- [OK] Already under git control -- proceed directly to Phase 1

**Step 2: Git installed but not initialized -- initialize immediately**
```bash
git --version 2>/dev/null  # check if git is installed
```
- [OK] Git is installed, just not initialized -- **run git init, do not skip, do not degrade**:
```bash
cd <skill-path>
git init
git add .
git commit -m "chore: init git for evolve tracking"
```

**Step 3: Git not installed -- prompt installation**
- Prompt the user to install, wait, do not auto-degrade:
```
[WARN] Git not detected. Please install and retry:
  macOS:  brew install git  or  xcode-select --install
  Ubuntu: sudo apt-get install git
  CentOS: sudo yum install git
  Windows: https://git-scm.com/download/win
```

**Step 4: Git cannot be installed (no network / restricted environment) -- degrade**
- Enable folder-based backup only after confirming git is unavailable:
  1. Back up pre-modification files to `<workspace>/evolve/best_versions/pre-iteration-N/`
  2. Record key line changes in experiments.jsonl
  3. When gate decision is discard, manually restore from backup
- **Tag results.tsv rows with `[no-git]` to remind the user to re-run with git later**



## Phase 5: Verify (Execute Per evolve_plan.md Evaluation Strategy)

The evaluation strategy is not hard-coded; it is defined in `<workspace>/evolve/evolve_plan.md`. Three configurable evaluation tiers:

### Quick Gate (every iteration, seconds)

Can invoke `python3 scripts/run_l1_gate.py <skill-path> [--gt <gt-json>]`:
- Skill file syntax is valid (YAML frontmatter is legal)
- No obvious destructive changes
- Trigger quick-sample (sample size specified by evolve_plan)
- Hard assertion spot-check (sampled core GT cases)

**Quick Gate failure -- skip directly to Phase 6 discard; do not run Dev Eval.**

### Dev Eval (frequency defined by evolve_plan, minutes)

Orchestrated by Claude (spawn subagent + grader scoring), with `scripts/run_l2_eval.py` providing helper functions:

1. **Execute**: Spawn subagent, load skill, run each prompt
2. **Grade**: Read `agents/grader_agent.md` (or Creator's full version), judge each assertion
3. **Collect timing**: Record tokens and duration
4. **Aggregate**: `run_l2_eval.aggregate_grades()` -- produces benchmark.json
5. **Focus areas**: High-priority assertion types marked in evolve_plan.md

### Strict Eval (trigger conditions defined by evolve_plan, ~10 minutes)

Trigger conditions (configured in evolve_plan.md):
- Auto-trigger every N iterations
- Or when Dev Eval pass_rate exceeds a threshold
- Or before a layer promotion

Content:
- Run holdout set (split="holdout")
- Run regression set (split="regression")
- Optional: blind A/B comparison (read `agents/comparator_agent.md`)

---

## Phase 6: Gate (Multi-Gate Decision)

Read `references/gate_rules.md` for complete gate logic.

**Simplified decision:**

```
IF crash or timeout → REVERT
IF L1 fail → DISCARD
IF dev_pass_rate > baseline.dev_pass_rate + min_delta
   AND trigger not degraded
   AND tokens <= baseline × 1.2
   AND duration <= baseline × 1.2
   AND regression not broken
   → KEEP
ELSE → DISCARD
```

**Keep action:**
- Update baseline to the current version
- Snapshot skill to best_versions/

**Discard action:**
```bash
git revert HEAD --no-edit
```
Note: Use `git revert`, not `git reset`, to preserve the history of failed experiments.

**Revert action (crash / severe regression):**
```bash
git revert HEAD --no-edit
```
Record crash reason in experiments.jsonl.

---

## Phase 7: Log

### results.tsv

```bash
echo -e "${iteration}\t${commit}\t${metric}\t${delta}\t${trigger_f1}\t${tokens}\t${guard}\t${status}\t${layer}\t${description}" >> <workspace>/evolve/results.tsv
```

### experiments.jsonl

```bash
echo '{"iteration":N,"mutation_type":"...","mutation_layer":"...","intent":"...","diagnosis":"...","cases_improved":[...],"cases_degraded":[...],"trigger_delta":0.0,"token_delta":0,"status":"keep/discard"}' >> <workspace>/evolve/experiments.jsonl
```

### Progress Summary (every 10 iterations)

```
=== Skill Evolve Progress (iteration 20) ===
Baseline: 65.0% → Current best: 78.0% (+13.0%)
Keeps: 6 | Discards: 12 | Crashes: 2
Current layer: body
Last 5: keep, discard, discard, keep, keep
```

---

## Phase 8: Loop

- **bounded**: Reached max_iterations -- output summary + best skill
- **unbounded**: Continue to Phase 1
- **layer promotion**: Current layer has K consecutive iterations with no keep -- promote to next layer
- **stuck detection**: 5 consecutive discards -- switch to radical strategy (Priority 6)
- **exhaustion**: All 3 layers attempted with no improvement -- output final report and terminate

---

## Terminal Output

When Evolve terminates, output:

1. **best_skill/**: Complete skill directory of the current best version
2. **results.tsv**: Full experiment log
3. **experiments.jsonl**: Fine-grained memory
4. **summary.md**:
   - Improvement from baseline to best
   - List of effective changes
   - List of ineffective changes
   - Keep/discard ratio per layer
   - Recommended next optimization directions

---

## Artifact Cleanup

Evolve produces many intermediate artifacts (git commits, best_versions snapshots, evaluation outputs). Clean up after termination:

### Automatic Cleanup Rules

1. **best_versions/**: Retain only the 3 most recent snapshots; delete older ones
2. **iteration-EN/ evaluation artifacts**: Retain only the 5 most recent iterations and all kept iterations; delete the rest
3. **git history**: **Never auto-clean** (git revert preserves full history; manual squash is optional)

### Manual Cleanup Commands

```bash
# Clean evaluation artifacts (keep last 5 iterations + all kept iterations)
python3 scripts/evolve_loop.py <skill-path> --cleanup

# Clean best_versions (keep only the 3 most recent)
python3 scripts/evolve_loop.py <skill-path> --cleanup-versions

# Full cleanup (delete entire evolve/ subdirectory, preserve Creator data)
rm -rf <workspace>/evolve/
```

### Git Cleanup Recommendations

After Evolve completes, to clean experiment and revert commits from git history:
```bash
# Find the commit before evolve started
git log --oneline | grep -v "experiment\|Revert" | head -1

# Interactive rebase to that point (optional, not required)
# git rebase -i <commit-before-evolve>
```

**Note: Do not clean git during an active evolve run. Intermediate artifacts are part of the memory system.**
