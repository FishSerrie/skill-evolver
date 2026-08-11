"""Tests for setup_workspace's target dispatch.

The point of these is the acceptance criterion the abstraction exists to
satisfy: a standalone prompt file must be a valid optimization subject.
Before target.py that command failed with "Skill directory not found",
and the class alone did not fix it — the entry point had to be wired up,
which is what these tests hold in place.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "plugin" / "skills" / "skill-evolver" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from setup_workspace import setup_workspace  # noqa: E402
from target import (  # noqa: E402
    PromptFileTarget,
    SectionTarget,
    SkillTarget,
    resolve_target,
)

SETUP = SCRIPTS / "setup_workspace.py"

SKILL_MD = (
    "---\n"
    "name: demo-skill\n"
    "description: Does a demo thing when asked.\n"
    "---\n\n"
    "# Demo\n\nBody line.\n"
)

SECTIONED = "Preamble.\n\n## Rules\n\nBe concise.\n\n## Examples\n\nQ/A.\n"


class SetupWorkspaceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, rel: str, text: str) -> Path:
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def make_skill(self, name: str = "demo-skill") -> Path:
        skill = self.tmp / name
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(SKILL_MD)
        return skill

    def run_cli(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SETUP), *[str(a) for a in args]],
            capture_output=True, text=True,
        )

    def assertScaffolded(self, result: dict) -> Path:
        """Every target type must produce the same workspace layout."""
        evolve = Path(result["evolve_dir"])
        for expected in (
            evolve / "results.tsv",
            evolve / "experiments.jsonl",
            evolve / "evolve_plan.md",
            evolve / "best_versions",
            Path(result["workspace"]) / "evals" / "checks",
        ):
            self.assertTrue(expected.exists(), f"missing {expected}")
        return evolve


class PromptFileAcceptanceTests(SetupWorkspaceTestCase):
    """The acceptance criterion for the Target abstraction."""

    def test_cli_accepts_a_standalone_prompt_file(self):
        p = self.write("my_prompt.txt", "You are a helpful assistant.\n")
        proc = self.run_cli(p)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["target_type"], "PromptFileTarget")
        self.assertScaffolded(result)

    def test_cli_no_longer_reports_a_missing_skill_directory(self):
        """The exact regression: a prompt file is not a malformed skill."""
        p = self.write("my_prompt.txt", "x\n")
        proc = self.run_cli(p)
        self.assertNotIn("Skill directory not found", proc.stderr)

    def test_cli_accepts_a_section_of_a_file(self):
        p = self.write("doc.md", SECTIONED)
        proc = self.run_cli(p, "--section", "Rules")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["target_type"], "SectionTarget")
        self.assertScaffolded(result)

    def test_cli_still_accepts_a_skill_directory(self):
        """Regression protection for the pre-existing path."""
        skill = self.make_skill()
        proc = self.run_cli(skill)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["target_type"], "SkillTarget")
        self.assertEqual(result["skill_name"], "demo-skill")
        self.assertScaffolded(result)

    def test_cli_reports_a_missing_path_without_a_traceback(self):
        proc = self.run_cli(self.tmp / "nope.md")
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("Error:", proc.stderr)

    def test_cli_rejects_section_on_a_directory_with_a_usable_hint(self):
        skill = self.make_skill()
        proc = self.run_cli(skill, "--section", "Rules")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SKILL.md", proc.stderr)


class LibraryApiTests(SetupWorkspaceTestCase):
    def test_accepts_a_target_instance(self):
        p = self.write("my_prompt.txt", "x\n")
        result = setup_workspace(PromptFileTarget(p))
        self.assertEqual(result["target_type"], "PromptFileTarget")

    def test_takes_a_target_not_a_path(self):
        """Accepting both would mean inspecting the argument's type here.

        Deciding what shape an artifact has belongs to resolve_target and
        nowhere else; a second copy of that decision is how such checks
        multiply across entry points.
        """
        skill = self.make_skill()
        with self.assertRaises(AttributeError):
            setup_workspace(skill)

    def test_callers_holding_a_path_resolve_it_first(self):
        """The pattern evolve_loop uses."""
        skill = self.make_skill()
        result = setup_workspace(resolve_target(skill))
        self.assertEqual(result["target_type"], "SkillTarget")

    def test_workspace_override_is_honoured(self):
        p = self.write("my_prompt.txt", "x\n")
        override = self.tmp / "elsewhere"
        result = setup_workspace(PromptFileTarget(p), override)
        self.assertEqual(Path(result["workspace"]), override.resolve())

    def test_two_sections_scaffold_independently(self):
        """Slug collision here would make them share evolve state."""
        p = self.write("doc.md", SECTIONED)
        a = setup_workspace(SectionTarget(p, "Rules"))
        b = setup_workspace(SectionTarget(p, "Examples"))
        self.assertNotEqual(a["workspace"], b["workspace"])

    def test_rerun_is_idempotent(self):
        p = self.write("my_prompt.txt", "x\n")
        first = setup_workspace(PromptFileTarget(p))
        second = setup_workspace(PromptFileTarget(p))
        self.assertEqual(first["workspace"], second["workspace"])
        self.assertEqual(second["created"], [])

    def test_plan_records_the_structural_baseline(self):
        """The gate needs a before-value; the plan is where it is recorded."""
        p = self.write("my_prompt.txt", "line one\nline two\n")
        result = setup_workspace(PromptFileTarget(p))
        plan = (Path(result["evolve_dir"]) / "evolve_plan.md").read_text()
        self.assertIn("Structural Baseline", plan)
        self.assertIn('"chars"', plan)

    def test_plan_header_is_not_skill_specific(self):
        """A prompt file's plan must not describe it as a skill."""
        p = self.write("my_prompt.txt", "x\n")
        result = setup_workspace(PromptFileTarget(p))
        plan = (Path(result["evolve_dir"]) / "evolve_plan.md").read_text()
        self.assertIn("PromptFileTarget", plan)
        self.assertNotIn("> Skill:", plan)

    def test_plan_summary_comes_from_the_target(self):
        skill = self.make_skill()
        result = setup_workspace(resolve_target(skill))
        plan = (Path(result["evolve_dir"]) / "evolve_plan.md").read_text()
        self.assertIn("Does a demo thing when asked.", plan)

    def test_directory_without_skill_md_is_rejected_with_a_usable_message(self):
        """Stricter than the code this replaced, deliberately.

        The old path scaffolded a workspace and wrote "(could not parse
        SKILL.md)" into the plan, leaving every later phase to fail with a
        message about whatever it touched first rather than the cause.
        """
        empty = self.tmp / "empty"
        empty.mkdir()
        proc = self.run_cli(empty)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("SKILL.md", proc.stderr)

    def test_non_utf8_target_is_reported_without_a_traceback(self):
        """A binary file is a usage error, not a crash."""
        p = self.tmp / "blob.md"
        p.write_bytes(b"\xff\xfe\x00not text")
        proc = self.run_cli(p)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("UTF-8", proc.stderr)


if __name__ == "__main__":
    unittest.main()
