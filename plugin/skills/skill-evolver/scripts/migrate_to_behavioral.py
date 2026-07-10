#!/usr/bin/env python3
"""Back-fill ``target: "output" | "skill_doc"`` onto an existing evals.json.

Stage A of the multi-agent evolution architecture plan (see
docs/private/multi-agent-evolution-upgrade/architecture.md, Module A).
``BehavioralEvaluator`` (evaluator_backends.py) routes each assertion to
either the real transcript (``target="output"``) or the static skill
corpus (``target="skill_doc"``) based on this field, defaulting missing
values to ``"output"``. Some existing GT assertions were written to test
documentation structure ("SKILL.md must reference references/foo.md")
rather than behavior — those need the ``skill_doc`` tag, or switching to
``evaluator: behavioral`` will make them fail for the wrong reason
(testing them against agent output instead of the doc they're actually
about).

This is a CLASSIFICATION problem, not a regression-safety mechanism —
switching evaluators safely still requires re-running baseline (see
architecture plan Module A "向后兼容与迁移期风险"). The two are
independent and this script only does the former.

This script does NOT overwrite the input file. It writes a sibling
``<name>.migrated.json`` plus a ``migration_report.md`` listing every
assertion the heuristic tagged ``skill_doc`` with its reason, for a
human to confirm/override before the migrated file replaces the
original — "不自动生效，需要人工过一遍再落盘" (architecture plan
Module A). The heuristic is deliberately conservative: anything it
isn't confident about defaults to "output" (the architecture plan's
own stated bias — "宁可暴露真问题，也不要悄悄继续只检查文档").

Usage:
    python migrate_to_behavioral.py evals.json
    python migrate_to_behavioral.py evals.json --out custom_name.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Assertion types that are inherently about output/execution, not doc
# structure — always "output" regardless of their value string.
_ALWAYS_OUTPUT_TYPES = {"path_hit", "script_check"}

# Substring markers (case-insensitive) that suggest an assertion's value
# is really about documentation structure ("this doc must mention X"),
# not about what the agent's response should contain. Deliberately the
# same illustrative set the architecture plan names — extend with care,
# a keyword false-positive here silently routes an output assertion at
# the static corpus and produces a misleadingly-passing eval.
_SKILL_DOC_MARKERS = (
    "readme",
    "references/",
    "agents/",
    "skill.md",
    "必须包含",
    "字段说明",
)

_HEURISTIC_TYPES = {"contains", "not_contains", "regex"}


def classify_assertion(assertion: dict) -> tuple[str, str]:
    """Return (target, reason) for one assertion. Pure function — no I/O,
    so this is unit-testable without touching disk."""
    atype = assertion.get("type", "contains")

    if atype in _ALWAYS_OUTPUT_TYPES:
        return "output", f"type={atype} is inherently about execution/output"

    if atype in _HEURISTIC_TYPES:
        value = str(assertion.get("value", ""))
        lowered = value.lower()
        hit = next((m for m in _SKILL_DOC_MARKERS if m in lowered), None)
        if hit is not None:
            return "skill_doc", f"value contains doc-structure marker {hit!r}"

    return "output", "conservative default (no doc-structure marker matched)"


def migrate_evals(data: dict | list) -> tuple[dict | list, list[dict]]:
    """Tag every assertion in every eval case with a ``target`` field.

    Returns (migrated_data, skill_doc_entries) where skill_doc_entries
    is the flat list of {"case_id", "assertion_index", "type", "value",
    "reason"} dicts for every assertion classified as "skill_doc" — the
    input to the human-facing migration report.
    """
    raw_cases = data if isinstance(data, list) else data.get("evals", [])
    skill_doc_entries: list[dict] = []

    for case in raw_cases:
        case_id = case.get("id", "?")
        for idx, assertion in enumerate(case.get("assertions", [])):
            target, reason = classify_assertion(assertion)
            assertion["target"] = target
            if target == "skill_doc":
                skill_doc_entries.append({
                    "case_id": case_id,
                    "assertion_index": idx,
                    "type": assertion.get("type", "contains"),
                    "value": assertion.get("value", ""),
                    "reason": reason,
                })

    return data, skill_doc_entries


def build_migration_report(skill_doc_entries: list[dict], source: Path) -> str:
    lines = [
        f"# Migration report: {source.name}",
        "",
        f"{len(skill_doc_entries)} assertion(s) tagged `target: \"skill_doc\"` "
        "by heuristic. Everything else defaulted to `\"output\"`.",
        "",
        "This is a heuristic classification, not a verified one — review",
        "each row below before trusting the migrated file. A false",
        "positive here routes a real behavior assertion at the static",
        "skill corpus instead of the agent's transcript.",
        "",
    ]
    if not skill_doc_entries:
        lines.append("(none — every assertion defaulted to `\"output\"`)")
    else:
        lines.append("| case_id | assertion_index | type | value | reason |")
        lines.append("|---|---|---|---|---|")
        for e in skill_doc_entries:
            value = str(e["value"]).replace("|", "\\|")[:80]
            lines.append(
                f"| {e['case_id']} | {e['assertion_index']} | {e['type']} "
                f"| `{value}` | {e['reason']} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evals_path", type=Path, help="path to evals.json")
    parser.add_argument("--out", type=Path, default=None,
                        help="output path (default: <name>.migrated.json "
                             "next to the input)")
    args = parser.parse_args(argv)

    if not args.evals_path.exists():
        print(f"[ERROR] not found: {args.evals_path}", file=sys.stderr)
        return 1

    data = json.loads(args.evals_path.read_text())
    migrated, skill_doc_entries = migrate_evals(data)

    out_path = args.out or args.evals_path.with_suffix(".migrated.json")
    out_path.write_text(json.dumps(migrated, indent=2))

    report_path = out_path.with_name("migration_report.md")
    report_path.write_text(build_migration_report(skill_doc_entries, args.evals_path))

    print(f"Wrote {out_path}")
    print(f"Wrote {report_path}")
    print(f"{len(skill_doc_entries)} assertion(s) tagged skill_doc — "
          f"review the report before replacing {args.evals_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
