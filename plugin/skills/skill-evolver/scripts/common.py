#!/usr/bin/env python3
"""Shared utilities for skill-evolver scripts."""

import re
import sys
from pathlib import Path


def find_creator_path(verbose: bool = False) -> Path | None:
    """Search for skill-creator installation that has scripts/.

    Returns the skill-creator directory, or None if not found.
    Searches plugin directories, marketplace plugins, user skills, and project skills.

    When verbose=True, prints search progress and installation hint if not found.
    """
    import glob

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
        print("skill-creator not found. Some features will use fallback evaluators.",
              file=sys.stderr)
        print("Install from: https://github.com/anthropics/claude-plugins-official",
              file=sys.stderr)
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
