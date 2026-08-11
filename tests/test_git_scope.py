"""The scope of every version-control command the loop issues.

Why these tests use a real repository and no mocks
--------------------------------------------------
The bug they exist for was not a mistake in our logic. Our logic was
exactly what its author intended; the mistake was a belief about what
``git add -u`` does. It stages the whole working tree, not the current
directory's subtree — a Git 2.0 behaviour change that reads as trivia
and behaves as data loss:

    a user had uncommitted work in a file the loop never touched; the
    experiment commit swept it in; the gate rejected the candidate; the
    revert deleted the user's work. Afterwards it was in neither the
    working tree nor the index, so nothing could bring it back.

A mocked ``subprocess.run`` would assert that we called git the way we
meant to, which was never in doubt. It would encode the same wrong
belief into the test and pass. So every test here builds a real
repository with ``git init``, makes real commits, and inspects real
output — the only arrangement in which the test can disagree with us.

Committer identity is passed per-invocation with ``-c`` rather than
relying on ambient configuration, so the tests do not depend on (or
disturb) whatever the machine running them has configured.
"""

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "plugin" / "skills" / "skill-evolver" / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evolve_loop = _load_module("evolve_loop", SCRIPTS_DIR / "evolve_loop.py")
target_mod = _load_module("target", SCRIPTS_DIR / "target.py")
run_l1_gate_mod = _load_module("run_l1_gate", SCRIPTS_DIR / "run_l1_gate.py")


def git(args, cwd):
    """Run git with a fixed identity, returning the CompletedProcess."""
    return subprocess.run(
        ["git",
         "-c", "user.email=evolve-test@example.invalid",
         "-c", "user.name=Evolve Test",
         "-c", "commit.gpgsign=false"] + args,
        cwd=str(cwd), capture_output=True, text=True,
    )


class RealRepoTestCase(unittest.TestCase):
    """A repository laid out like the one the bug was found in.

        repo/
        ├── README.md            <- the user's, committed
        ├── prompts/answer.md    <- the artifact (a file target)
        ├── skill/SKILL.md       <- the artifact (a directory target)
        └── src/app.py           <- the user's, committed

    Both artifact shapes sit in subdirectories of a repository that also
    holds files the loop must never touch. That arrangement is the point:
    a repository containing only the artifact cannot exhibit the bug at
    all, which is why it went unnoticed.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve()

        (self.repo / "prompts").mkdir()
        (self.repo / "skill").mkdir()
        (self.repo / "src").mkdir()

        (self.repo / "README.md").write_text("# Project\noriginal readme\n")
        (self.repo / "prompts" / "answer.md").write_text(
            "You are a helpful assistant.\nAnswer concisely.\n")
        (self.repo / "skill" / "SKILL.md").write_text(
            "---\n"
            "name: demo-skill\n"
            "description: A demonstration skill used by the git scope tests "
            "to verify that commit and revert stay inside the artifact.\n"
            "---\n\n"
            "# Demo\n\n"
            "Body content that is comfortably long enough to satisfy the "
            "gate's minimum body length rule, which wants a couple of "
            "hundred characters so that a skill's entry point actually "
            "says something substantive rather than being a stub that "
            "happens to parse. This paragraph exists to clear that bar.\n"
        )
        (self.repo / "src" / "app.py").write_text("def main():\n    return 1\n")

        git(["init"], self.repo)
        git(["add", "-A"], self.repo)
        git(["commit", "-m", "initial commit"], self.repo)

    # ---- helpers ----

    def artifact_file(self) -> Path:
        return self.repo / "prompts" / "answer.md"

    def artifact_dir(self) -> Path:
        return self.repo / "skill"

    def status(self) -> str:
        return git(["status", "--porcelain"], self.repo).stdout

    def committed_paths(self, rev="HEAD") -> list[str]:
        out = git(["show", "--stat", "--name-only", "--format=", rev],
                  self.repo).stdout
        return sorted(p for p in out.strip().split("\n") if p.strip())

    def user_work_uncommitted(self):
        """Give the user unrelated uncommitted work of all three kinds.

        Staged, unstaged and untracked are covered together because they
        behave differently: ``git revert`` silently succeeds over an
        unstaged change, refuses outright when something is staged, and
        ignores an untracked file. A test that only dirtied the working
        tree would miss the refusal entirely.
        """
        (self.repo / "src" / "app.py").write_text(
            "def main():\n    return 'PRECIOUS UNCOMMITTED WORK'\n")
        git(["add", "--", "src/app.py"], self.repo)          # staged
        (self.repo / "README.md").write_text("# Project\nEDITED BY USER\n")  # unstaged
        (self.repo / "src" / "notes.txt").write_text("user scratch notes\n")  # untracked

    def assert_user_work_intact(self):
        self.assertIn("PRECIOUS UNCOMMITTED WORK",
                      (self.repo / "src" / "app.py").read_text(),
                      "the user's staged change was lost")
        self.assertIn("EDITED BY USER",
                      (self.repo / "README.md").read_text(),
                      "the user's unstaged change was lost")
        self.assertTrue((self.repo / "src" / "notes.txt").exists(),
                        "the user's untracked file was deleted")


class TheCommitContainsOnlyTheArtifact(RealRepoTestCase):
    """The invariant everything else rests on.

    Undoing a commit is only safe if the commit held nothing of anyone
    else's, so the guarantee has to be established when committing.
    """

    def test_a_file_target_commit_excludes_unrelated_modifications(self):
        self.user_work_uncommitted()
        self.artifact_file().write_text("Rewritten by the mutation.\n")

        result = evolve_loop.phase_4_commit(
            self.artifact_file(), "body", "rewrite the answer prompt")

        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(self.committed_paths(), ["prompts/answer.md"])

    def test_a_staged_unrelated_change_does_not_ride_along(self):
        """Narrowing ``git add`` is not sufficient, and this is why.

        ``git commit`` records the entire index, so a path the *user*
        staged before the run gets committed no matter how carefully we
        scoped our own staging. Only a pathspec on the commit itself
        excludes it.
        """
        (self.repo / "src" / "app.py").write_text("USER STAGED THIS\n")
        git(["add", "--", "src/app.py"], self.repo)
        self.artifact_file().write_text("Rewritten by the mutation.\n")

        result = evolve_loop.phase_4_commit(
            self.artifact_file(), "body", "rewrite the answer prompt")

        self.assertTrue(result["success"], result.get("error"))
        self.assertNotIn("src/app.py", self.committed_paths())
        self.assertIn("M  src/app.py", self.status(),
                      "the user's staged change should still be staged")

    def test_a_directory_target_commit_excludes_unrelated_modifications(self):
        self.user_work_uncommitted()
        skill_md = self.artifact_dir() / "SKILL.md"
        skill_md.write_text(skill_md.read_text() + "\nAn added line.\n")

        result = evolve_loop.phase_4_commit(
            self.artifact_dir(), "body", "extend the skill body")

        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(self.committed_paths(), ["skill/SKILL.md"])

    def test_a_new_file_inside_a_directory_target_is_committed(self):
        (self.artifact_dir() / "references").mkdir()
        new_rel = "skill/references/extra.md"
        (self.repo / new_rel).write_text("Supporting reference material.\n")

        result = evolve_loop.phase_4_commit(
            self.artifact_dir(), "script", "add a reference file",
            new_files=[new_rel])

        self.assertTrue(result["success"], result.get("error"))
        self.assertIn(new_rel, self.committed_paths())

    def test_a_new_file_outside_the_artifact_is_refused(self):
        """Offered as a mutation product, declined for lack of evidence.

        Phase 0 only verified the artifact was clean, so outside it there
        is no basis for believing a new file came from the mutation
        rather than from the user — and committing it would put it within
        reach of the next revert.
        """
        self.artifact_file().write_text("Rewritten by the mutation.\n")
        (self.repo / "src" / "sneaky.py").write_text("not ours\n")

        result = evolve_loop.phase_4_commit(
            self.artifact_file(), "body", "rewrite",
            new_files=["src/sneaky.py"])

        self.assertTrue(result["success"], result.get("error"))
        self.assertNotIn("src/sneaky.py", self.committed_paths())
        self.assertIn("?? src/sneaky.py", self.status())

    def test_a_traversal_path_is_refused(self):
        self.artifact_file().write_text("Rewritten by the mutation.\n")
        result = evolve_loop.phase_4_commit(
            self.artifact_file(), "body", "rewrite",
            new_files=["../outside.md", "/etc/passwd", ""])
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(self.committed_paths(), ["prompts/answer.md"])


class TheRevertLeavesEveryoneElseAlone(RealRepoTestCase):
    """The regression test for the destruction chain, end to end.

    Phase 0 → mutate → commit → revert, on a real repository, asserting
    that unrelated uncommitted work survives. This is the test that would
    have caught the original bug; nothing at the unit level could,
    because each individual command did exactly what it was told.
    """

    def test_a_file_target_survives_the_full_commit_revert_cycle(self):
        gt = self.repo / "evals.json"
        gt.write_text('[{"prompt": "hi", "assertions": [{"type": "contains", '
                      '"value": "hello"}]}]')

        # Phase 0 must accept a file artifact in a subdirectory. Before
        # the fix this raised NotADirectoryError via subprocess(cwd=<file>).
        setup = evolve_loop.phase_0_setup(
            self.artifact_file(), gt,
            workspace=self.repo.parent / "ws-file-target")
        self.assertIn("workspace", setup)

        self.user_work_uncommitted()
        self.artifact_file().write_text("A candidate rewrite.\n")

        commit = evolve_loop.phase_4_commit(
            self.artifact_file(), "body", "a candidate rewrite")
        self.assertTrue(commit["success"], commit.get("error"))
        self.assertEqual(self.committed_paths(), ["prompts/answer.md"])

        revert = evolve_loop.git_revert_last(self.artifact_file())
        self.assertTrue(revert["success"], revert.get("output"))

        # The artifact is back to its pre-experiment content...
        self.assertIn("You are a helpful assistant.",
                      self.artifact_file().read_text())
        # ...and this is the assertion the whole exercise is for.
        self.assert_user_work_intact()

    def test_revert_succeeds_even_though_the_user_has_staged_changes(self):
        """``git revert`` refuses here; the restore/commit pair does not.

        Measured behaviour of ``git revert HEAD`` with an unrelated
        staged change present: exit 128, ``your local changes would be
        overwritten by revert``, nothing reverted. The orchestrator
        treats a failed revert as grounds for aborting the run, so this
        is the difference between a loop that works over a user's normal
        working state and one that dies on its first discard.
        """
        self.artifact_file().write_text("A candidate rewrite.\n")
        commit = evolve_loop.phase_4_commit(
            self.artifact_file(), "body", "a candidate rewrite")
        self.assertTrue(commit["success"], commit.get("error"))

        (self.repo / "src" / "app.py").write_text("USER STAGED THIS\n")
        git(["add", "--", "src/app.py"], self.repo)

        revert = evolve_loop.git_revert_last(self.artifact_file())
        self.assertTrue(revert["success"], revert.get("output"))
        self.assertIn("You are a helpful assistant.",
                      self.artifact_file().read_text())
        self.assertEqual(
            "USER STAGED THIS\n", (self.repo / "src" / "app.py").read_text())

    def test_the_revert_deletes_a_file_the_experiment_added(self):
        (self.artifact_dir() / "references").mkdir()
        new_rel = "skill/references/extra.md"
        (self.repo / new_rel).write_text("Added by the mutation.\n")
        self.user_work_uncommitted()

        commit = evolve_loop.phase_4_commit(
            self.artifact_dir(), "script", "add a reference file",
            new_files=[new_rel])
        self.assertTrue(commit["success"], commit.get("error"))

        revert = evolve_loop.git_revert_last(self.artifact_dir())
        self.assertTrue(revert["success"], revert.get("output"))
        self.assertFalse((self.repo / new_rel).exists(),
                         "the experiment's own new file should be gone")
        self.assert_user_work_intact()

    def test_the_revert_records_an_inverse_commit(self):
        self.artifact_file().write_text("A candidate rewrite.\n")
        evolve_loop.phase_4_commit(self.artifact_file(), "body", "rewrite")
        before = git(["rev-parse", "HEAD"], self.repo).stdout.strip()

        evolve_loop.git_revert_last(self.artifact_file())

        after = git(["rev-parse", "HEAD"], self.repo).stdout.strip()
        self.assertNotEqual(before, after, "a revert must be recorded")
        self.assertEqual(self.committed_paths(), ["prompts/answer.md"])


class PhaseZeroChecksExactlyWhatItStages(RealRepoTestCase):
    """The dirty check and the staging scope must be the same set.

    Wider, and unrelated work anywhere in the user's repository refuses
    to let the loop start. Narrower, and something the commit can reach
    was never verified to be ours. Equality is the property that makes
    "the commit contains only the artifact" true.
    """

    def _gt(self) -> Path:
        gt = self.repo / "evals.json"
        gt.write_text('[{"prompt": "hi", "assertions": []}]')
        return gt

    def test_unrelated_dirt_does_not_block_the_run(self):
        self.user_work_uncommitted()
        setup = evolve_loop.phase_0_setup(
            self.artifact_file(), self._gt(),
            workspace=self.repo.parent / "ws-unrelated-dirt")
        self.assertIn("workspace", setup)

    def test_dirt_in_the_artifact_does_block_the_run(self):
        self.artifact_file().write_text("uncommitted edit by the user\n")
        with self.assertRaises(RuntimeError) as ctx:
            evolve_loop.phase_0_setup(
                self.artifact_file(), self._gt(),
                workspace=self.repo.parent / "ws-artifact-dirt")
        self.assertIn("uncommitted changes", str(ctx.exception))

    def test_dirt_inside_a_directory_artifact_blocks_the_run(self):
        (self.artifact_dir() / "SKILL.md").write_text("edited\n")
        with self.assertRaises(RuntimeError):
            evolve_loop.phase_0_setup(
                self.artifact_dir(), self._gt(),
                workspace=self.repo.parent / "ws-dir-dirt")


class ListingUntrackedFilesDistinguishesFailureFromEmptiness(RealRepoTestCase):
    """An empty set must mean "nothing untracked", never "could not look".

    The two were indistinguishable before, and their consequences are
    opposite: the honest empty set means the mutation added no files,
    while the swallowed error meant every file the mutation added was
    left out of the commit — with the log still reporting success, so the
    loop went on scoring a candidate whose new files were never saved.
    """

    def test_an_untracked_file_in_the_artifact_is_reported(self):
        (self.artifact_dir() / "references").mkdir()
        (self.artifact_dir() / "references" / "new.md").write_text("x\n")
        self.assertEqual(
            evolve_loop._list_untracked(self.artifact_dir()),
            {"skill/references/new.md"})

    def test_a_clean_artifact_reports_the_empty_set(self):
        self.assertEqual(evolve_loop._list_untracked(self.artifact_dir()), set())

    def test_untracked_files_outside_the_artifact_are_not_reported(self):
        (self.repo / "src" / "user_new.py").write_text("theirs\n")
        self.assertEqual(evolve_loop._list_untracked(self.artifact_dir()), set())

    def test_a_missing_artifact_raises_instead_of_reporting_emptiness(self):
        with self.assertRaises((RuntimeError, FileNotFoundError)):
            evolve_loop._list_untracked(self.repo / "no" / "such" / "place")

    def test_an_empty_directory_and_a_missing_one_are_distinguishable(self):
        """Both would have returned ``set()`` before; only one may now."""
        empty = self.repo / "skill" / "references"
        empty.mkdir()
        self.assertEqual(evolve_loop._list_untracked(self.artifact_dir()), set())

        with self.assertRaises((RuntimeError, FileNotFoundError)):
            evolve_loop._list_untracked(self.repo / "skill" / "absent")


class TheVersionControlPathspecIsRepositoryRelative(RealRepoTestCase):
    """Pathspecs and git's own output must use one representation.

    Absolute paths work for ``git add`` and then quietly fail to match
    anything when compared against ``status --porcelain`` or ``ls-files``
    output, which git prints relative to the repository root.
    """

    def test_a_file_target_reports_its_path_relative_to_the_repository(self):
        t = target_mod.resolve_target(self.artifact_file())
        self.assertEqual(t.vcs_root, self.repo)
        self.assertEqual(t.vcs_pathspec, "prompts/answer.md")

    def test_a_directory_target_reports_its_path_relative_to_the_repository(self):
        t = target_mod.resolve_target(self.artifact_dir())
        self.assertEqual(t.vcs_root, self.repo)
        self.assertEqual(t.vcs_pathspec, "skill")

    def test_an_artifact_that_is_the_repository_root_uses_dot(self):
        (self.repo / "SKILL.md").write_text(
            "---\nname: root-skill\ndescription: "
            + "A skill whose directory is itself the repository root, which "
            "is the case where a relative pathspec would come out empty.\n"
            "---\n\nBody.\n")
        t = target_mod.resolve_target(self.repo)
        self.assertEqual(t.vcs_pathspec, ".")

    def test_the_scope_question_is_answered_by_the_target(self):
        self.assertTrue(target_mod.resolve_target(self.artifact_dir())
                        .vcs_scope_is_tree)
        self.assertFalse(target_mod.resolve_target(self.artifact_file())
                         .vcs_scope_is_tree)


class TheBestVersionArchiveWorksForEveryShape(RealRepoTestCase):
    """A file target must be archivable and restorable.

    ``shutil.copytree`` raises ``NotADirectoryError`` on a file, so the
    archive of best-scoring versions — the only record of what actually
    worked — could not be written at all for a prompt file.
    """

    def test_a_file_artifact_is_saved_and_can_be_restored(self):
        ws = self.repo.parent / "ws-archive-file"
        original = self.artifact_file().read_text()

        saved = Path(evolve_loop.save_best_version(self.artifact_file(), ws, 3))

        self.assertTrue(saved.is_file(), f"{saved} should be a file")
        self.assertEqual(saved.read_text(), original)
        self.assertEqual(saved.suffix, ".md",
                         "the extension must survive so the copy is restorable")

        # Restorable: overwrite the artifact, copy the archive back.
        self.artifact_file().write_text("a later, worse candidate\n")
        self.artifact_file().write_text(saved.read_text())
        self.assertEqual(self.artifact_file().read_text(), original)

    def test_a_directory_artifact_is_saved_without_its_git_directory(self):
        ws = self.repo.parent / "ws-archive-dir"
        saved = Path(evolve_loop.save_best_version(self.artifact_dir(), ws, 1))
        self.assertTrue((saved / "SKILL.md").is_file())
        self.assertFalse((saved / ".git").exists())

    def test_saving_twice_replaces_the_earlier_copy(self):
        ws = self.repo.parent / "ws-archive-twice"
        evolve_loop.save_best_version(self.artifact_file(), ws, 5)
        self.artifact_file().write_text("second version\n")
        saved = Path(evolve_loop.save_best_version(self.artifact_file(), ws, 5))
        self.assertEqual(saved.read_text(), "second version\n")


class TheLevelOneGateJudgesTheArtifactItWasGiven(RealRepoTestCase):
    """The gate must check a file target, not look for a skill inside it.

    It used to fail every file artifact with ``SKILL.md not found``, and
    since ``run_evolve_loop`` aborts on an L1 failure, the loop could not
    start on a prompt file at all. Note that the fix is not "skip the
    checks for shapes I don't recognise" — a gate that waves through what
    it cannot classify is a decoration.
    """

    def _csv_dataset(self) -> Path:
        gt = self.repo / "cases.csv"
        gt.write_text(
            "id,prompt,expectations\n"
            "1,What is the capital of France?,Paris\n"
            "2,Name a primary colour,red|blue|yellow\n"
        )
        return gt

    def test_a_file_target_with_a_csv_dataset_passes(self):
        result = run_l1_gate_mod.run_l1_gate(
            self.artifact_file(), self._csv_dataset())
        self.assertTrue(result["pass"], result["errors"])

    def test_a_file_target_with_a_json_dataset_passes(self):
        gt = self.repo / "evals.json"
        gt.write_text('[{"prompt": "hi", "assertions": '
                      '[{"type": "contains", "value": "hello"}]}]')
        result = run_l1_gate_mod.run_l1_gate(self.artifact_file(), gt)
        self.assertTrue(result["pass"], result["errors"])

    def test_a_skill_target_still_passes(self):
        result = run_l1_gate_mod.run_l1_gate(
            self.artifact_dir(), self._csv_dataset())
        self.assertTrue(result["pass"], result["errors"])

    def test_an_empty_file_target_fails(self):
        """The checks are real, not waved through for unfamiliar shapes."""
        blank = self.repo / "prompts" / "blank.md"
        blank.write_text("   \n\n")
        result = run_l1_gate_mod.run_l1_gate(blank, self._csv_dataset())
        self.assertFalse(result["pass"])
        self.assertTrue(any("no content" in e for e in result["errors"]),
                        result["errors"])

    def test_a_secret_in_a_file_target_still_blocks_the_gate(self):
        """Security rules apply whatever the artifact's shape.

        These used to be skipped entirely for a file target, because the
        scan returned early when SKILL.md was absent — reporting no
        findings, which reads identically to "scanned and clean".
        """
        leaky = self.repo / "prompts" / "leaky.md"
        leaky.write_text(
            "Use this key to authenticate:\n\n"
            "AKIAIOSFODNN7EXAMPLE\n\n"
            "Then proceed with the request as normal.\n")
        result = run_l1_gate_mod.run_l1_gate(leaky, self._csv_dataset())
        self.assertFalse(result["pass"])
        self.assertTrue(any("SEC003" in e for e in result["errors"]),
                        result["errors"])

    def test_an_unreadable_dataset_is_reported_as_such(self):
        bad = self.repo / "cases.json"
        bad.write_text("this is not json at all")
        result = run_l1_gate_mod.run_l1_gate(self.artifact_file(), bad)
        self.assertFalse(result["pass"])
        self.assertTrue(any("GT" in e for e in result["errors"]),
                        result["errors"])


class TheEngineHasNoTypeBranches(unittest.TestCase):
    """Shape differences are answered by the target, never inspected.

    ``target.py`` exists to remove type dispatch from the engine, so a
    fix that reintroduced ``isinstance`` in a phase function would undo
    the reason the module is there — while passing every behavioural test
    above.
    """

    ENGINE_FILES = ("evolve_loop.py", "orchestrator.py", "run_l1_gate.py")

    def test_no_engine_file_dispatches_on_target_type(self):
        for name in self.ENGINE_FILES:
            source = (SCRIPTS_DIR / name).read_text()
            for shape in ("SkillTarget", "PromptFileTarget", "SectionTarget"):
                self.assertNotIn(
                    f"isinstance(target, {shape}", source,
                    f"{name} dispatches on {shape}; add a polymorphic "
                    f"method to Target instead")


class EverySnapshotCarriesTheSameKeys(unittest.TestCase):
    """The uniform contract the gates depend on, re-checked here.

    A gate testing whether a key exists is a type branch wearing a
    dictionary lookup as a disguise, so the key set must not vary by
    shape — including for the shapes this change touched.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def test_all_shapes_agree_on_the_snapshot_key_set(self):
        skill = self.tmp / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: s\ndescription: d\n---\n\n## Rules\n\nbody\n")
        prompt = self.tmp / "p.md"
        prompt.write_text("## Rules\n\nbody\n")

        expected = set(target_mod.SNAPSHOT_KEYS)
        for t in (target_mod.SkillTarget(skill),
                  target_mod.PromptFileTarget(prompt),
                  target_mod.SectionTarget(prompt, "Rules")):
            with self.subTest(shape=type(t).__name__):
                self.assertEqual(
                    expected, set(t.snapshot()) - {"extra"})


if __name__ == "__main__":
    unittest.main()
