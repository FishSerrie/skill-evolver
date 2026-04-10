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
    """Validate skill directory structure."""
    checks = []

    # SKILL.md exists
    skill_md = skill_path / "SKILL.md"
    checks.append({
        "name": "skill_md_exists",
        "pass": skill_md.exists(),
        "detail": "SKILL.md exists" if skill_md.exists() else "SKILL.md not found",
    })

    if not skill_md.exists():
        return checks

    # Frontmatter valid
    valid, msg = validate_frontmatter(skill_path)
    checks.append({
        "name": "frontmatter_valid",
        "pass": valid,
        "detail": msg,
    })

    # Check file not empty
    content = skill_md.read_text()
    body_start = content.find("---", 3)
    if body_start > 0:
        body = content[body_start + 3:].strip()
        has_body = len(body) > 10
    else:
        has_body = False
    checks.append({
        "name": "has_body",
        "pass": has_body,
        "detail": "SKILL.md has body content" if has_body else "SKILL.md body is empty or too short",
    })

    return checks


def quick_gt_sample(gt_path: Path, n_samples: int = 3) -> list[dict]:
    """Quick-sample a few GT cases and check basic structure."""
    checks = []

    try:
        data = json.loads(gt_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        checks.append({
            "name": "gt_readable",
            "pass": False,
            "detail": f"Cannot read GT file: {e}",
        })
        return checks

    # Support both flat list and {"evals": [...]} format
    if isinstance(data, list):
        cases = data
    elif isinstance(data, dict) and "evals" in data:
        cases = data["evals"]
    else:
        checks.append({
            "name": "gt_format",
            "pass": False,
            "detail": "GT must be a list or {evals: [...]}",
        })
        return checks

    checks.append({
        "name": "gt_readable",
        "pass": True,
        "detail": f"GT has {len(cases)} cases",
    })

    if not cases:
        checks.append({
            "name": "gt_nonempty",
            "pass": False,
            "detail": "GT has 0 cases",
        })
        return checks

    # Sample a few and check structure
    samples = random.sample(cases, min(n_samples, len(cases)))
    for i, case in enumerate(samples):
        has_prompt = "prompt" in case or "query" in case
        has_assertions = "assertions" in case or "expectations" in case or "expected_output" in case
        ok = has_prompt and has_assertions
        checks.append({
            "name": f"gt_case_{case.get('id', i)}_structure",
            "pass": ok,
            "detail": f"Case {case.get('id', i)}: prompt={'ok' if has_prompt else 'MISSING'}, "
                      f"assertions={'ok' if has_assertions else 'MISSING'}",
        })

    return checks


def creator_validate(skill_path: Path) -> list[dict]:
    """Run creator's quick_validate.py. Creator MUST be available."""
    creator = require_creator()  # raises CreatorNotFoundError if missing

    validate_script = creator / "scripts" / "quick_validate.py"
    if not validate_script.exists():
        return [{
            "name": "creator_validate",
            "pass": False,
            "detail": f"Creator's quick_validate.py not found at {validate_script}. "
                      "Your skill-creator installation may be incomplete or outdated.",
        }]

    try:
        result = subprocess.run(
            [sys.executable, str(validate_script), str(skill_path)],
            capture_output=True, text=True, timeout=10,
        )
        return [{
            "name": "creator_validate",
            "pass": result.returncode == 0,
            "detail": result.stdout.strip() or result.stderr.strip() or "creator validation complete",
        }]
    except subprocess.TimeoutExpired:
        return [{
            "name": "creator_validate",
            "pass": False,
            "detail": "Creator validation timed out (10s)",
        }]
    except OSError as e:
        return [{
            "name": "creator_validate",
            "pass": False,
            "detail": f"Creator validation error: {e}",
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
    (r"\beval\s*\(", "eval() call"),
    (r"\bexec\s*\(", "exec() call"),
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
    """Collect all scannable files under the skill directory.

    Returns (relative_path, content) tuples for .md, .py, .sh, .js, .ts
    files. Skips binary files and anything outside the skill tree.
    """
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
    """Remove content inside markdown code fences (triple-backtick).

    Security regex checks on .md files should skip code blocks so
    documented anti-patterns like 'Do NOT use `eval()`' don't trigger
    false positives. The fenced content is replaced with empty strings
    to preserve line count for location tracking.
    """
    return re.sub(r"```[\s\S]*?```", "", text)


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

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return {"critical": critical, "warnings": warnings}

    content = skill_md.read_text()
    all_files = _collect_skill_files(skill_path)

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
    try:
        raw = skill_md.read_bytes()
        if raw[:3] == b"\xef\xbb\xbf":
            warnings.append({
                "rule_id": "C005",
                "severity": "warning",
                "detail": "C005: SKILL.md has UTF-8 BOM — remove for "
                          "cross-platform compatibility",
                "file": "SKILL.md",
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
    parser.add_argument("skill_path", type=Path, help="Path to skill directory")
    parser.add_argument("--gt", type=Path, default=None, help="Path to GT JSON file")
    args = parser.parse_args()

    if not args.skill_path.is_dir():
        result = {"pass": False, "checks": [], "errors": [f"Not a directory: {args.skill_path}"]}
        print(json.dumps(result))
        sys.exit(1)

    result = run_l1_gate(args.skill_path, args.gt)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["pass"] else 1)


if __name__ == "__main__":
    main()
