"""Tests for target — the abstraction over what is being optimized.

Imports the module under test plus stdlib. ``SkillTarget.workspace`` and
``SkillTarget.summary`` deliberately delegate to ``common`` (the skill
workspace convention has one definition, not two), so the tests for those
two properties assert the delegation rather than re-deriving the rule —
duplicating the convention in a test would defeat the point of having one
implementation.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).parent.parent / "plugin" / "skills" / "skill-evolver" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from target import (  # noqa: E402
    SNAPSHOT_KEYS,
    AmbiguousSection,
    InvalidBody,
    PromptFileTarget,
    SectionNotFound,
    SectionTarget,
    SkillTarget,
    Target,
    resolve_target,
)

class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, rel: str, text: str) -> Path:
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def make_skill(self, name: str = "demo-skill", body: str | None = None) -> Path:
        skill_dir = self.tmp / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        if body is None:
            body = (
                "---\n"
                "name: demo-skill\n"
                "description: Does a demo thing when asked.\n"
                "---\n\n"
                "# Demo\n\nBody line.\n"
            )
        (skill_dir / "SKILL.md").write_text(body)
        return skill_dir


# ─────────────────────────────────────────────
# Abstraction contract
# ─────────────────────────────────────────────

class AbstractionTests(unittest.TestCase):
    def test_target_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            Target()

    def test_partial_implementation_cannot_be_instantiated(self):
        class Partial(Target):
            @property
            def name(self):
                return "x"

        with self.assertRaises(TypeError):
            Partial()

    def test_the_full_contract_is_abstract(self):
        """The contract is enforced by ABC, so its membership is asserted.

        A future edit that dropped ``@abstractmethod`` from one of these
        would let a half-built target reach the loop and fail mid-run.
        """
        self.assertEqual(
            Target.__abstractmethods__,
            frozenset({"name", "artifact_path", "read", "write", "snapshot"}),
        )

    def test_context_is_concrete_so_shapes_opt_in(self):
        """``context()`` has a sane default; only differing shapes override.

        Making it abstract would force every future shape to restate
        "the scored text is the mutable text", which is true by default.
        """
        self.assertNotIn("context", Target.__abstractmethods__)

    def test_vcs_root_is_concrete_and_shared(self):
        """Finding a repository root is identical for every shape.

        An abstract ``vcs_root`` would have each subclass reimplement the
        same upward walk, which is how the three copies of the skill
        layout got out of sync in the first place.
        """
        self.assertNotIn("vcs_root", Target.__abstractmethods__)

    def test_target_exposes_only_read_write_snapshot(self):
        """The narrow interface is the safeguard, so it is asserted.

        If an evaluate/score method ever appeared here, a mutation step
        holding a Target could grade its own output.
        """
        forbidden = {"evaluate", "grade", "score", "judge", "run"}
        self.assertEqual(forbidden & set(dir(Target)), set())


# ─────────────────────────────────────────────
# SkillTarget
# ─────────────────────────────────────────────

class SkillTargetTests(TempDirTestCase):
    def test_read_returns_whole_skill_md_including_frontmatter(self):
        skill = self.make_skill()
        t = SkillTarget(skill)
        text = t.read()
        self.assertTrue(text.startswith("---"))
        self.assertIn("description: Does a demo thing", text)

    def test_write_replaces_content(self):
        skill = self.make_skill()
        t = SkillTarget(skill)
        t.write("---\nname: demo-skill\ndescription: New.\n---\n\n# New\n")
        self.assertIn("description: New.", t.read())

    def test_name_is_directory_name(self):
        skill = self.make_skill("my-skill")
        self.assertEqual(SkillTarget(skill).name, "my-skill")

    def test_vcs_root_finds_the_repository_root(self):
        skill = self.make_skill()
        (self.tmp / ".git").mkdir()
        self.assertEqual(SkillTarget(skill).vcs_root, self.tmp.resolve())

    def test_vcs_root_prefers_the_nearest_repository(self):
        """A skill with its own repo must not commit into the outer one."""
        skill = self.make_skill()
        (self.tmp / ".git").mkdir()
        (skill / ".git").mkdir()
        self.assertEqual(SkillTarget(skill).vcs_root, skill.resolve())

    def test_vcs_root_falls_back_to_the_artifact_directory(self):
        """Outside a repository, the honest answer is the artifact itself.

        Guessing an ancestor would make the loop commit somewhere the
        user never asked it to.
        """
        skill = self.make_skill()
        self.assertEqual(SkillTarget(skill).vcs_root, skill.resolve())

    def test_artifact_path_and_vcs_root_coincide_for_a_repo_skill(self):
        """The one shape where the two anchors are legitimately equal."""
        skill = self.make_skill()
        (skill / ".git").mkdir()
        t = SkillTarget(skill)
        self.assertEqual(t.artifact_path, t.vcs_root)

    def test_missing_directory_raises(self):
        with self.assertRaises(FileNotFoundError):
            SkillTarget(self.tmp / "nope")

    def test_directory_without_skill_md_raises(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(FileNotFoundError) as ctx:
            SkillTarget(empty)
        self.assertIn("SKILL.md", str(ctx.exception))

    def test_workspace_delegates_to_the_single_convention(self):
        from common import find_workspace

        skill = self.make_skill()
        self.assertEqual(SkillTarget(skill).workspace, find_workspace(skill))

    def test_summary_uses_frontmatter_description(self):
        skill = self.make_skill()
        self.assertEqual(
            SkillTarget(skill).summary(), "Does a demo thing when asked."
        )

    def test_summary_falls_back_when_frontmatter_unparseable(self):
        skill = self.make_skill(body="# No frontmatter here\n\nBody.\n")
        self.assertEqual(SkillTarget(skill).summary(), "# No frontmatter here")

    def test_summary_falls_back_when_description_is_empty(self):
        skill = self.make_skill(
            body="---\nname: demo-skill\n---\n\nFirst real line.\n"
        )
        self.assertEqual(SkillTarget(skill).summary(), "---")

    def test_snapshot_counts_skill_md_and_supporting_files(self):
        skill = self.make_skill()
        (skill / "references").mkdir()
        (skill / "references" / "a.md").write_text("one\ntwo\n")
        (skill / "scripts").mkdir()
        (skill / "scripts" / "s.py").write_text("print(1)\n")

        snap = SkillTarget(skill).snapshot()
        self.assertEqual(snap["child_units"], 2)
        self.assertEqual(snap["child_lines"], 3)
        self.assertEqual(snap["extra"]["files_per_dir"]["references"], 1)
        self.assertEqual(snap["extra"]["files_per_dir"]["scripts"], 1)
        self.assertEqual(snap["extra"]["files_per_dir"]["agents"], 0)
        self.assertGreater(snap["chars"], 0)

    def test_snapshot_survives_unreadable_supporting_file(self):
        """A binary helper must not abort the snapshot.

        A gate with one uncounted file still functions; a gate that
        raised would have nothing to compare against.
        """
        skill = self.make_skill()
        (skill / "scripts").mkdir()
        (skill / "scripts" / "blob.bin").write_bytes(b"\xff\xfe\x00binary")

        snap = SkillTarget(skill).snapshot()
        self.assertEqual(snap["child_units"], 1)
        self.assertEqual(snap["child_lines"], 0)

    def test_context_is_the_corpus_while_read_is_only_skill_md(self):
        """The distinction that justifies context() existing.

        References are read at run time and so belong in the scored
        text, but only SKILL.md is offered for rewriting.
        """
        skill = self.make_skill()
        (skill / "references").mkdir()
        (skill / "references" / "extra.md").write_text("reference content\n")

        t = SkillTarget(skill)
        self.assertNotIn("reference content", t.read())
        self.assertIn("reference content", t.context())
        self.assertIn("Demo", t.context())

    def test_context_carries_the_path_headers_downstream_parses(self):
        """The ``### <path> ###`` format maps a match back to file+line."""
        skill = self.make_skill()
        (skill / "references").mkdir()
        (skill / "references" / "extra.md").write_text("x\n")

        corpus = SkillTarget(skill).context()
        self.assertIn("### SKILL.md ###", corpus)
        self.assertIn("### references/extra.md ###", corpus)

    def test_context_matches_the_evaluator_corpus_builder(self):
        """One definition of "the corpus", asserted rather than assumed."""
        from common import build_skill_corpus

        skill = self.make_skill()
        (skill / "agents").mkdir()
        (skill / "agents" / "a.md").write_text("agent text\n")
        self.assertEqual(SkillTarget(skill).context(), build_skill_corpus(skill))


# ─────────────────────────────────────────────
# PromptFileTarget
# ─────────────────────────────────────────────

class PromptFileTargetTests(TempDirTestCase):
    def test_read_and_write_round_trip(self):
        p = self.write("answer.md", "You are a helpful assistant.\n")
        t = PromptFileTarget(p)
        self.assertEqual(t.read(), "You are a helpful assistant.\n")
        t.write("You are a terse assistant.\n")
        self.assertEqual(t.read(), "You are a terse assistant.\n")

    def test_name_drops_the_extension(self):
        p = self.write("answer.md", "x\n")
        self.assertEqual(PromptFileTarget(p).name, "answer")

    def test_name_of_extensionless_file(self):
        p = self.write("PROMPT", "x\n")
        self.assertEqual(PromptFileTarget(p).name, "PROMPT")

    def test_name_of_dotfile_keeps_full_name(self):
        """``.prompt`` has an empty stem; the fallback keeps it usable."""
        p = self.write(".prompt", "x\n")
        self.assertEqual(PromptFileTarget(p).name, ".prompt")

    def test_vcs_root_walks_up_from_a_nested_file(self):
        """A file two levels down still reports the repository root."""
        p = self.write("a/b/answer.md", "x\n")
        (self.tmp / ".git").mkdir()
        self.assertEqual(PromptFileTarget(p).vcs_root, self.tmp.resolve())

    def test_vcs_root_is_not_merely_the_parent_directory(self):
        """Regression: the parent directory is usually not the repo root.

        Git commands resolve upward on their own, so the old value looked
        fine until something tried to interpret a repository-relative
        path from git output.
        """
        p = self.write("a/b/answer.md", "x\n")
        (self.tmp / ".git").mkdir()
        t = PromptFileTarget(p)
        self.assertNotEqual(t.vcs_root, p.parent)

    def test_artifact_path_is_the_file_not_the_directory(self):
        """``vcs_root`` and ``artifact_path`` must not be conflated.

        ``vcs_root`` is where history lives, ``artifact_path`` is what
        changes. They coincide only for a skill at a repository root;
        treating them as one put a file target's workspace a directory
        too high.
        """
        p = self.write("nested/answer.md", "x\n")
        t = PromptFileTarget(p)
        self.assertEqual(t.artifact_path, p.resolve())
        self.assertNotEqual(t.artifact_path, t.vcs_root)

    def test_workspace_sits_beside_the_file(self):
        p = self.write("nested/answer.md", "x\n")
        t = PromptFileTarget(p)
        self.assertEqual(t.workspace, p.parent.resolve() / "answer-workspace")

    def test_workspace_is_not_inside_the_artifact_directory_tree_above(self):
        """Regression: anchoring on ``root.parent`` skipped a level."""
        p = self.write("nested/answer.md", "x\n")
        self.assertEqual(PromptFileTarget(p).workspace.parent, p.parent.resolve())

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            PromptFileTarget(self.tmp / "nope.md")

    def test_directory_is_not_a_prompt_file(self):
        with self.assertRaises(FileNotFoundError):
            PromptFileTarget(self.tmp)

    def test_snapshot_reports_size(self):
        p = self.write("answer.md", "line one\n\nline three\n")
        snap = PromptFileTarget(p).snapshot()
        self.assertEqual(snap["lines"], 3)
        self.assertEqual(snap["non_empty_lines"], 2)
        self.assertEqual(snap["chars"], len("line one\n\nline three\n"))

    def test_lines_does_not_over_count_the_trailing_newline(self):
        """A one-line file must measure as one line.

        Counting the newline-terminated blank as a line gave every gate
        comparing line counts a systematic off-by-one.
        """
        self.assertEqual(
            PromptFileTarget(self.write("a.md", "a\n")).snapshot()["lines"], 1
        )
        self.assertEqual(
            PromptFileTarget(self.write("b.md", "a")).snapshot()["lines"], 1
        )
        self.assertEqual(
            PromptFileTarget(self.write("c.md", "a\nb\n")).snapshot()["lines"], 2
        )

    def test_snapshot_of_empty_file_reports_zero_lines(self):
        p = self.write("empty.md", "")
        snap = PromptFileTarget(p).snapshot()
        self.assertEqual(
            snap,
            {
                "chars": 0,
                "lines": 0,
                "non_empty_lines": 0,
                "child_units": 0,
                "child_lines": 0,
            },
        )

    def test_context_defaults_to_the_mutable_text(self):
        """For a whole-file target the two genuinely coincide."""
        p = self.write("answer.md", "the entire prompt\n")
        t = PromptFileTarget(p)
        self.assertEqual(t.context(), t.read())

    def test_summary_is_first_non_blank_line(self):
        p = self.write("answer.md", "\n\n  Real content here.\nmore\n")
        self.assertEqual(PromptFileTarget(p).summary(), "Real content here.")

    def test_summary_of_blank_file_is_empty(self):
        p = self.write("blank.md", "\n \n")
        self.assertEqual(PromptFileTarget(p).summary(), "")

    def test_write_restores_trailing_newline(self):
        """Diff noise suppression: the newline style should not flip."""
        p = self.write("answer.md", "original\n")
        PromptFileTarget(p).write("rewritten")
        self.assertEqual(p.read_text(), "rewritten\n")

    def test_write_does_not_add_newline_when_original_lacked_one(self):
        p = self.write("answer.md", "original")
        PromptFileTarget(p).write("rewritten")
        self.assertEqual(p.read_text(), "rewritten")

    def test_write_accepts_empty_text(self):
        p = self.write("answer.md", "original\n")
        PromptFileTarget(p).write("")
        self.assertEqual(p.read_text(), "")

    def test_write_leaves_no_temp_files_behind(self):
        p = self.write("answer.md", "original\n")
        PromptFileTarget(p).write("rewritten\n")
        self.assertEqual([q.name for q in self.tmp.iterdir()], ["answer.md"])

    def test_write_preserves_crlf_line_endings(self):
        """Rewriting a CRLF file must not rewrite every line of it.

        ``read_text`` normalises CRLF to LF, so a naive write-back turned
        the whole file into a diff and buried the one real edit.
        """
        p = self.tmp / "crlf.md"
        p.write_bytes(b"line one\r\nline two\r\n")
        t = PromptFileTarget(p)
        t.write(t.read())
        self.assertEqual(p.read_bytes(), b"line one\r\nline two\r\n")

    def test_write_preserves_lf_when_the_file_is_lf(self):
        p = self.tmp / "lf.md"
        p.write_bytes(b"line one\nline two\n")
        t = PromptFileTarget(p)
        t.write(t.read())
        self.assertEqual(p.read_bytes(), b"line one\nline two\n")

    def test_mixed_endings_normalise_to_the_majority(self):
        """A file with inconsistent endings is normalised, not preserved.

        Named for what it does rather than for "preservation": there is no
        faithful answer when the input is already inconsistent, and picking
        the majority at least makes the result stable under repeated
        writes — which is what the loop needs.
        """
        p = self.tmp / "mixed.md"
        p.write_bytes(b"a\nb\nc\r\n")
        t = PromptFileTarget(p)
        t.write(t.read())
        self.assertEqual(p.read_bytes(), b"a\nb\nc\n")

    def test_cr_only_endings_are_preserved(self):
        """Classic Mac files are rare, but getting them wrong costs the same.

        Every line would be rewritten and the one real edit lost in the
        diff — exactly the failure CRLF handling exists to prevent.
        """
        p = self.tmp / "cr.md"
        p.write_bytes(b"a\rb\r")
        t = PromptFileTarget(p)
        t.write(t.read())
        self.assertEqual(p.read_bytes(), b"a\rb\r")

    def test_candidate_carrying_crlf_does_not_produce_double_cr(self):
        """A model editing a CRLF file routinely echoes CRLF back.

        Without normalising the incoming text first, its CR survived and an
        LF was translated on top of it, yielding ``\\r\\r\\n``.
        """
        p = self.tmp / "lf.md"
        p.write_bytes(b"a\nb\n")
        PromptFileTarget(p).write("x\r\ny\r\n")
        self.assertEqual(p.read_bytes(), b"x\ny\n")

    def test_crlf_file_with_crlf_candidate_stays_crlf(self):
        p = self.tmp / "crlf.md"
        p.write_bytes(b"a\r\nb\r\n")
        PromptFileTarget(p).write("x\r\ny\r\n")
        self.assertEqual(p.read_bytes(), b"x\r\ny\r\n")

    def test_repr_does_not_raise(self):
        """A debugging aid that crashes is worse than none.

        This was hidden behind a coverage pragma, so a stale attribute
        reference survived a rename with100% reported coverage.
        """
        p = self.write("answer.md", "x\n")
        self.assertIn("answer", repr(PromptFileTarget(p)))

    def test_write_preserves_file_mode(self):
        """``mkstemp`` creates 0o600 and ``os.replace`` keeps that mode.

        Left alone, every write silently stripped the execute bit and any
        group or world permission from the artifact.
        """
        p = self.write("answer.md", "x\n")
        p.chmod(0o755)
        PromptFileTarget(p).write("y\n")
        self.assertEqual(p.stat().st_mode & 0o777, 0o755)

    def test_failed_write_cleans_up_temp_file(self):
        p = self.write("answer.md", "original\n")
        t = PromptFileTarget(p)
        with mock.patch("target.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                t.write("rewritten\n")
        self.assertEqual([q.name for q in self.tmp.iterdir()], ["answer.md"])
        self.assertEqual(p.read_text(), "original\n")


# ─────────────────────────────────────────────
# SectionTarget
# ─────────────────────────────────────────────

SECTIONED = """# Title

Intro text.

## Rules

Rule one.
Rule two.

### Sub-rule

Nested detail.

## Examples

An example.
"""


class SectionWriteValidationTests(TempDirTestCase):
    """The corruption class that makes SectionTarget worth having.

    A section is addressed by its heading and delimited by the next
    heading of the same or shallower level. A body that leaks a heading —
    or an unclosed fence, which hides every heading after it — moves that
    boundary, so the *next* write lands on a different span and deletes
    whatever was there. It happens between two writes, before the
    accept-or-revert commit, so git cannot undo it.
    """

    NEIGHBOURED = "# T\n\n## Rules\n\nbody\n\n## Other\n\nMUST-SURVIVE\n"

    def test_unclosed_fence_is_rejected(self):
        p = self.write("doc.md", self.NEIGHBOURED)
        with self.assertRaises(InvalidBody) as ctx:
            SectionTarget(p, "Rules").write("```sh\necho hi\n")
        self.assertIn("fence", str(ctx.exception))

    def test_a_file_with_an_unbalanced_fence_has_no_locatable_sections(self):
        """A fence opened in one section and closed in another.

        The file looks balanced overall but the section's own text is not,
        so replacing the body closed nothing: every later heading went
        invisible and the neighbour was deleted on the *first* write.
        Refusing to locate at all is the fix — returning a plausible-looking
        boundary is the dangerous option, because nothing downstream can
        tell a section that genuinely reaches the end of the file from one
        that only appears to.
        """
        p = self.write(
            "doc.md", "## Rules\n\n```sh\nx\n\n## Other\n\nMUST-SURVIVE\n"
        )
        with self.assertRaises(InvalidBody) as ctx:
            SectionTarget(p, "Rules")
        self.assertIn("unclosed code fence", str(ctx.exception))
        self.assertIn("MUST-SURVIVE", p.read_text())

    def test_write_that_would_make_another_section_ambiguous_is_rejected(self):
        """Addressability belongs to the file, not to one section.

        A deeper heading is legitimate in a body, but if another section
        already has a subsection of that name the title becomes
        duplicated — and a duplicated title can never be located again, so
        that section is permanently lost with no pre-commit state to
        revert to.
        """
        p = self.write(
            "doc.md", "# T\n\n## Rules\n\nbody\n\n## Other\n\n### Deep\n\nx\n"
        )
        t = SectionTarget(p, "Rules")
        with self.assertRaises(InvalidBody) as ctx:
            t.write("\n### Deep\n\nmine\n")
        self.assertIn("Deep", str(ctx.exception).replace("deep", "Deep"))
        SectionTarget(p, "Deep")  # still addressable; would raise otherwise

    def test_duplicate_detection_is_case_insensitive(self):
        """It must match how the locator compares titles, or it misses."""
        p = self.write(
            "doc.md", "# T\n\n## Rules\n\nbody\n\n## Other\n\n### Deep\n\nx\n"
        )
        with self.assertRaises(InvalidBody):
            SectionTarget(p, "Rules").write("\n### DEEP\n\nx\n")

    def test_preexisting_duplicates_do_not_block_an_unrelated_section(self):
        """Only *newly* createdambiguity is rejected.

        Refusing to work on a well-formed section because of someone
        else's pre-existing flaw elsewhere in the file would block
        legitimate work for no benefit — those sections are already
        unaddressable either way.
        """
        p = self.write("doc.md", "## Rules\n\nbody\n\n## Dup\n\na\n\n## Dup\n\nb\n")
        t = SectionTarget(p, "Rules")
        t.write("\nrewritten\n")
        self.assertEqual(t.read().strip(), "rewritten")

    def test_unclosed_fence_would_have_deleted_the_neighbour(self):
        """The regression itself: two writes used to erase the next section."""
        p = self.write("doc.md", self.NEIGHBOURED)
        t = SectionTarget(p, "Rules")
        with self.assertRaises(InvalidBody):
            t.write("```sh\necho hi\n")
        t.write("\nsecond iteration\n")
        self.assertIn("MUST-SURVIVE", p.read_text())
        self.assertIn("## Other", p.read_text())

    def test_same_level_heading_is_rejected(self):
        p = self.write("doc.md", self.NEIGHBOURED)
        with self.assertRaises(InvalidBody) as ctx:
            SectionTarget(p, "Rules").write("## Sneaky\n\nx\n")
        self.assertIn("re-delimits", str(ctx.exception))

    def test_shallower_heading_is_rejected(self):
        p = self.write("doc.md", self.NEIGHBOURED)
        with self.assertRaises(InvalidBody):
            SectionTarget(p, "Rules").write("# Top\n")

    def test_duplicate_of_own_heading_is_rejected(self):
        """Self-replication used to be caught only on the next read."""
        p = self.write("doc.md", self.NEIGHBOURED)
        with self.assertRaises(InvalidBody):
            SectionTarget(p, "Rules").write("## Rules\n\nx\n")

    def test_deeper_heading_is_allowed(self):
        """Subsections are part of the body by design."""
        p = self.write("doc.md", self.NEIGHBOURED)
        t = SectionTarget(p, "Rules")
        t.write("\n### Deeper\n\nfine\n")
        self.assertIn("### Deeper", t.read())
        self.assertIn("MUST-SURVIVE", p.read_text())

    def test_balanced_fence_containing_a_heading_is_allowed(self):
        """A heading inside a closed fence is example text, not a boundary."""
        p = self.write("doc.md", self.NEIGHBOURED)
        t = SectionTarget(p, "Rules")
        t.write("\n```md\n## Not A Real Heading\n```\n")
        self.assertIn("## Not A Real Heading", t.read())
        self.assertIn("MUST-SURVIVE", p.read_text())

    def test_rejected_write_leaves_the_file_byte_identical(self):
        p = self.write("doc.md", self.NEIGHBOURED)
        before = p.read_bytes()
        with self.assertRaises(InvalidBody):
            SectionTarget(p, "Rules").write("## Sneaky\n")
        self.assertEqual(p.read_bytes(), before)

    def test_rejected_write_leaves_no_temp_file(self):
        p = self.write("doc.md", self.NEIGHBOURED)
        with self.assertRaises(InvalidBody):
            SectionTarget(p, "Rules").write("```\n")
        self.assertEqual([q.name for q in self.tmp.iterdir()], ["doc.md"])

    def test_last_section_may_still_be_freely_rewritten(self):
        """Validation must not over-reach: no neighbour, still no headings."""
        p = self.write("doc.md", self.NEIGHBOURED)
        t = SectionTarget(p, "Other")
        t.write("\nrewritten tail\n")
        self.assertEqual(t.read().strip(), "rewritten tail")

    def test_a_preexisting_unclosed_fence_is_reported_not_worked_around(self):
        """The file has no determinable boundaries, so nothing is located."""
        p = self.write("doc.md", "```\n\n## Rules\n\nbody\n")
        with self.assertRaises(InvalidBody) as ctx:
            SectionTarget(p, "Rules")
        self.assertIn("unclosed code fence", str(ctx.exception))


class SectionTargetTests(TempDirTestCase):
    def test_read_returns_body_without_heading(self):
        p = self.write("doc.md", SECTIONED)
        body = SectionTarget(p, "Rules").read()
        self.assertNotIn("## Rules", body)
        self.assertIn("Rule one.", body)

    def test_section_includes_its_subsections(self):
        p = self.write("doc.md", SECTIONED)
        body = SectionTarget(p, "Rules").read()
        self.assertIn("### Sub-rule", body)
        self.assertIn("Nested detail.", body)

    def test_section_stops_at_next_same_level_heading(self):
        p = self.write("doc.md", SECTIONED)
        body = SectionTarget(p, "Rules").read()
        self.assertNotIn("An example.", body)

    def test_last_section_extends_to_end_of_file(self):
        p = self.write("doc.md", SECTIONED)
        self.assertIn("An example.", SectionTarget(p, "Examples").read())

    def test_write_replaces_only_that_section(self):
        p = self.write("doc.md", SECTIONED)
        SectionTarget(p, "Rules").write("\nRewritten rule.\n")
        after = p.read_text()
        self.assertIn("Rewritten rule.", after)
        self.assertNotIn("Rule one.", after)
        self.assertIn("Intro text.", after)
        self.assertIn("An example.", after)

    def test_heading_line_survives_a_write(self):
        """The heading is the target's address; a rewrite must not lose it."""
        p = self.write("doc.md", SECTIONED)
        t = SectionTarget(p, "Rules")
        t.write("replaced\n")
        self.assertIn("## Rules", p.read_text())
        self.assertEqual(t.read().strip(), "replaced")

    def test_write_then_read_is_stable_across_iterations(self):
        p = self.write("doc.md", SECTIONED)
        t = SectionTarget(p, "Rules")
        for i in range(3):
            t.write(f"\nversion {i}\n")
            self.assertEqual(t.read().strip(), f"version {i}")
        self.assertIn("An example.", p.read_text())

    def test_heading_inside_code_fence_is_not_a_section(self):
        p = self.write(
            "doc.md",
            "## Rules\n\n```sh\n# Examples\necho hi\n```\n\nStill rules.\n",
        )
        body = SectionTarget(p, "Rules").read()
        self.assertIn("Still rules.", body)
        with self.assertRaises(SectionNotFound):
            SectionTarget(p, "Examples")

    def test_tilde_fence_is_also_respected(self):
        p = self.write(
            "doc.md", "## Rules\n\n~~~\n## Examples\n~~~\n\nStill rules.\n"
        )
        self.assertIn("Still rules.", SectionTarget(p, "Rules").read())

    def test_leading_hashes_in_argument_are_tolerated(self):
        p = self.write("doc.md", SECTIONED)
        self.assertIn("Rule one.", SectionTarget(p, "## Rules").read())

    def test_match_is_case_insensitive(self):
        p = self.write("doc.md", SECTIONED)
        self.assertIn("Rule one.", SectionTarget(p, "rules").read())

    def test_missing_section_raises_and_lists_candidates(self):
        p = self.write("doc.md", SECTIONED)
        with self.assertRaises(SectionNotFound) as ctx:
            SectionTarget(p, "Nonexistent")
        self.assertIn("Rules", str(ctx.exception))

    def test_missing_section_in_headingless_file_says_none(self):
        p = self.write("doc.md", "just prose\n")
        with self.assertRaises(SectionNotFound) as ctx:
            SectionTarget(p, "Rules")
        self.assertIn("none", str(ctx.exception))

    def test_duplicate_section_is_rejected_not_guessed(self):
        p = self.write("doc.md", "## Rules\n\nA\n\n## Rules\n\nB\n")
        with self.assertRaises(AmbiguousSection) as ctx:
            SectionTarget(p, "Rules")
        self.assertIn("2 times", str(ctx.exception))

    def test_empty_section_argument_is_rejected(self):
        p = self.write("doc.md", SECTIONED)
        for bad in ("", "   ", "###"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                SectionTarget(p, bad)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            SectionTarget(self.tmp / "nope.md", "Rules")

    def test_name_combines_file_stem_and_section_slug(self):
        p = self.write("doc.md", SECTIONED)
        name = SectionTarget(p, "Rules").name
        self.assertTrue(name.startswith("doc-rules-"))

    def test_slug_collision_yields_distinct_workspaces(self):
        """``Rule Set`` and ``Rule/Set`` slugify identically.

        Sharing a workspace would make two independent optimizations
        overwrite each other's results, plan and best-version history,
        with nothing in the logs looking wrong.
        """
        p = self.write("doc.md", "## Rule Set\n\nA\n\n## Rule/Set\n\nB\n")
        a = SectionTarget(p, "Rule Set")
        b = SectionTarget(p, "Rule/Set")
        self.assertNotEqual(a.name, b.name)
        self.assertNotEqual(a.workspace, b.workspace)

    def test_name_is_stable_across_constructions(self):
        """The digest must be content-derived, not random per instance."""
        p = self.write("doc.md", SECTIONED)
        self.assertEqual(
            SectionTarget(p, "Rules").name, SectionTarget(p, "Rules").name
        )

    def test_non_ascii_section_still_gets_a_usable_name(self):
        p = self.write("doc.md", "## 规则\n\n内容\n")
        name = SectionTarget(p, "规则").name
        self.assertTrue(name.startswith("doc-"))
        self.assertGreater(len(name), len("doc-"))

    def test_two_non_ascii_sections_do_not_collide(self):
        """Both slugify to nothing, so only the digest separates them."""
        p = self.write("doc.md", "## 规则\n\nA\n\n## 示例\n\nB\n")
        self.assertNotEqual(
            SectionTarget(p, "规则").name, SectionTarget(p, "示例").name
        )

    def test_name_contains_no_path_separator(self):
        p = self.write("doc.md", "## a/b\n\nx\n")
        self.assertNotIn("/", SectionTarget(p, "a/b").name)

    def test_long_heading_yields_a_creatable_workspace(self):
        """A heading can be a whole sentence; a path component cannot.

        Uncapped, a long title produced a directory name over the
        filesystem limit and the workspace could not be created at all.
        Truncation is safe because uniqueness rests on the digest.
        """
        heading = "Detailed " * 40
        p = self.write("doc.md", f"## {heading.strip()}\n\nbody\n")
        t = SectionTarget(p, heading.strip())
        self.assertLess(len(t.name), 100)
        t.workspace.mkdir(parents=True)
        self.assertTrue(t.workspace.is_dir())

    def test_truncation_does_not_reintroduce_collisions(self):
        """Two long headings sharing a prefix must stay distinct."""
        prefix = "Extremely Detailed Instructions About "
        a, b = f"{prefix}Alpha", f"{prefix}Beta"
        p = self.write("doc.md", f"## {a}\n\nx\n\n## {b}\n\ny\n")
        self.assertNotEqual(SectionTarget(p, a).name, SectionTarget(p, b).name)

    def test_vcs_root_is_the_repository_not_the_directory(self):
        p = self.write("nested/doc.md", SECTIONED)
        (self.tmp / ".git").mkdir()
        self.assertEqual(SectionTarget(p, "Rules").vcs_root, self.tmp.resolve())

    def test_workspace_is_named_after_file_and_section(self):
        p = self.write("nested/doc.md", SECTIONED)
        t = SectionTarget(p, "Rules")
        self.assertEqual(t.workspace.parent, p.parent.resolve())
        self.assertEqual(t.workspace.name, f"{t.name}-workspace")
        self.assertTrue(t.workspace.name.startswith("doc-rules-"))

    def test_two_sections_of_one_file_get_separate_workspaces(self):
        """Optimizing two sections independently must not share state."""
        p = self.write("doc.md", SECTIONED)
        a = SectionTarget(p, "Rules").workspace
        b = SectionTarget(p, "Examples").workspace
        self.assertNotEqual(a, b)

    def test_snapshot_reports_section_and_its_share(self):
        p = self.write("doc.md", SECTIONED)
        snap = SectionTarget(p, "Rules").snapshot()
        self.assertEqual(snap["extra"]["section"], "Rules")
        self.assertLess(snap["chars"], snap["extra"]["file_chars"])
        self.assertGreater(snap["extra"]["share_of_file"], 0.0)
        self.assertLess(snap["extra"]["share_of_file"], 1.0)

    def test_snapshot_share_of_minimal_section_is_valid(self):
        """A heading with an empty body still yields a usable share.

        No zero-division guard is needed here: the presence of a heading
        guarantees the file is non-empty.
        """
        p = self.write("empty_heading.md", "## Rules\n")
        snap = SectionTarget(p, "Rules").snapshot()
        self.assertEqual(snap["chars"], 0)
        self.assertEqual(snap["extra"]["share_of_file"], 0.0)
        self.assertGreater(snap["extra"]["file_chars"], 0)

    def test_context_is_the_whole_file_not_just_the_section(self):
        """Surrounding instructions shape the behaviour being judged."""
        p = self.write("doc.md", SECTIONED)
        t = SectionTarget(p, "Rules")
        self.assertNotIn("An example.", t.read())
        self.assertIn("An example.", t.context())
        self.assertEqual(t.context(), p.read_text())

    def test_snapshot_reflects_the_file_as_it_is_now(self):
        """Metrics are recomputed per call, not cached at construction."""
        p = self.write("doc.md", SECTIONED)
        t = SectionTarget(p, "Rules")
        before = t.snapshot()["chars"]
        t.write("\ntiny\n")
        self.assertLess(t.snapshot()["chars"], before)


class SectionRelocationTests(TempDirTestCase):
    def test_section_deleted_underneath_the_target_is_reported(self):
        """Losing the anchor must fail loudly, not score an empty body.

        A silent empty read would look to the loop like a candidate that
        deleted all its own content and scored zero — attributing a
        harness fault to the candidate.
        """
        p = self.write("doc.md", "## Rules\n\nbody\n")
        t = SectionTarget(p, "Rules")
        p.write_text("")
        with self.assertRaises(SectionNotFound):
            t.snapshot()
        with self.assertRaises(SectionNotFound):
            t.read()

    def test_section_moving_within_the_file_is_followed(self):
        """Re-locating on every call is what makes this robust.

        Content added above the heading shifts its line number; the
        target must still address the right span.
        """
        p = self.write("doc.md", "## Rules\n\nbody\n\n## Other\n\nx\n")
        t = SectionTarget(p, "Rules")
        p.write_text("# New Title\n\nPreamble.\n\n" + p.read_text())
        self.assertEqual(t.read().strip(), "body")
        t.write("\nreplaced\n")
        self.assertIn("Preamble.", p.read_text())
        self.assertIn("## Other", p.read_text())
        self.assertEqual(t.read().strip(), "replaced")


# ─────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────

class HeadingSyntaxTests(TempDirTestCase):
    """What counts as a heading is a decision, so it is pinned down.

    The fence and heading patterns must agree on indentation tolerance;
    if they drifted apart, a heading the scanner accepted could sit
    inside a fence the scanner did not, and boundaries would be computed
    against two different notions of "start of line".
    """

    def test_closed_atx_heading_is_recognised(self):
        p = self.write("doc.md", "## Rules ##\n\nbody\n\n## Other ##\n\nx\n")
        self.assertEqual(SectionTarget(p, "Rules").read().strip(), "body")

    def test_closing_hashes_are_not_part_of_the_title(self):
        p = self.write("doc.md", "## Rules ##\n\nbody\n")
        SectionTarget(p, "Rules")  # would raise if the title were "Rules ##"

    def test_indented_heading_is_recognised_up_to_three_spaces(self):
        p = self.write("doc.md", "   ## Rules\n\nbody\n\n## Other\n\nx\n")
        self.assertEqual(SectionTarget(p, "Rules").read().strip(), "body")

    def test_four_space_indent_is_not_a_heading(self):
        """Four spaces is an indented code block, per CommonMark."""
        p = self.write("doc.md", "## Rules\n\n    ## Not A Heading\n\nbody\n")
        self.assertIn("## Not A Heading", SectionTarget(p, "Rules").read())

    def test_fence_and_heading_agree_on_indentation(self):
        """A 3-space fence must hide a 3-space heading."""
        p = self.write(
            "doc.md",
            "## Rules\n\n   ```\n   ## Hidden\n   ```\n\nstill rules\n",
        )
        body = SectionTarget(p, "Rules").read()
        self.assertIn("still rules", body)
        with self.assertRaises(SectionNotFound):
            SectionTarget(p, "Hidden")

    def test_setext_headings_are_deliberately_not_recognised(self):
        """``---`` is also frontmatter and a thematic break.

        Treating it as a heading would make a skill's frontmatter fence
        look like a section boundary, so such a file has no addressable
        sections and says so.
        """
        p = self.write("doc.md", "Rules\n=====\n\nbody\n")
        with self.assertRaises(SectionNotFound) as ctx:
            SectionTarget(p, "Rules")
        self.assertIn("none", str(ctx.exception))

    def test_frontmatter_delimiters_are_not_section_boundaries(self):
        p = self.write(
            "doc.md",
            "---\nname: x\n---\n\n## Rules\n\nbody\n\n## Other\n\nkeep\n",
        )
        self.assertEqual(SectionTarget(p, "Rules").read().strip(), "body")

    def test_hash_without_a_space_is_not_a_heading(self):
        """``#hashtag`` is prose, not a heading."""
        p = self.write("doc.md", "## Rules\n\n#hashtag stays\n")
        self.assertIn("#hashtag", SectionTarget(p, "Rules").read())

    def test_seven_hashes_is_not_a_heading(self):
        p = self.write("doc.md", "## Rules\n\n####### deep\n")
        self.assertIn("#######", SectionTarget(p, "Rules").read())


class SnapshotContractTests(TempDirTestCase):
    """Every shape must return the same keys.

    This is the test that keeps the future structural gate free of type
    branching. If one shape omitted a key, the gate would have to write
    ``if "share_of_file" in snap`` — a type branch wearing a dictionary
    lookup as a disguise — and the no-isinstance invariant would be lost
    without anything failing.
    """

    def all_shapes(self):
        skill = self.make_skill()
        (skill / "references").mkdir()
        (skill / "references" / "r.md").write_text("ref\n")
        prompt = self.write("plain.md", "a prompt\n")
        sectioned = self.write("doc.md", "## Rules\n\nbody\n\n## Other\n\nx\n")
        return [
            SkillTarget(skill),
            PromptFileTarget(prompt),
            SectionTarget(sectioned, "Rules"),
        ]

    def test_every_shape_returns_the_contract_keys(self):
        for t in self.all_shapes():
            with self.subTest(shape=type(t).__name__):
                snap = t.snapshot()
                self.assertTrue(set(SNAPSHOT_KEYS).issubset(snap))

    def test_contract_values_are_numbers(self):
        """A gate does arithmetic on these; a string would crash it."""
        for t in self.all_shapes():
            for key in SNAPSHOT_KEYS:
                with self.subTest(shape=type(t).__name__, key=key):
                    self.assertIsInstance(t.snapshot()[key], (int, float))

    def test_shape_specific_detail_stays_out_of_the_contract(self):
        for t in self.all_shapes():
            with self.subTest(shape=type(t).__name__):
                snap = t.snapshot()
                self.assertEqual(set(snap) - set(SNAPSHOT_KEYS) - {"extra"}, set())

    def test_a_gate_can_compare_any_two_shapes_without_branching(self):
        """The whole point, expressed as the gate's actual operation."""
        def grew(before, after):
            return {k: after[k] - before[k] for k in SNAPSHOT_KEYS}

        for t in self.all_shapes():
            with self.subTest(shape=type(t).__name__):
                before = t.snapshot()
                self.assertEqual(
                    grew(before, t.snapshot()),
                    {k: 0 for k in SNAPSHOT_KEYS},
                )

    def test_every_shape_implements_context(self):
        for t in self.all_shapes():
            with self.subTest(shape=type(t).__name__):
                self.assertIsInstance(t.context(), str)
                self.assertGreater(len(t.context()), 0)

    def test_context_is_never_smaller_than_the_mutable_text(self):
        """context() adds surrounding material; it never withholds any."""
        for t in self.all_shapes():
            with self.subTest(shape=type(t).__name__):
                self.assertGreaterEqual(len(t.context()), len(t.read()))


class ResolveTargetTests(TempDirTestCase):
    def test_directory_with_skill_md_becomes_skill_target(self):
        skill = self.make_skill()
        self.assertIsInstance(resolve_target(skill), SkillTarget)

    def test_file_becomes_prompt_file_target(self):
        p = self.write("answer.md", "x\n")
        self.assertIsInstance(resolve_target(p), PromptFileTarget)

    def test_file_with_section_becomes_section_target(self):
        p = self.write("doc.md", SECTIONED)
        self.assertIsInstance(resolve_target(p, "Rules"), SectionTarget)

    def test_section_on_a_skill_directory_is_rejected(self):
        skill = self.make_skill()
        with self.assertRaises(ValueError) as ctx:
            resolve_target(skill, "Rules")
        self.assertIn("SKILL.md", str(ctx.exception))

    def test_directory_without_skill_md_raises(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(FileNotFoundError):
            resolve_target(empty)

    def test_nonexistent_path_raises(self):
        with self.assertRaises(FileNotFoundError):
            resolve_target(self.tmp / "nope")

    def test_accepts_a_string_path(self):
        p = self.write("answer.md", "x\n")
        self.assertIsInstance(resolve_target(str(p)), PromptFileTarget)


if __name__ == "__main__":
    unittest.main()
