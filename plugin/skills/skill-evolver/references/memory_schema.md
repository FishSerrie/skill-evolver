# Memory Schema

## Workspace Structure

```
<skill-name>-workspace/
├── evals/                      # GT and evaluation data (from Creator)
├── evolve/
│   ├── results.tsv             # Per-iteration summary log
│   ├── experiments.jsonl        # Per-iteration fine-grained memory
│   ├── evolve_plan.md          # Adaptive evaluation strategy
│   ├── best_versions/          # Skill snapshots for each keep
│   │   ├── iteration-1/
│   │   ├── iteration-5/
│   │   └── ...
│   ├── iteration-E1/           # Per-iteration evaluation artifacts
│   │   ├── benchmark.json      # aggregated stats (run_l2_eval.write_benchmark)
│   │   ├── grading.json        # per-case grade dump (run_l2_eval.write_grading)
│   │   └── traces/             # Execution traces (see below)
│   ├── iteration-E2/
│   │   ├── benchmark.json
│   │   ├── grading.json
│   │   └── traces/
│   └── ...
└── ...
```

### traces/ Directory

Each `iteration-E{N}/traces/` directory stores raw execution traces for that iteration's evaluation. Traces are the primary diagnostic input for Phase 1 (Review) and Phase 2 (Ideate).

Contents:
- One trace file per evaluated case (e.g., `iteration-E{N}/traces/case_3.md`)
- Each trace captures: the prompt sent, the full LLM output, assertion results, and any error/crash output
- Traces for failed cases are the most important -- they are the evidence base for counterfactual diagnosis

Retention policy: keep traces for the 5 most recent iterations and all kept iterations; delete the rest during cleanup.

---

## results.tsv

AutoResearch-style experiment log, one row per iteration.

### Format

```
# metric_direction: higher_is_better
iteration<TAB>commit<TAB>metric<TAB>delta<TAB>trigger_f1<TAB>tokens<TAB>guard<TAB>status<TAB>layer<TAB>description
```

### Column Definitions

| Column | Type | Description |
|---|---|---|
| iteration | int | Sequence number, 0 = baseline |
| commit | string | Git short hash (7 characters), "-" when discarded |
| metric | float | Primary metric value (dev pass_rate, percentage) |
| delta | float | Change relative to the previous best (signed) |
| trigger_f1 | float | Trigger F1 score |
| tokens | int | tokens_mean |
| guard | enum | `pass` / `fail` / `-` |
| status | enum | `baseline` / `keep` / `discard` / `crash` / `revert` |
| layer | string | `description` / `body` / `script` / `-` |
| description | string | One-sentence description of the iteration's change |

### Example

```tsv
iteration	commit	metric	delta	trigger_f1	tokens	guard	status	layer	description
0	a1b2c3d	65.0	0.0	0.88	1200	pass	baseline	-	initial baseline
1	b2c3d4e	68.0	+3.0	0.88	1180	pass	keep	body	improve ambiguous-path retrieval prompts
2	-	64.0	-1.0	0.85	1350	fail	discard	body	simplify pipeline to two steps
3	c3d4e5f	70.0	+2.0	0.90	1190	pass	keep	body	add cross-category retrieval guidance
```

### Initialization

```bash
echo "# metric_direction: higher_is_better" > <workspace>/evolve/results.tsv
echo -e "iteration\tcommit\tmetric\tdelta\ttrigger_f1\ttokens\tguard\tstatus\tlayer\tdescription" >> <workspace>/evolve/results.tsv
```

---

## experiments.jsonl

Fine-grained per-iteration experiment memory, one JSON object per line.

### Field Definitions

| Field | Type | Description |
|---|---|---|
| iteration | int | Corresponds to the iteration in results.tsv |
| mutation_type | string | Change type (body_rewrite / body_simplify / rule_reorder / template_change / script_fix, etc.) |
| mutation_layer | string | Change layer (description / body / script) |
| intent | string | Change intent (one sentence) |
| diagnosis | string | Counterfactual diagnosis from Phase 2: why the targeted cases failed and what this change is expected to fix |
| changed_files | [string] | List of modified files |
| cases_improved | [int] | Case IDs that improved this iteration |
| cases_degraded | [int] | Case IDs that degraded this iteration |
| trigger_delta | float | Change in trigger F1 |
| token_delta | int | Change in tokens_mean |
| tokens | int | Total tokens consumed by this iteration's evaluation |
| duration | float | Wall-clock duration of this iteration's evaluation (seconds) |
| status | string | keep / discard / crash / revert |
| failure_reason | string | If discard/crash, brief reason |

### Example

```jsonl
{"iteration":1,"mutation_type":"body_rewrite","mutation_layer":"body","intent":"improve ambiguous-path retrieval prompts","diagnosis":"Case 3 failed because the retrieval step matched the wrong category when paths overlap. Adding an explicit disambiguation rule should force category-aware ranking.","changed_files":["SKILL.md"],"cases_improved":[3,15],"cases_degraded":[],"trigger_delta":0.0,"token_delta":-20,"tokens":4200,"duration":38.5,"status":"keep","failure_reason":""}
{"iteration":2,"mutation_type":"body_simplify","mutation_layer":"body","intent":"simplify pipeline to two steps","diagnosis":"Hypothesized that the 4-step pipeline introduces unnecessary intermediate state. Merging steps 2-3 should reduce confusion.","changed_files":["SKILL.md"],"cases_improved":[1],"cases_degraded":[3,23,40],"trigger_delta":-0.03,"token_delta":150,"tokens":4350,"duration":42.1,"status":"discard","failure_reason":"regression: 3 cases degraded, trigger dropped"}
```

---

## best_versions/

On each keep, snapshot the current skill:

```bash
cp -r <skill-dir> <workspace>/evolve/best_versions/iteration-<N>/
```

Retain the 3 most recent best versions; auto-clean older snapshots (matches `cleanup_best_versions(keep_n=3)` in `scripts/evolve_loop.py`).

---

## Memory Read Protocol

At every Phase 1 (Review), read:

1. `tail -20 <workspace>/evolve/results.tsv` -- observe trends and recent status
2. `tail -10 <workspace>/evolve/experiments.jsonl` -- inspect fine-grained failure reasons and diagnoses
3. `git log --oneline -20` -- review change history
4. `ls <workspace>/evolve/iteration-E{N}/traces/` -- scan execution traces for failed cases
5. Compute keeps/discards/crashes ratio -- determine whether stuck
