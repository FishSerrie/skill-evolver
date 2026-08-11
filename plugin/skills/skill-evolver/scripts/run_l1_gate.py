#!/usr/bin/env python3
"""L1 Quick Gate — fast validation before running full eval.

Usage: python run_l1_gate.py <skill-path> [--gt <gt-json>]

Exit code 0 = pass, 1 = fail.
Outputs JSON: {"pass": bool, "checks": [...], "errors": [...],
               "quality_findings": {"critical": [...], "warnings": [...]}}

Post skill-qa-workflow integration (2026-04-10): the gate now includes
P0 quality rules (security scanning, structural quality, compatibility)
alongside the original YAML/body/Creator checks. Critical findings
block the gate; warnings are logged for Phase 2 diagnosis but don't
block the iteration.

Rule IDs follow the skill-qa-workflow naming convention (SEC001,
S003, TD011, C001, etc.) for cross-project traceability.
"""

import argparse
import json
import random
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    require_creator, CreatorNotFoundError, validate_frontmatter,
    parse_skill_md,
)


def check_skill_structure(skill_path: Path) -> list[dict]:
    """Validate the artifact's structure by asking the artifact.

    Delegates to ``Target.structural_checks``. This function used to look
    for ``SKILL.md`` inside ``skill_path`` unconditionally, which made the
    gate answer for a skill on behalf of every shape: a prompt-file target
    failed with ``SKILL.md not found``, and since ``run_evolve_loop``
    aborts on an L1 failure, the loop could never begin on a file at all.

    Note what is *not* done here: file targets are not exempted from
    checking. A gate that skips whatever it does not recognise is a
    decoration, so the shared checks (exists, readable, non-empty) apply
    to every shape and each shape adds what only it can verify.
    """
    from target import resolve_target

    try:
        target = resolve_target(skill_path)
    except (FileNotFoundError, ValueError) as exc:
        return [{
            "name": "artifact_resolvable",
            "pass": False,
            "detail": f"cannot resolve a target for {skill_path}: {exc}",
        }]
    return target.structural_checks()


def quick_gt_sample(gt_path: Path, n_samples: int = 3) -> list[dict]:
    """Quick-sample a few GT cases and check basic structure.

    Parsing goes through ``datasets.load_cases``, which owns the mapping
    from file extension to loader. The gate used to call
    ``json.loads(gt_path.read_text())`` directly, so a CSV or TSV dataset
    — both fully supported by the evaluator that runs immediately
    afterwards — failed here with ``Expecting value: line 1 column 1``.
    The gate rejected datasets the loop could actually evaluate, and the
    reported reason pointed at JSON syntax rather than at the real
    mismatch.

    What the gate must *not* do is impose a column mapping. ``load_cases``
    defaults to requiring a column literally named ``input``, because an
    evaluator is configured with the mapping for its own dataset. The gate
    is not: it runs before any evaluator is chosen, on whatever file the
    user pointed at. An earlier version of this function called
    ``load_cases(gt_path)`` with no mapping and thereby rejected the
    engine's own historical format (``prompt`` / ``assertions``) — the
    shape every existing ``evals.json`` uses — reporting it as a missing
    ``input`` column.

    So this reads the file with the loader's *format* knowledge but keeps
    its own, deliberately loose, notion of a usable case: something that
    poses a task and states an expectation, under any of the spellings in
    circulation. Being permissive is right here — a quick gate exists to
    catch a file that is empty or unparseable before the loop spends model
    calls, not to adjudicate schemas. The evaluator will fail loudly and
    specifically if the mapping is actually wrong.
    """
    checks = []

    try:
        records = _read_records(gt_path)
    except (ValueError, OSError, KeyError) as e:
        checks.append({
            "name": "gt_readable",
            "pass": False,
            "detail": f"Cannot read GT file: {e}",
        })
        return checks

    checks.append({
        "name": "gt_readable",
        "pass": True,
        "detail": f"GT has {len(records)} cases",
    })

    if not records:
        checks.append({
            "name": "gt_nonempty",
            "pass": False,
            "detail": "GT has 0 cases",
        })
        return checks

    samples = random.sample(records, min(n_samples, len(records)))
    for i, case in enumerate(samples):
        has_prompt = _mentions(case, _TASK_WORDS)
        has_assertions = _mentions(case, _EXPECTATION_WORDS)
        ok = has_prompt and has_assertions
        case_id = case.get("case_id", case.get("id", i))
        checks.append({
            "name": f"gt_case_{case_id}_structure",
            "pass": ok,
            "detail": f"Case {case_id}: prompt={'ok' if has_prompt else 'MISSING'}, "
                      f"assertions={'ok' if has_assertions else 'MISSING'}",
        })

    return checks


#: Words that appear in a column holding the task. Matched as substrings of
#: the column name, not as the whole name — see :func:`_mentions`.
_TASK_WORDS = ("prompt", "query", "question", "input", "问题")

#: Words that appear in a column holding what the answer must contain.
_EXPECTATION_WORDS = (
    "assertion", "expectation", "expected", "point", "answer", "gt",
    "ground_truth", "label", "答",
)


def _mentions(case: dict, words: tuple[str, ...]) -> bool:
    """Whether any column whose name contains one of ``words`` has a value.

    Substring matching on the column name, deliberately, and this is the
    second attempt. The first compared whole names against a list of
    spellings the engine had used before — and immediately rejected the
    dataset it was written for, whose expectations live in
    ``gt_answer_答对_points``. ``points`` was on the list; the column is not
    called ``points``.

    That is the failure mode of naming every acceptable value in advance:
    real column names are decorated with prefixes, suffixes and a project's
    own language, so an exact-match list is wrong for every dataset except
    the ones someone remembered. The same mistake, in the same codebase,
    once let thirteen of fourteen misspelled plan settings through a
    hand-written list of look-alikes.

    A quick gate should be permissive here. It exists to catch a file that
    is empty or unparseable before the loop spends model calls — not to
    adjudicate schemas, which the evaluator does immediately afterwards
    with the actual column mapping in hand and a specific error when it is
    wrong. Being too strict here costs a run that would have worked; being
    too loose costs one L1 check that the next step repeats properly.

    Emptiness disqualifies, not absence: a row with ``prompt: ""`` states no
    task, and treating the key's presence as sufficient would pass a file of
    blank rows.
    """
    for name, value in case.items():
        lowered = str(name).lower()
        if not any(word in lowered for word in words):
            continue
        if isinstance(value, str):
            if value.strip():
                return True
        elif value:
            return True
    return False


def _read_records(gt_path: Path) -> list[dict]:
    """Read a dataset as raw records, with no column mapping applied.

    Delegates to ``datasets.read_records`` rather than reaching into a
    loader here, so that the set of supported formats has one definition.
    """
    from datasets import read_records

    return read_records(gt_path)



def creator_validate(skill_path: Path) -> list[dict]:
    """Run Creator's quick_validate.py as a redundant cross-check.

    Creator is an OPTIONAL enhancement here, not a hard dependency. The
    authoritative frontmatter check is ``common.validate_frontmatter``
    (see the ``frontmatter`` check in :func:`structural_checks`), which
    enforces the same rule set using only the stdlib. Creator's
    validator is a second opinion from an independently maintained
    implementation — valuable when present, never required.

    When Creator is absent the check reports ``skipped`` and does NOT
    contribute a failure. A skip is never silently treated as a pass:
    it is recorded explicitly so a reader can tell "nobody checked"
    apart from "checked and fine" — the same rule
    ``verifier_panel.aggregate_verdicts`` applies to skipped verifiers.
    """
    try:
        creator = require_creator()
    except CreatorNotFoundError:
        return [{
            "name": "creator_validate",
            "pass": True,
            "skipped": True,
            "detail": "skill-creator not installed — skipped this redundant "
                      "cross-check. The authoritative frontmatter validation "
                      "(stdlib-only) already ran; see the 'frontmatter' check.",
        }]

    validate_script = creator / "scripts" / "quick_validate.py"
    if not validate_script.exists():
        return [{
            "name": "creator_validate",
            "pass": True,
            "skipped": True,
            "detail": f"Creator found but quick_validate.py is missing at "
                      f"{validate_script} — skipped. Installation may be "
                      "incomplete or outdated.",
        }]

    try:
        result = subprocess.run(
            [sys.executable, str(validate_script), str(skill_path)],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return [{
            "name": "creator_validate",
            "pass": True,
            "skipped": True,
            "detail": "Creator validation timed out (10s) — skipped.",
        }]
    except OSError as e:
        return [{
            "name": "creator_validate",
            "pass": True,
            "skipped": True,
            "detail": f"Could not execute Creator validation({e}) — skipped.",
        }]

    # Creator's validator imports PyYAML. On an interpreter without it the
    # script dies with ModuleNotFoundError, which says nothing about the
    # skill under test — treat an environment failure as a skip, not a
    # verdict. Only a clean run (exit 0 or a real validation failure with
    # a message on stdout) counts as an opinion.
    stderr = result.stderr.strip()
    if result.returncode != 0 and "ModuleNotFoundError" in stderr:
        missing = "PyYAML" if "yaml" in stderr else "a dependency"
        return [{
            "name": "creator_validate",
            "pass": True,
            "skipped": True,
            "detail": f"Creator's validator could not run ({missing} missing "
                      f"in {sys.executable}) — skipped. This reflects the "
                      "environment, not the skill.",
        }]

    return [{
        "name": "creator_validate",
        "pass": result.returncode == 0,
        "detail": result.stdout.strip() or stderr or "creator validation complete",
    }]


# ─────────────────────────────────────────────
# P0 Quality Rules (program-checkable, no LLM)
#
# Inspired by skill-qa-workflow's 83-rule ruleset. Only the critical
# and error rules that are deterministic (regex/len/byte-check) are
# included here. P1 heuristic/LLM rules live in L2 GT probes.
# ─────────────────────────────────────────────

# Secret patterns — SEC003 / SEC005
_SECRET_PATTERNS = [
    (r"sk-[a-zA-Z0-9]{20,}", "OpenAI API key"),
    (r"ghp_[a-zA-Z0-9]{36,}", "GitHub personal access token"),
    (r"gho_[a-zA-Z0-9]{36,}", "GitHub OAuth token"),
    (r"AKIA[A-Z0-9]{16}", "AWS access key ID"),
    (r"""(?:password|passwd|secret|token|api_key)\s*=\s*['"][^'"${\s]{4,}['"]""",
     "hardcoded credential assignment"),
]

# Dangerous command patterns — SEC001
_DANGEROUS_CMD_PATTERNS = [
    (r"rm\s+-rf\s+[/*~$]", "dangerous recursive delete"),
    (r"rm\s+-rf\s+\$\{?\w", "recursive delete with variable (risky)"),
    (r"rmdir\s+/s\s+/q", "Windows recursive delete"),
    (r"DROP\s+TABLE\s+", "SQL DROP TABLE without safeguard"),
    (r"DROP\s+DATABASE\s+", "SQL DROP DATABASE without safeguard"),
]

# Dynamic execution — SEC004
_DYNAMIC_EXEC_PATTERNS = [
    (r"\beval\(", "eval() call"),
    (r"\bexec\(", "exec() call"),
    (r"subprocess\s*\(.*shell\s*=\s*True", "subprocess with shell=True"),
]

# Pipe-to-execution — SEC006
_PIPE_EXEC_PATTERNS = [
    (r"curl\s+[^\|]*\|\s*(ba)?sh", "curl piped to shell"),
    (r"wget\s+[^\|]*\|\s*(ba)?sh", "wget piped to shell"),
    (r"curl\s+[^\|]*\|\s*python", "curl piped to Python"),
]

# Hardcoded API URLs — TD011
_HARDCODED_URL_PATTERNS = [
    (r"http://localhost:\d+", "hardcoded localhost URL"),
    (r"http://127\.0\.0\.1(:\d+)?", "hardcoded loopback URL"),
    (r"https?://0\.0\.0\.0(:\d+)?", "hardcoded wildcard URL"),
]

# Hardcoded absolute paths — C001
_ABSOLUTE_PATH_PATTERNS = [
    (r"/Users/\w+", "macOS user home path"),
    (r"/home/[a-z]\w+", "Linux user home path"),
    (r"C:\\\\Users\\\\", "Windows user path"),
    (r"/Applications/", "macOS Applications path"),
]


def _collect_skill_files(skill_path: Path) -> list[tuple[str, str]]:
    """Collect all scannable files belonging to the artifact.

    Returns (relative_path, content) tuples for .md, .py, .sh, .js, .ts
    files. Skips binary files and anything outside the artifact.

    Handles a file artifact as well as a directory one: ``rglob`` on a
    file yields nothing, so the security scan silently examined zero
    files for every prompt-file target and reported a clean result. "No
    findings because nothing was scanned" and "no findings because the
    content is clean" are the same output with opposite meanings.
    """
    if skill_path.is_file():
        try:
            return [(skill_path.name, skill_path.read_text(errors="replace"))]
        except OSError:
            return []

    files = []
    for ext in ("*.md", "*.py", "*.sh", "*.js", "*.ts"):
        for f in skill_path.rglob(ext):
            try:
                text = f.read_text(errors="replace")
                rel = str(f.relative_to(skill_path))
                files.append((rel, text))
            except OSError:
                continue
    return files


def _strip_code_fences(text: str) -> str:
    """Remove content inside markdown code markup (fences + inline).

    Security regex checks on .md files should skip code blocks AND
    inline backtick spans so documented anti-patterns like
    'No `rm -rf /`' or 'No `password=literal`' don't trigger false
    positives. Fenced content is replaced with empty strings to
    preserve line count for location tracking; inline spans are
    replaced with empty strings.
    """
    # Strip triple-backtick code blocks first (greedy over newlines)
    text = re.sub(r"```[\s\S]*?```", "", text)
    # Strip inline backtick spans (single backtick pairs)
    text = re.sub(r"`[^`\n]+`", "", text)
    return text


def _scan_patterns(text: str, patterns: list[tuple[str, str]],
                   filepath: str, rule_id: str,
                   severity: str) -> list[dict]:
    """Apply a list of regex patterns and return findings."""
    findings = []
    for pattern, label in patterns:
        matches = list(re.finditer(pattern, text, re.IGNORECASE))
        for m in matches:
            line = text[:m.start()].count("\n") + 1
            findings.append({
                "rule_id": rule_id,
                "severity": severity,
                "detail": f"{rule_id}: {label} in {filepath}:{line}",
                "file": filepath,
                "line": line,
            })
    return findings


def _check_quality_rules(skill_path: Path) -> dict:
    """Check P0 quality rules — program-only, no LLM needed.

    Returns {"critical": [...], "warnings": [...]} where each item is
    {"rule_id", "severity", "detail", "file", "line"}.

    Critical findings block the L1 gate. Warnings are logged for
    Phase 2 diagnosis but don't block the iteration.

    Rule sources (skill-qa-workflow naming convention):
      SEC001-006: security scanning
      S003+/S004+/S007: enhanced structural quality
      TD011: no hardcoded API URLs
      C001/C005: compatibility
    """
    critical: list[dict] = []
    warnings: list[dict] = []

    all_files = _collect_skill_files(skill_path)

    # The S00x rules below describe a skill's entry point specifically —
    # a frontmatter description, a body after it, a length band for the
    # file an agent reads first. A prompt file has none of that structure,
    # so those rules have nothing to measure and are skipped. The security
    # scans are NOT skipped: a secret or an `rm -rf` in a prompt is a
    # finding whatever the artifact's shape, and this function used to
    # return before reaching them whenever SKILL.md was absent — so every
    # file target came back with an empty findings list that read as "clean".
    skill_md = skill_path / "SKILL.md" if skill_path.is_dir() else None
    if skill_md is not None and skill_md.exists():
        content = skill_md.read_text()

        # --- Enhanced structural checks (S003, S004, S007) ---

        try:
            _, description, _ = parse_skill_md(skill_path)
        except (ValueError, FileNotFoundError):
            description = ""

        if 0 < len(description) < 50:
            warnings.append({
                "rule_id": "S003",
                "severity": "warning",
                "detail": f"S003: description too short ({len(description)} chars, "
                          f"recommend >= 50 for clear trigger matching)",
                "file": "SKILL.md",
                "line": None,
            })

        # Body length (after frontmatter)
        body_start = content.find("---", 3)
        body = content[body_start + 3:].strip() if body_start > 0 else content
        if 0 < len(body) < 200:
            warnings.append({
                "rule_id": "S004",
                "severity": "warning",
                "detail": f"S004: SKILL.md body too short ({len(body)} chars, "
                          f"recommend >= 200 for substantive instructions)",
                "file": "SKILL.md",
                "line": None,
            })

        # Line count range
        lines = content.split("\n")
        if len(lines) < 20:
            warnings.append({
                "rule_id": "S007",
                "severity": "warning",
                "detail": f"S007: SKILL.md only {len(lines)} lines "
                          f"(recommend 20-500 for appropriate skill granularity)",
                "file": "SKILL.md",
                "line": None,
            })

    # --- Security scans (SEC001-SEC006) ---
    #
    # Scoping matters: the security rules check what the SKILL
    # INSTRUCTS an agent to do (prompt content in .md files), not the
    # evaluation framework's own implementation (scripts/*.py). So:
    #
    #   .md files → scan for ALL rules (SEC001-006, TD011, C001)
    #               with code-fence stripping to avoid false positives
    #               on documented anti-patterns
    #   .py/.sh   → scan ONLY for secrets (SEC003/SEC005) because
    #               secrets must never appear in ANY file; but skip
    #               SEC001/SEC004/SEC006 which would false-positive on
    #               the eval framework's own subprocess calls, regex
    #               pattern definitions, etc.

    for filepath, text in all_files:
        is_markdown = filepath.endswith(".md")
        scan_text = _strip_code_fences(text) if is_markdown else text

        # SEC003/SEC005: hardcoded secrets — critical, ALL files
        critical.extend(
            _scan_patterns(scan_text, _SECRET_PATTERNS,
                           filepath, "SEC003", "critical"))

        # The remaining rules only apply to prompt content (.md files)
        if not is_markdown:
            continue

        # SEC001: dangerous delete commands — critical
        critical.extend(
            _scan_patterns(scan_text, _DANGEROUS_CMD_PATTERNS,
                           filepath, "SEC001", "critical"))

        # SEC002: sudo escalation — warning
        sudo_match = re.search(r"\bsudo\b", scan_text)
        if sudo_match:
            line = scan_text[:sudo_match.start()].count("\n") + 1
            warnings.append({
                "rule_id": "SEC002",
                "severity": "warning",
                "detail": f"SEC002: sudo usage in {filepath}:{line} "
                          f"(justify or use least-privilege)",
                "file": filepath,
                "line": line,
            })

        # SEC004: dynamic execution — warning
        warnings.extend(
            _scan_patterns(scan_text, _DYNAMIC_EXEC_PATTERNS,
                           filepath, "SEC004", "warning"))

        # SEC006: pipe-to-execution — warning
        warnings.extend(
            _scan_patterns(scan_text, _PIPE_EXEC_PATTERNS,
                           filepath, "SEC006", "warning"))

        # TD011: hardcoded API URLs — warning
        warnings.extend(
            _scan_patterns(scan_text, _HARDCODED_URL_PATTERNS,
                           filepath, "TD011", "warning"))

        # C001: hardcoded absolute paths — warning
        warnings.extend(
            _scan_patterns(scan_text, _ABSOLUTE_PATH_PATTERNS,
                           filepath, "C001", "warning"))

    # --- C005: UTF-8 BOM check ---
    # Applies to whichever file an agent actually reads first: SKILL.md
    # for a skill, the artifact itself for a file target.
    bom_path = skill_md if skill_md is not None else skill_path
    try:
        if bom_path.is_file() and bom_path.read_bytes()[:3] == b"\xef\xbb\xbf":
            warnings.append({
                "rule_id": "C005",
                "severity": "warning",
                "detail": f"C005: {bom_path.name} has UTF-8 BOM — remove for "
                          "cross-platform compatibility",
                "file": bom_path.name,
                "line": 1,
            })
    except OSError:
        pass

    return {"critical": critical, "warnings": warnings}


def run_l1_gate(skill_path: Path, gt_path: Path | None = None) -> dict:
    """Run L1 quick gate validation.

    Returns {"pass": bool, "checks": [...], "errors": [...],
             "quality_findings": {"critical": [...], "warnings": [...]}}.

    The gate FAILs if any structural check fails OR if any critical
    quality finding is present. Warnings are returned in
    quality_findings for Phase 2 visibility but don't block.
    """
    all_checks = []

    # Structure checks
    all_checks.extend(check_skill_structure(skill_path))

    # Creator validation
    all_checks.extend(creator_validate(skill_path))

    # P0 quality rules (security, structural, compatibility)
    quality = _check_quality_rules(skill_path)

    # GT sampling
    if gt_path and gt_path.exists():
        all_checks.extend(quick_gt_sample(gt_path))

    errors = [c["detail"] for c in all_checks if not c["pass"]]

    # Critical quality findings also block the gate
    for finding in quality["critical"]:
        errors.append(f"[{finding['rule_id']}] {finding['detail']}")

    return {
        "pass": len(errors) == 0,
        "checks": all_checks,
        "errors": errors,
        "quality_findings": quality,
    }


def main():
    parser = argparse.ArgumentParser(description="Run L1 gate validation")
    parser.add_argument("skill_path", type=Path,
                        help="Path to a skill directory or a prompt file")
    parser.add_argument("--gt", type=Path, default=None, help="Path to GT dataset")
    args = parser.parse_args()

    # Accepts a file as well as a directory: the gate's checks are the
    # target's, and a prompt file is a valid target. Rejecting
    # non-directories here contradicted resolve_target and made the CLI
    # unable to gate the very artifact shape the engine supports.
    if not args.skill_path.exists():
        result = {"pass": False, "checks": [],
                  "errors": [f"Not found: {args.skill_path}"]}
        print(json.dumps(result))
        sys.exit(1)

    result = run_l1_gate(args.skill_path, args.gt)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["pass"] else 1)


if __name__ == "__main__":
    main()
