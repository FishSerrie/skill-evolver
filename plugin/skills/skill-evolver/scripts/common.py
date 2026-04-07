#!/usr/bin/env python3
"""Shared utilities for skill-evolver scripts."""

import re
import sys
from pathlib import Path


def find_any_creator(verbose: bool = False) -> tuple[Path | None, str]:
    """Search for ANY creator-like tool (skill-creator, claw-creator, etc.).

    Returns (path, creator_name) or (None, "").
    Searches for skill-creator first, then any *-creator pattern.
    """
    import glob

    home = Path.home()

    # First try skill-creator (most common)
    sc = find_creator_path(verbose=False)
    if sc:
        if verbose:
            print(f"Found skill-creator at: {sc}", file=sys.stderr)
        return sc, "skill-creator"

    # Search for any *-creator pattern in common locations
    patterns = [
        str(home / ".claude" / "plugins" / "marketplaces" / "*" / "plugins" / "*-creator" / "skills" / "*-creator"),
        str(home / ".claude" / "plugins" / "*-creator" / "skills" / "*-creator"),
        str(home / ".claude" / "skills" / "*-creator"),
    ]
    for pattern in patterns:
        for p in glob.glob(pattern):
            p = Path(p)
            if (p / "scripts").is_dir() or (p / "SKILL.md").exists():
                name = p.name
                if verbose:
                    print(f"Found creator: {name} at {p}", file=sys.stderr)
                return p, name

    if verbose:
        print("No creator found.", file=sys.stderr)
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
