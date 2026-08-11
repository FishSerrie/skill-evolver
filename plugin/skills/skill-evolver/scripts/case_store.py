"""Where per-case records are written.

Single responsibility: turn a list of case dicts into files on disk. Knows
nothing about evaluation, gating, or the loop's phases — it takes a
directory and some dicts.

Why this is its own module
--------------------------
It used to live in ``evolve_loop``, and four modules imported it from
there: ``evaluators``, ``evaluator_backends``, ``grader_evaluator`` and
``orchestrator``. That is a two-line file-writing helper dragging in the
entire phase module — measured at eleven of this package's modules and
78 ms — and, worse, ``evolve_loop`` imports ``evaluators`` back, so the
import had to be written inside each function body to keep the cycle from
closing.

That arrangement worked and was fragile in a specific way: hoisting any one
of those four function-level imports to the top of its file raises
``ImportError`` at load time. Verified, not assumed — the failure is
``cannot import name 'get_evaluator' from partially initialized module
'evaluators'``, reported from ``aggregate_results`` and pointing at neither
the file that was edited nor the helper being imported.

An editor's "optimise imports" does exactly that hoist. So the cycle is
broken here structurally instead: this module imports nothing from the
package, which means the four callers can import it at the top of the file
like anything else.
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["write_cases_to_dir"]


def write_cases_to_dir(cases_dir: Path, cases: list | None) -> Path | None:
    """Write per-case structured JSON files to an explicit target directory.

    Low-level primitive. Does not know about workspace/iteration
    conventions — just takes a target directory and a list of case dicts and
    writes one ``case_{case_id}.json`` per entry. Creates the directory if
    it doesn't exist. Returns the directory on success, or None if ``cases``
    is empty.

    Each case dict is expected to have at minimum ``case_id``, ``prompt``,
    ``assertions``, and ``summary`` fields (the shape produced by
    ``LocalEvaluator.full_eval``). The files are laid out to be
    grep-friendly so a proposer can ``grep -l '"pass": false'
    iteration-E*/cases/*.json`` to find failing cases across history,
    matching the Meta-Harness paper §2 filesystem access pattern
    (arXiv 2603.28052).

    Case ids are zero-padded to 3 digits in the filename
    (``case_003.json``) so lexicographic listing also gives numeric order
    for typical skill GT sizes (< 1000 cases). This eliminates the lex-sort
    bug family entirely for case file iteration.
    """
    if not cases:
        return None
    cases_dir = Path(cases_dir)
    cases_dir.mkdir(parents=True, exist_ok=True)
    for case in cases:
        case_id = case.get("case_id", "?")
        # Zero-pad for lex-sort friendliness (case_003 < case_010)
        try:
            file_name = f"case_{int(case_id):03d}.json"
        except (TypeError, ValueError):
            file_name = f"case_{case_id}.json"
        (cases_dir / file_name).write_text(
            json.dumps(case, indent=2, ensure_ascii=False)
        )
    return cases_dir
