# Memory Schema

Reference: the layout here is aligned with the Meta-Harness paper (Lee et al. 2026, arXiv 2603.28052) §2 filesystem model:

> "For every previous candidate harness, the filesystem stores the source code, evaluation scores, and execution traces, which the proposer retrieves via standard operations such as grep and cat rather than ingesting them as a single prompt."

In our layout: source code lives in git (via commit hash recorded in meta.json), scores live in results.tsv (time-series) and iteration-E{N}/meta.json (per-iteration aggregate), execution traces live in iteration-E{N}/cases/case_{id}.json (one structured file per GT case).

## Workspace Structure

```
<skill-name>-workspace/
├── evals/                      # GT and evaluation data (from Creator)
├── evolve/
│   ├── results.tsv             # Per-iteration summary log (time-series)
│   ├── experiments.jsonl       # Per-iteration fine-grained diagnoses
│   ├── evolve_plan.md          # Adaptive evaluation strategy
│   ├── best_versions/          # Skill snapshots for each keep
│   │   ├── iteration-1/
│   │   ├── iteration-5/
│   │   └── ...
│   ├── iteration-E1/           # Per-candidate namespace (paper §2)
│   │   ├── meta.json           # Iteration metadata + aggregate snapshot
│   │   └── cases/              # Per-case structured traces (grep-friendly)
│   │       ├── case_001.json   # one file per GT case, zero-padded ids
│   │       ├── case_002.json
│   │       └── ...
│   ├── iteration-E2/
│   │   ├── meta.json
│   │   └── cases/
│   └── ...
└── ...
```

### iteration-E{N}/meta.json

Per-iteration metadata + aggregate snapshot. Written by `evolve_loop.write_meta_json`.

```json
{
  "iteration": 12,
  "timestamp": "2026-04-09T15:23:17Z",
  "commit": "e1795fd",
  "split": "dev",
  "aggregate": {
    "total_cases": 31,
    "total_assertions": 71,
    "passed_assertions": 67,
    "pass_rate": 0.9437,
    "tokens": 0,
    "duration": 0.49
  },
  "cases_dir": "cases/",
  "cases_listed": [1, 2, 3, 7, 8, 12, ...]
}
```

The `aggregate` sub-field is a convenience snapshot — the authoritative time-series lives in `results.tsv`. `commit` links back to git history (paper §2: "source code" lives in git, referenced here).

### iteration-E{N}/cases/ Directory

Per-case structured JSON files. Written by `evolve_loop.persist_cases`, which calls `evolve_loop.write_cases_to_dir` under the hood.

Each case file (`case_{id}.json` with zero-padded id, e.g. `case_003.json`) captures paper §3's four trace components for that case.

### Field map by paper component

| Paper §3 component | Field(s) in case JSON | Populated for |
|---|---|---|
| **prompts** | `prompt` (the GT case prompt) + `skill_loaded.*` (what the evaluator corpus-loaded) | every case |
| **tool calls** | `assertions[].exit_code` / `stdout` / `stderr` / `duration_ms` / `resolved_path` | script_check |
| **model outputs** | `assertions[].judge_reasoning` (path_hit) or `assertions[].judge_verdicts[].reasoning` (fact_coverage preset) | path_hit, fact_coverage |
| **state updates** | `assertions[].match.{file,line,excerpt}` / `nearest_match` / `found_at` — matching state at eval time | contains, not_contains, regex |

### Per-assertion-type rich field reference

Every assertion record has these common fields: `index`, `type`, `value`, `description`, `pass`. Type-specific extras:

**`contains` (pass)** — matching state
```json
{"type": "contains", "value": "...", "pass": true,
 "match": {"file": "SKILL.md", "line": 114,
           "excerpt": "... matched context ±40 chars ..."}}
```

**`contains` (fail)** — diagnostic shortcut for near-misses
```json
{"type": "contains", "value": "/install skill-creator", "pass": false,
 "nearest_match": {
   "matched_text": "install skill-creator",     // longest prefix/suffix that DID match
   "missing_prefix": "/",                        // (or missing_suffix)
   "match_ratio": 0.95,                          // 0..1 fraction of chars matched
   "file": "SKILL.md",
   "line": 98,
   "excerpt": "... you need to install skill-creator before running ..."
 }}
```
`nearest_match` is `null` if no useful prefix/suffix overlap exists.

**`not_contains` (fail)** — where the forbidden string actually is
```json
{"type": "not_contains", "value": "FORBIDDEN_TOKEN", "pass": false,
 "found_at": {"file": "references/some_ref.md", "line": 42,
              "excerpt": "... docs still mention FORBIDDEN_TOKEN here ..."}}
```

**`regex` (pass)** — matched substring + location
```json
{"type": "regex", "value": "case_\\d+\\.json", "pass": true,
 "match": {"file": "references/memory_schema.md", "line": 64,
           "text": "case_003.json",
           "excerpt": "..."}}
```

**`regex` (fail)** — `nearest_match: null` (regex nearest-match is hard to compute; diagnose from the pattern + content)

**`script_check` (pass or fail)** — full tool-call capture
```json
{"type": "script_check", "value": "evals/checks/check_foo.py",
 "pass": false,
 "exit_code": 2,
 "stdout": "... up to 2000 chars ...",
 "stderr": "... up to 2000 chars ...",
 "duration_ms": 45,
 "resolved_path": "/abs/path/to/check_foo.py"}
```
`resolved_path` is `null` only when the script file couldn't be located. stdout/stderr are capped at 2000 chars each to keep the case file bounded.

**`path_hit` (pass or fail)** — LLM judge rationale
```json
{"type": "path_hit", "value": "references/eval_strategy.md",
 "pass": true,
 "judge_reasoning": "The SKILL.md Prerequisites section mentions eval_strategy.md at line 128 as the source of evaluator config templates."}
```

**`fact_coverage` preset mode** — per-fact breakdown
```json
{"type": "fact_coverage", "value": "...", "pass": false,
 "mode": "preset",
 "judge_verdicts": [
   {"fact": "three install methods", "verdict": true,
    "reasoning": "Section lists /install, git clone, and env var."},
   {"fact": "marketplace path", "verdict": false,
    "reasoning": "Mentions /install but does not specify the plugin path."},
   {"fact": "env var name", "verdict": true,
    "reasoning": "SKILL_CREATOR_PATH is called out at line 128."}
 ],
 "passed_facts": 2,
 "total_facts": 3}
```

**`fact_coverage` online mode** — keyword hit list
```json
{"type": "fact_coverage", "value": "skill-creator, marketplace, env var",
 "pass": true,
 "mode": "online",
 "keyword_hits": ["skill-creator", "marketplace", "env var"],
 "keyword_total": 3}
```

**`file_exists` (fail)** — the path that was checked
```json
{"type": "file_exists", "value": "references/missing.md", "pass": false,
 "expected_path": "/abs/path/to/skill/references/missing.md"}
```

**`json_schema`** — just `{"pass": bool}` (no extras yet)

### case.skill_loaded — state updates snapshot

```json
"skill_loaded": {
  "path": "plugin/skills/skill-evolver/",
  "size_bytes": 25092,
  "skill_md_lines": 453,
  "description_chars": 761,
  "references_loaded": [
    "references/creator_integration.md",
    "references/eval_strategy.md",
    ...
  ],
  "agents_loaded": [
    "agents/analyzer_agent.md",
    "agents/grader_agent.md",
    ...
  ]
}
```

Captured once per `full_eval` call (identical across cases in the same run). Gives a proposer reading a historical case JSON everything they need to reconstruct the corpus shape without re-running git checkout.

### Minimal example (two assertions)

```json
{
  "case_id": 3,
  "split": "dev",
  "prompt": "What installation instructions does skill-evolver show...",
  "skill_loaded": { "... see above ..." },
  "assertions": [
    {
      "index": 0, "type": "contains",
      "value": "https://github.com/anthropics/skills",
      "description": "GitHub URL must be visible",
      "pass": true,
      "match": {"file": "SKILL.md", "line": 114, "excerpt": "..."}
    },
    {
      "index": 1, "type": "script_check",
      "value": "evals/checks/check_python_version_gate.py",
      "description": "Python 3.10+ gate in common.py",
      "pass": false,
      "exit_code": 2, "stdout": "", "stderr": "Expected '3.10' ...",
      "duration_ms": 45,
      "resolved_path": "/.../check_python_version_gate.py"
    }
  ],
  "summary": {
    "total_assertions": 2,
    "passed": 1,
    "failed": 1,
    "failed_indexes": [1]
  }
}
```

The per-case JSON layout is designed to be **grep-friendly**, matching paper §2's access pattern ("the proposer retrieves via standard operations such as grep and cat"):

```bash
# Find all failing cases across history
grep -l '"pass": false' evolve/iteration-E*/cases/*.json

# Find all script_check failures with their surrounding context
grep -B1 -A3 '"type": "script_check"' evolve/iteration-E*/cases/case_003.json

# Find which iterations had cases with failed_indexes > 0
grep -l '"failed_indexes": \[.' evolve/iteration-E*/cases/*.json
```

Phase 1 Review returns **file paths**, not preloaded content — Phase 2 diagnosis uses the Read / Grep tools to pull selectively. This matches paper §2 verbatim ("rather than ingesting them as a single prompt") and keeps Phase 1 output O(kilobytes) regardless of how many iterations have accumulated.

Retention policy: keep cases/ for the 5 most recent iterations and all kept iterations; delete the rest during cleanup (same policy as before, unchanged by this refactor).

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

Retain the 3 most recent best versions; auto-clean older snapshots (matches `cleanup_best_versions(keep_n=3)` in `scripts/cleanup.py`, also re-exported from `scripts/evolve_loop.py` for back-compat).

---

## Memory Read Protocol

At every Phase 1 (Review), read:

1. `tail -20 <workspace>/evolve/results.tsv` -- observe trends and recent status
2. `tail -10 <workspace>/evolve/experiments.jsonl` -- inspect fine-grained failure reasons and diagnoses
3. `git log --oneline -20` -- review change history
4. `cat <workspace>/evolve/iteration-E{N-1}/meta.json` -- most recent iteration's aggregate + cases_listed pointer
5. `grep -l '"pass": false' <workspace>/evolve/iteration-E*/cases/*.json` -- locate failing cases across history (grep/cat model per paper §2)
6. `Read` the specific `case_{id}.json` files for failing cases -- do NOT try to read all of them; Phase 1 provides `failed_case_paths` as a targeted pointer list
7. Compute keeps/discards/crashes ratio -- determine whether stuck
