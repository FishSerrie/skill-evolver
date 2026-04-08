#!/usr/bin/env python3
"""Shared utilities for skill-evolver scripts."""

import os
import re
import sys
from pathlib import Path


class CreatorNotFoundError(Exception):
    """Raised when skill-creator is not installed."""
    pass


_cached_creator_path: Path | None = None
_creator_path_resolved: bool = False


def require_creator() -> Path:
    """Find and return the skill-creator path, or raise with installation guidance.

    Caches the result after first successful resolution.
    """
    global _cached_creator_path, _creator_path_resolved

    if _creator_path_resolved:
        if _cached_creator_path is not None:
            return _cached_creator_path
        _raise_creator_not_found()

    path = find_creator_path()
    _creator_path_resolved = True

    if path is None:
        _raise_creator_not_found()

    _cached_creator_path = path
    return path


def _raise_creator_not_found() -> None:
    """Raise CreatorNotFoundError with installation guidance."""
    env_val = os.environ.get("SKILL_CREATOR_PATH", "not set")
    msg = f"""
skill-creator is REQUIRED but was not found.

skill-evolver depends on skill-creator for evaluation, grading, and
comparison capabilities. Without it, evolver cannot function.

How to install:
  1. Plugin marketplace (recommended):
     In Claude Code, run: /install skill-creator

  2. Manual install from GitHub:
     git clone https://github.com/anthropics/skills.git /tmp/anthropic-skills-latest
     cp -r /tmp/anthropic-skills-latest/skills/skill-creator ~/.claude/skills/skill-creator

     Source: https://github.com/anthropics/skills/tree/main/skills/skill-creator

  3. Already installed at a custom path?
     export SKILL_CREATOR_PATH=/your/path/to/skill-creator

Searched:
  - $SKILL_CREATOR_PATH ({env_val})
  - ~/.claude/plugins/marketplaces/*/plugins/skill-creator/
  - ~/.claude/skills/skill-creator/
  - .claude/skills/skill-creator/
""".strip()
    raise CreatorNotFoundError(msg)


def get_creator_agent_path(agent_name: str) -> Path:
    """Return the path to a creator agent file (e.g., 'grader.md').

    Raises CreatorNotFoundError if creator is not installed.
    Raises FileNotFoundError if the agent file doesn't exist.
    """
    creator = require_creator()
    agent_path = creator / "agents" / agent_name
    if not agent_path.exists():
        raise FileNotFoundError(
            f"Creator agent '{agent_name}' not found at {agent_path}. "
            f"Your skill-creator installation may be outdated."
        )
    return agent_path


def find_any_creator(verbose: bool = False) -> tuple[Path | None, str]:
    """Search for ANY skill with evaluation capabilities (skill-creator, claw-creator, etc.).

    Detection strategy:
    1. Try skill-creator by name (most common, fast path)
    2. Scan all installed skills/plugins, read their SKILL.md description,
       and check for evaluation-related keywords (eval, grading, benchmark, etc.)

    Returns (path, creator_name) or (None, "").
    """
    import glob

    home = Path.home()

    # Fast path: try skill-creator by name
    sc = find_creator_path(verbose=False)
    if sc:
        if verbose:
            print(f"Found skill-creator at: {sc}", file=sys.stderr)
        return sc, "skill-creator"

    # Slow path: scan all skills/plugins for eval capability
    # Look for any skill whose description suggests it can evaluate skills
    EVAL_KEYWORDS = {"eval", "grading", "benchmark", "test case", "assertion",
                     "scoring", "evaluate skill", "skill quality", "run_eval"}

    scan_patterns = [
        str(home / ".claude" / "plugins" / "marketplaces" / "*" / "plugins" / "*" / "skills" / "*"),
        str(home / ".claude" / "plugins" / "*" / "skills" / "*"),
        str(home / ".claude" / "skills" / "*"),
    ]

    for pattern in scan_patterns:
        for p in glob.glob(pattern):
            p = Path(p)
            if not (p / "SKILL.md").exists():
                continue
            # Read description to check for eval capability
            try:
                _, desc, _ = parse_skill_md(p)
                desc_lower = desc.lower()
                # Must have eval-related keywords AND scripts/ dir (actual tooling)
                has_eval_keywords = any(kw in desc_lower for kw in EVAL_KEYWORDS)
                has_scripts = (p / "scripts").is_dir()
                if has_eval_keywords and has_scripts:
                    name = p.name
                    if verbose:
                        print(f"Found creator-like tool: {name} at {p}",
                              file=sys.stderr)
                        print(f"  (matched eval keywords in description)",
                              file=sys.stderr)
                    return p, name
            except (ValueError, OSError):
                continue

    if verbose:
        print("No creator-like tool found.", file=sys.stderr)
    return None, ""


def setup_creator_config(workspace: Path, skill_path: Path,
                         interactive: bool = True) -> dict:
    """First-use creator configuration.

    Checks if creator is configured in evolve_plan.md.
    If not, auto-detects or prompts user.

    Returns: {"creator_path": str|None, "creator_name": str, "configured": bool}
    """
    plan_path = workspace / "evolve" / "evolve_plan.md"

    # Check if already configured
    if plan_path.exists():
        content = plan_path.read_text()
        for line in content.split("\n"):
            if line.strip().startswith("creator_path:"):
                val = line.split(":", 1)[1].strip()
                if val and val != "auto":
                    p = Path(val)
                    if p.exists():
                        return {"creator_path": str(p),
                                "creator_name": p.name, "configured": True}

    # Auto-detect
    creator_path, creator_name = find_any_creator(verbose=True)

    if creator_path:
        # Found — save to config
        _save_creator_to_plan(plan_path, str(creator_path), creator_name)
        return {"creator_path": str(creator_path),
                "creator_name": creator_name, "configured": True}

    if interactive:
        # Not found — guide user
        print("\n" + "=" * 60, file=sys.stderr)
        print("CREATOR SETUP", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print("No creator tool found. Options:", file=sys.stderr)
        print("  1. Install skill-creator (recommended):", file=sys.stderr)
        print("     https://github.com/anthropics/claude-plugins-official",
              file=sys.stderr)
        print("  2. Specify a custom creator path", file=sys.stderr)
        print("  3. Skip — use built-in evaluator (works for most cases)",
              file=sys.stderr)
        print("", file=sys.stderr)

    # Default: no creator, use local evaluator
    return {"creator_path": None, "creator_name": "", "configured": False}


def _save_creator_to_plan(plan_path: Path, creator_path: str,
                          creator_name: str) -> None:
    """Save creator configuration to evolve_plan.md."""
    if not plan_path.exists():
        return
    content = plan_path.read_text()
    # Add or update creator_path line
    if "creator_path:" in content:
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if line.strip().startswith("creator_path:"):
                lines[i] = f"creator_path: {creator_path}"
                break
        plan_path.write_text("\n".join(lines))
    else:
        # Add after evaluator config section
        content += f"\n## Creator Configuration\ncreator_path: {creator_path}\ncreator_name: {creator_name}\n"
        plan_path.write_text(content)


def find_creator_path(verbose: bool = False) -> Path | None:
    """Search for skill-creator installation that has scripts/.

    Returns the skill-creator directory, or None if not found.
    Searches plugin directories, marketplace plugins, user skills, and project skills.

    When verbose=True, prints search progress and installation hint if not found.
    """
    import glob

    # Highest priority: user-specified override via env var
    env_path = os.environ.get("SKILL_CREATOR_PATH")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if (p / "scripts").is_dir():
            if verbose:
                print(f"Found skill-creator via $SKILL_CREATOR_PATH: {p}", file=sys.stderr)
            return p

    home = Path.home()
    candidates = [
        # Marketplace plugins — skill content is inside skills/skill-creator/
        home / ".claude" / "plugins" / "marketplaces" / "claude-plugins-official" / "plugins" / "skill-creator" / "skills" / "skill-creator",
        # Marketplace plugins — plugin root level
        home / ".claude" / "plugins" / "marketplaces" / "claude-plugins-official" / "plugins" / "skill-creator",
        # Direct plugin install
        home / ".claude" / "plugins" / "skill-creator" / "skills" / "skill-creator",
        # Standalone plugin with plugin/ subdir
        home / ".claude" / "plugins" / "skill-creator" / "plugin" / "skills" / "skill-creator",
        # User skills directory
        home / ".claude" / "skills" / "skill-creator",
        # Project-level skills
        Path.cwd() / ".claude" / "skills" / "skill-creator",
    ]

    # Also search any marketplace for skill-creator (both levels)
    marketplace_glob = str(home / ".claude" / "plugins" / "marketplaces" / "*" / "plugins" / "skill-creator" / "skills" / "skill-creator")
    for p in glob.glob(marketplace_glob):
        candidates.append(Path(p))
    marketplace_glob2 = str(home / ".claude" / "plugins" / "marketplaces" / "*" / "plugins" / "skill-creator")
    for p in glob.glob(marketplace_glob2):
        candidates.append(Path(p))

    # Also search plugin subdirs with skills/skill-creator pattern
    plugin_glob = str(home / ".claude" / "plugins" / "*" / "plugin" / "skills" / "skill-creator")
    for p in glob.glob(plugin_glob):
        candidates.append(Path(p))

    for p in candidates:
        # Check for scripts/ subdir (full creator) or SKILL.md (minimal)
        if (p / "scripts").is_dir():
            if verbose:
                print(f"Found skill-creator at: {p}", file=sys.stderr)
            return p
        if (p / "SKILL.md").exists():
            if verbose:
                print(f"Found skill-creator (minimal) at: {p}", file=sys.stderr)
            return p

    if verbose:
        print("skill-creator not found.", file=sys.stderr)
        print("Install from: https://github.com/anthropics/skills/tree/main/skills/skill-creator",
              file=sys.stderr)
        print("Or set: export SKILL_CREATOR_PATH=/your/path", file=sys.stderr)
    return None


def get_evolver_root() -> Path:
    """Get the root directory of skill-evolver's own installation.

    Useful for finding our own agents/, references/, scripts/ dirs.
    """
    # This file lives at: <evolver-root>/scripts/common.py
    return Path(__file__).parent.parent


def find_workspace(skill_path: Path) -> Path:
    """Find or determine workspace path for a skill.

    Convention: <skill-parent>/<skill-name>-workspace/
    """
    skill_path = skill_path.resolve()
    name = skill_path.name
    return skill_path.parent / f"{name}-workspace"


def find_evolve_dir(skill_path: Path) -> Path:
    """Find the evolve/ subdirectory within the workspace."""
    return find_workspace(skill_path) / "evolve"


def parse_skill_md(skill_path: Path) -> tuple[str, str, str]:
    """Parse a SKILL.md file, returning (name, description, full_content).

    Compatible with skill-creator's parse_skill_md.
    """
    content = (skill_path / "SKILL.md").read_text()
    lines = content.split("\n")

    if lines[0].strip() != "---":
        raise ValueError("SKILL.md missing frontmatter (no opening ---)")

    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        raise ValueError("SKILL.md missing frontmatter (no closing ---)")

    name = ""
    description = ""
    frontmatter_lines = lines[1:end_idx]
    i = 0
    while i < len(frontmatter_lines):
        line = frontmatter_lines[i]
        if line.startswith("name:"):
            name = line[len("name:"):].strip().strip('"').strip("'")
        elif line.startswith("description:"):
            value = line[len("description:"):].strip()
            if value in (">", "|", ">-", "|-"):
                continuation: list[str] = []
                i += 1
                while i < len(frontmatter_lines) and (
                    frontmatter_lines[i].startswith("  ")
                    or frontmatter_lines[i].startswith("\t")
                ):
                    continuation.append(frontmatter_lines[i].strip())
                    i += 1
                description = " ".join(continuation)
                continue
            else:
                description = value.strip('"').strip("'")
        i += 1

    return name, description, content


def validate_frontmatter(skill_path: Path) -> tuple[bool, str]:
    """Validate SKILL.md YAML frontmatter.

    Returns (is_valid, message).
    """
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md not found"

    content = skill_md.read_text()
    if not content.startswith("---"):
        return False, "No YAML frontmatter found"

    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False, "Invalid frontmatter format"

    try:
        name, description, _ = parse_skill_md(skill_path)
    except ValueError as e:
        return False, str(e)

    if not name:
        return False, "Missing 'name' in frontmatter"
    if not description:
        return False, "Missing 'description' in frontmatter"

    return True, "Valid"
