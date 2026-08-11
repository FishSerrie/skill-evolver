# Isolation Protocol (Module B)

Companion to `evolve_protocol.md` Phase 2/Phase 3. This document exists because "isolation" is easy to say and easy to accidentally undo — this is the concrete checklist for what isolation actually requires in this codebase.

## The problem this fixes

Phase 2 (diagnose) and Phase 3 (modify) used to run in the same continuous reasoning context — one `claude -p` call in CLI mode, or the driving Claude reasoning through both phases itself in one unbroken trace in the primary in-conversation path. "Diagnoser" and "mutator" were two names for the same trace, not two independent judgments.

The actual failure mode this guards against is closer to same-context confirmation bias than the cross-model identity-label bias Panickssery et al. 2024 (arXiv 2404.13076) studied — see `docs/private/multi-agent-evolution-upgrade/architecture.md` Module B for the honest boundary between what that paper proved and what this module is designed to address. Nobody has published a study of "does splitting a single-context diagnose+modify call into two isolated calls actually fix the bias" — this is our own diagnosis-driven design, not a peer-reviewed technique.

## What isolation means concretely, here

1. **Two separate calls, not one call with two names.** CLI mode: two separate `_call_claude` subprocess invocations (`phase_2_diagnose`, `phase_3_modify`). In-conversation mode: two separate Agent tool calls (`build_diagnoser_task_spec`, `build_mutator_task_spec`). If you find yourself doing the diagnosis reasoning and the modification reasoning in the same Read-Edit sequence without a call boundary between them, isolation is not actually happening regardless of what the code around you is named.

2. **Narrow function signatures, not prompt instructions.** `build_mutator_prompt(skill_path, diagnosis, current_layer)` has no `review`/`gt_path`/`workspace` parameter. This is enforced by a signature-introspection test (`tests/test_isolation_mutator.py`), not just a docstring — a future edit that adds those parameters back will fail that test immediately.

3. **Holdout exclusion is a filter, not a request.** `build_diagnoser_prompt` filters every path-like field for the substring `"holdout"` before interpolating it (`isolation._strip_holdout_items`). It does not rely on `review` never containing holdout paths in the first place — test it by trying to leak one in and checking the output.

## What this does NOT claim

- It does not claim to be the specific mechanism Panickssery et al. validated — see the honest-boundary note above.
- It does not prevent a determined mutator from re-deriving similar conclusions to the diagnoser through its own independent reasoning — isolation prevents *shared context*, not *convergent reasoning*. If the mutator would have reached the same conclusion anyway from first principles, that's not a failure of isolation.
- It does not extend to Phase 5 eval isolation (Module A) or Phase 6.5 adversarial review isolation (Module D) — those reuse the same `isolation.py` narrow-signature pattern but are separate mechanisms with separate test coverage.

## Verification

- `tests/test_isolation_diagnoser_prompt.py` — holdout exclusion, including deliberate leak-attempt inputs.
- `tests/test_isolation_diagnoser_spec.py` — conversation-mode spec shape + response parsing.
- `tests/test_isolation_mutator.py` — narrow-signature enforcement (introspection) + response parsing.
- `tests/test_llm_phase2_phase3.py` — CLI-mode two-independent-calls verification.
- Real smoke test (2026-07-13, see architecture plan §2.7): ran the full diagnose→modify round-trip against `examples/hello-skill` with a deliberate holdout-leak attempt planted in the review data, using real Agent tool calls (not mocks) for both steps. The leak attempt was filtered out before reaching either prompt, and the resulting real SKILL.md edit fixed the diagnosed failure (dev pass rate 8/10 → 9/10) without breaking the regression-guard cases.
