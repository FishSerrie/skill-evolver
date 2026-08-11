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
    spec.loader.exec_module(module)
    return module


orchestrator = _load_module("skill_evolver_orchestrator", SCRIPTS_DIR / "orchestrator.py")


def _run_git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com",
                        "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})


class GitDiffForCommitTests(unittest.TestCase):
    """Phase 6.5 reviews what a candidate actually changed, not a
    description of the change — this is the helper that fetches that
    diff for verifier_panel.build_verifier_task_spec / phase_6_5_review."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.skill_path = Path(self._tmp.name)
        _run_git(["init"], self.skill_path)
        (self.skill_path / "SKILL.md").write_text("# Test\noriginal\n")
        _run_git(["add", "."], self.skill_path)
        _run_git(["commit", "-m", "init"], self.skill_path)

    def test_returns_diff_content_of_the_named_commit(self):
        (self.skill_path / "SKILL.md").write_text("# Test\nmodified\n")
        _run_git(["commit", "-am", "experiment(body): change a line"], self.skill_path)
        commit_hash = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(self.skill_path),
            capture_output=True, text=True).stdout.strip()

        diff = orchestrator._git_diff_for_commit(self.skill_path, commit_hash)
        self.assertIn("-original", diff)
        self.assertIn("+modified", diff)

    def test_returns_empty_string_for_commit_with_no_parent(self):
        commit_hash = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(self.skill_path),
            capture_output=True, text=True).stdout.strip()

        diff = orchestrator._git_diff_for_commit(self.skill_path, commit_hash)
        self.assertEqual(diff, "")

    def test_returns_empty_string_for_invalid_commit_hash(self):
        diff = orchestrator._git_diff_for_commit(self.skill_path, "not-a-real-hash")
        self.assertEqual(diff, "")


if __name__ == "__main__":
    unittest.main()
