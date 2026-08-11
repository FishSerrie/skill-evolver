"""Creator-decoupling regression tests.

skill-creator ships as a Claude Code plugin, so it is typically absent on
Codex / OpenCode / other hosts. These tests pin the two properties that
make the evolve loop portable:

1. The built-in frontmatter validator is authoritative — it enforces the
   full rule set with the stdlib alone, so a bad skill is still caught
   when Creator is nowhere to be found.
2. Creator's absence degrades to an explicit ``skipped``, never to a
   silent pass and never to a crash.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).parent.parent / "plugin" / "skills" / "skill-evolver" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402
import run_l1_gate  # noqa: E402


def write_skill(tmp: Path, frontmatter: str, body: str = "# Body\n\n" + "Real content. " * 20) -> Path:
    d = tmp / "skill"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}\n")
    return d


class BuiltInValidatorIsAuthoritativeTests(unittest.TestCase):
    """common.validate_frontmatter must not need Creator or PyYAML."""

    def setUp(self):
        self._tmp = Path(__file__).parent / "_tmp_decouple"
        self._tmp.mkdir(exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_accepts_a_valid_skill(self):
        d = write_skill(self._tmp, 'name: good-skill\ndescription: "Does a thing."')
        ok, msg = common.validate_frontmatter(d)
        self.assertTrue(ok, msg)

    def test_rejects_non_kebab_case_name(self):
        d = write_skill(self._tmp, 'name: Bad_Skill\ndescription: "x"')
        ok, msg = common.validate_frontmatter(d)
        self.assertFalse(ok)
        self.assertIn("kebab-case", msg)

    def test_rejects_consecutive_and_edge_hyphens(self):
        for bad in ("bad--skill", "-badskill", "badskill-"):
            d = write_skill(self._tmp, f'name: {bad}\ndescription: "x"')
            ok, msg = common.validate_frontmatter(d)
            self.assertFalse(ok, f"{bad} should be rejected")
            self.assertIn("hyphen", msg)

    def test_rejects_overlong_name(self):
        d = write_skill(self._tmp, f'name: {"a" * 70}\ndescription: "x"')
        ok, msg = common.validate_frontmatter(d)
        self.assertFalse(ok)
        self.assertIn("too long", msg)

    def test_rejects_angle_brackets_in_description(self):
        d = write_skill(self._tmp, 'name: angle-skill\ndescription: "Use <this>"')
        ok, msg = common.validate_frontmatter(d)
        self.assertFalse(ok)
        self.assertIn("angle bracket", msg)

    def test_rejects_overlong_description(self):
        d = write_skill(self._tmp, f'name: long-desc\ndescription: "{"x" * 1100}"')
        ok, msg = common.validate_frontmatter(d)
        self.assertFalse(ok)
        self.assertIn("too long", msg)

    def test_rejects_unexpected_frontmatter_key(self):
        d = write_skill(self._tmp, 'name: key-skill\ndescription: "x"\nverison: 1.0')
        ok, msg = common.validate_frontmatter(d)
        self.assertFalse(ok)
        self.assertIn("Unexpected key", msg)
        self.assertIn("verison", msg)

    def test_allows_nested_metadata_and_list_items(self):
        """Nested keys are not top-level keys — a line scan must not confuse them."""
        d = write_skill(
            self._tmp,
            'name: meta-skill\ndescription: "x"\nmetadata:\n  author: serrie\n'
            '  tags:\n    - alpha\n    - beta\nallowed-tools: Read',
        )
        ok, msg = common.validate_frontmatter(d)
        self.assertTrue(ok, msg)

    def test_does_not_import_pyyaml(self):
        """The whole point: validation must work on an interpreter without PyYAML."""
        d = write_skill(self._tmp, 'name: no-yaml\ndescription: "x"')
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def deny_yaml(name, *a, **kw):
            if name == "yaml":
                raise ImportError("PyYAML is deliberately unavailable in this test")
            return real_import(name, *a, **kw)

        with mock.patch("builtins.__import__", side_effect=deny_yaml):
            ok, msg = common.validate_frontmatter(d)
        self.assertTrue(ok, msg)


class CreatorAbsenceDegradesTests(unittest.TestCase):
    """L1's creator_validate must skip loudly, not crash and not lie."""

    def setUp(self):
        self._tmp = Path(__file__).parent / "_tmp_decouple2"
        self._tmp.mkdir(exist_ok=True)
        self.skill = write_skill(self._tmp, 'name: ok-skill\ndescription: "Fine."')

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_missing_creator_is_skipped_not_failed(self):
        with mock.patch.object(
            run_l1_gate, "require_creator",
            side_effect=common.CreatorNotFoundError("not installed"),
        ):
            checks = run_l1_gate.creator_validate(self.skill)
        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0]["pass"], "absence must not fail the gate")
        self.assertTrue(checks[0]["skipped"], "absence must be recorded as skipped")

    def test_missing_pyyaml_in_creator_is_skipped_not_failed(self):
        """An environment failure in Creator's script says nothing about the skill."""
        fake = self._tmp / "creator"
        (fake / "scripts").mkdir(parents=True)
        (fake / "scripts" / "quick_validate.py").write_text("import yaml\n")
        completed = mock.Mock(
            returncode=1, stdout="",
            stderr="ModuleNotFoundError: No module named 'yaml'",
        )
        with mock.patch.object(run_l1_gate, "require_creator", return_value=fake), \
                mock.patch.object(run_l1_gate.subprocess, "run", return_value=completed):
            checks = run_l1_gate.creator_validate(self.skill)
        self.assertTrue(checks[0]["pass"])
        self.assertTrue(checks[0]["skipped"])
        self.assertIn("PyYAML", checks[0]["detail"])

    def test_real_validation_failure_still_fails(self):
        """Degradation must not swallow a genuine verdict from Creator."""
        fake = self._tmp / "creator2"
        (fake / "scripts").mkdir(parents=True)
        (fake / "scripts" / "quick_validate.py").write_text("pass\n")
        completed = mock.Mock(
            returncode=1, stdout="Name 'Bad_Skill' should be kebab-case", stderr="",
        )
        with mock.patch.object(run_l1_gate, "require_creator", return_value=fake), \
                mock.patch.object(run_l1_gate.subprocess, "run", return_value=completed):
            checks = run_l1_gate.creator_validate(self.skill)
        self.assertFalse(checks[0]["pass"], "a real failure must still fail")
        self.assertFalse(checks[0].get("skipped", False))

    def test_bad_skill_is_caught_with_no_creator_at_all(self):
        """The end-to-end property that makes the loop portable."""
        bad = write_skill(self._tmp,'name: Bad_Name\ndescription: "x"')
        with mock.patch.object(
            run_l1_gate, "require_creator",
            side_effect=common.CreatorNotFoundError("not installed"),
        ):
            result = run_l1_gate.run_l1_gate(bad)
        self.assertFalse(result["pass"], "bad skill must be caught without Creator")
        failed = [c["name"] for c in result["checks"] if not c["pass"]]
        self.assertIn("frontmatter_valid", failed)


if __name__ == "__main__":
    unittest.main()
