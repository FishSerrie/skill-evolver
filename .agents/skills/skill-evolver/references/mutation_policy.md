# Layered Mutation Strategy

## Core Principle

**Exhaust the current layer before promoting to the next. Cross-layer changes are forbidden.**

Each iteration allows exactly one atomic change within the current layer.

---

## Layer Definitions

### Layer 1: Description

**What changes**: The `description` field in SKILL.md frontmatter

**Objective**:
- Trigger when the skill should trigger (recall)
- Stay silent when it should not (precision)

**Evaluation metric**: Trigger F1

**Cost**: Low -- each iteration only runs trigger eval (~20 queries, seconds)

**Methods**:
- Generate candidate descriptions -- run trigger eval -- select the best
- Can reuse skill-creator's run_loop approach (60/40 train/test split)

**Entry condition**: Default first layer (skip if trigger is already strong)

**Exit condition**: K consecutive iterations (default 5) with no trigger F1 improvement

### Layer 2: SKILL.md Body

**What changes**:
- Instruction wording and phrasing
- Step ordering and flow structure
- Output format templates
- Rule organization and priority
- Guidance text and explanations

**Objective**:
- Improve response quality (pass_rate)
- Improve behavioral stability (reduce variance)
- Reduce unnecessary token consumption

**Evaluation metric**: Behavior GT (assertion pass_rate)

**Cost**: Medium -- each iteration runs Dev Eval

**Methods**:
- Analyze common patterns in failed cases
- Generate targeted improvement hypotheses
- Make one atomic change (one rule / one step / one template section)

**Entry condition**: After Layer 1 exits

**Exit condition**: K consecutive iterations with no pass_rate improvement

### Layer 3: Scripts / References / Resources

**What changes**:
- Helper script logic
- Reference file content and structure
- Retrieval configuration and parameters
- Template files
- Knowledge base indexes

**Objective**:
- Raise the skill's capability ceiling
- Solve structural problems that the body layer cannot address

**Evaluation metric**: Full behavior suite + performance metrics

**Cost**: High -- each iteration runs Dev Eval + may trigger Strict Eval

**Methods**:
- Requires deeper code-level analysis
- Change scope may be larger, but atomicity must still be maintained

**Entry condition**: After Layer 2 exits

**Exit condition**: K consecutive iterations with no improvement -- all layers exhausted, end evolve

---

## Layer Promotion Mechanism

```
Current layer has K consecutive iterations with no keep
  → Output current layer summary (successful/failed change statistics)
  → Promote to the next layer
  → Reset consecutive discard counter

All layers attempted with no improvement
  → Output final report
  → End evolve
```

---

## Atomic Change Self-Check

After every Modify phase, run this self-check:

1. **One-sentence test**: Can you describe this change in one sentence? If you need "and", it is two changes -- split them.
2. **File count check**: `git diff --name-only | wc -l`. More than 3 files -- likely not atomic.
3. **Diff size check**: `git diff --stat`. More than 30 lines added -- review whether it can be more concise.
