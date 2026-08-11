"""Invariants that a passing test suite does not imply.

Every bug these guard against was present while the whole suite was green.
That is the point: each function behaved as its own tests said, and the
fault lay in a contract *between* them — which no per-function test is
positioned to see.

The engine already had six such invariants (one JSON extractor, no division
in `graders`, no project vocabulary in the core, no branching on target
type, `scoring` pure, imports one-directional). All six held while a bug
that deletes the user's uncommitted work sat in `phase_4_commit`, because
none of them said anything about how git is invoked. These are the ones
added after that.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugin/skills/skill-evolver/scripts"
sys.path.insert(0, str(SCRIPTS))

#: Git subcommands that change the repository. A pathspec confines them to
#: the artifact; without one their scope is whatever git chooses, which is
#: not the same thing for all of them — `add -u` takes the whole work tree
#: while `checkout -- .` takes the current directory. That asymmetry is what
#: made the original bug hard to see.
MUTATING = {"add", "commit", "checkout", "restore", "rm", "clean", "reset"}


def _modules() -> dict[str, ast.Module]:
    return {p.name: ast.parse(p.read_text()) for p in sorted(SCRIPTS.glob("*.py"))}


def _git_calls(tree: ast.Module):
    """Yield (call, argv) for every subprocess invocation of git.

    Recognises both a literal ``["git", ...]`` and a concatenation such as
    ``["git"] + args``, because the engine uses the second form in its
    helper.

    A call whose command cannot be read statically is skipped rather than
    reported. The first version of this yielded those too, and immediately
    flagged two calls that run a user-supplied pytest command and a model
    CLI — neither of them git. A checker that cries wolf on unrelated code
    gets switched off, which costs more than the cases it would have caught.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = ast.unparse(node.func)
        if target not in ("subprocess.run", "subprocess.check_output"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        parts = first.left if isinstance(first, ast.BinOp) else first
        if not isinstance(parts, (ast.List, ast.Tuple)) or not parts.elts:
            continue
        head = parts.elts[0]
        if not (isinstance(head, ast.Constant) and head.value == "git"):
            continue
        argv = [e.value for e in parts.elts if isinstance(e, ast.Constant)]
        yield node, argv


class GitRunsAtTheRepositoryRoot(unittest.TestCase):
    """I7 — every git call takes its working directory from the target.

    `subprocess.run(cwd=...)` was handed `skill_path` directly. For a skill
    that is the repository, so it worked; for a single file it raised
    NotADirectoryError, and for a file inside a larger repository it would
    have run git one directory below the root, where paths in git's output
    no longer line up with the paths being passed in.

    `Target.vcs_root` existed for this, with a docstring explaining the
    hazard, and had zero call sites. An abstraction nobody calls is
    indistinguishable from one that was never written, and nothing in the
    suite could tell the difference.
    """

    def test_no_git_call_uses_a_bare_path_as_cwd(self):
        offenders = []
        for name, tree in _modules().items():
            for call, _argv in _git_calls(tree):
                cwd = next((k.value for k in call.keywords if k.arg == "cwd"), None)
                if cwd is None:
                    continue
                expr = ast.unparse(cwd)
                if "vcs_root" not in expr:
                    offenders.append(f"{name}:{call.lineno}: cwd={expr}")
        self.assertEqual(
            offenders, [],
            "git must run at Target.vcs_root, not at a path the caller "
            "happened to have:\n  " + "\n  ".join(offenders),
        )


class GitWritesAreConfinedToTheArtifact(unittest.TestCase):
    """I8 — every mutating git command carries a pathspec.

    `git add -u` with no pathspec stages the entire work tree regardless of
    cwd, and a plain `git commit` records the whole index including whatever
    the user staged themselves. A discarded iteration then reverted that
    commit, taking the user's unrelated edit out of both the working tree
    and the index.

    Checked structurally rather than by naming the commands that went wrong,
    so a future `git rm` or `git clean` is covered without anyone
    remembering to add it here.
    """

    def test_every_mutating_command_is_scoped(self):
        offenders = []
        for name, tree in _modules().items():
            for call, argv in _git_calls(tree):
                sub = next((a for a in argv[1:] if not a.startswith("-")), None)
                if sub not in MUTATING:
                    continue
                if "--" not in argv:
                    offenders.append(
                        f"{name}:{call.lineno}: git {sub} without a pathspec")
        self.assertEqual(
            offenders, [],
            "a mutating git command without `--` acts on more than the "
            "artifact:\n  " + "\n  ".join(offenders),
        )

    def test_the_check_would_have_caught_the_original_bug(self):
        """Proof the rule is not vacuous.

        A rule that passes on the code it was written to reject is
        decoration. This reconstructs the exact call that shipped and
        asserts the checker rejects it.
        """
        tree = ast.parse(
            'subprocess.run(["git", "add", "-u"], cwd=str(skill_path))\n'
            'subprocess.run(["git", "commit", "-m", msg], cwd=str(skill_path))\n'
        )
        unscoped = []
        for call, argv in _git_calls(tree):
            sub = next((a for a in argv[1:] if not a.startswith("-")), None)
            if sub in MUTATING and "--" not in argv:
                unscoped.append(sub)
        self.assertEqual(sorted(unscoped), ["add", "commit"])

        cwds = [
            ast.unparse(next(k.value for k in call.keywords if k.arg == "cwd"))
            for call, _ in _git_calls(tree)
        ]
        self.assertTrue(all("vcs_root" not in c for c in cwds))


class TargetIsActuallyUsed(unittest.TestCase):
    """I9 — every public member of the Target abstraction has a caller.

    The most useful of these, because it needs no knowledge of any specific
    accident. `vcs_root` was written, documented, and never called; so was
    `vcs_pathspec`. The failure mode is general — an abstraction added
    alongside the code it was meant to replace, with the old path left in
    place — and this catches it by shape.

    It also explains a false reassurance: the invariant "the engine never
    branches on target type" held trivially while the engine held no Target
    at all. An unused abstraction satisfies every rule about how it must be
    used.
    """

    def test_no_public_target_member_is_unused(self):
        from target import Target

        tree = ast.parse((SCRIPTS / "target.py").read_text())
        members = {
            node.name
            for cls in ast.walk(tree)
            if isinstance(cls, ast.ClassDef) and cls.name == "Target"
            for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.name.startswith("_")
        }
        self.assertTrue(members, "found no public members to check")

        used: set[str] = set()
        for name, other in _modules().items():
            if name == "target.py":
                continue
            for node in ast.walk(other):
                if isinstance(node, ast.Attribute) and node.attr in members:
                    used.add(node.attr)

        unused = sorted(members - used)
        self.assertEqual(
            unused, [],
            f"defined on Target but called nowhere outside target.py: "
            f"{unused}. Either the engine still does this another way, or "
            f"the member should go.",
        )


class ImportDirectionIsDeliberate(unittest.TestCase):
    """I10 — the cycle-breaking import placement is not accidental.

    `grader_evaluator` imports `evaluators` at module level; `evaluators`
    imports it back inside a function. That is a genuine cycle, broken by
    placement rather than by structure, and it works. What makes it fragile
    is that nothing said so: an editor's "optimise imports" hoisting the
    lazy import to the top produces an ImportError at load time, far from
    whatever was being edited.

    Stated as a rule so the arrangement is a decision on record instead of
    a coincidence.
    """

    LEAVES = ("grader_evaluator.py", "evaluator_backends.py")

    def test_leaves_import_the_protocol_at_module_level(self):
        for name in self.LEAVES:
            tree = ast.parse((SCRIPTS / name).read_text())
            top = {
                n.module
                for n in tree.body
                if isinstance(n, ast.ImportFrom)
            }
            self.assertIn(
                "evaluators", top,
                f"{name} must import evaluators at module level",
            )

    def test_the_factory_imports_the_leaves_lazily(self):
        tree = ast.parse((SCRIPTS / "evaluators.py").read_text())
        hoisted = [
            n.module for n in tree.body
            if isinstance(n, ast.ImportFrom) and n.module in
            ("grader_evaluator", "evaluator_backends")
        ]
        self.assertEqual(
            hoisted, [],
            "evaluators.py must import these inside functions; at module "
            "level the cycle closes and every import of either fails",
        )


class DocumentedSettingsReachTheCode(unittest.TestCase):
    """I11 — a CLI choice list must come from the registry.

    `--evaluator` hard-coded four names while `get_evaluator` supported six
    and SKILL.md documented one of the missing two. `--evaluator grader` was
    rejected by argparse with exit 2: a supported backend unreachable from
    the CLI that documents it.
    """

    def test_the_evaluator_flag_offers_every_registered_backend(self):
        from evaluators import EVALUATOR_NAMES

        tree = ast.parse((SCRIPTS / "orchestrator.py").read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not ast.unparse(node.func).endswith("add_argument"):
                continue
            if not any(isinstance(a, ast.Constant) and a.value == "--evaluator"
                       for a in node.args):
                continue
            choices = next(
                (k.value for k in node.keywords if k.arg == "choices"), None)
            self.assertIsNotNone(choices, "--evaluator lost its choices=")
            if isinstance(choices, (ast.List, ast.Tuple)):
                literals = [e.value for e in choices.elts
                            if isinstance(e, ast.Constant)]
                self.assertCountEqual(
                    literals, list(EVALUATOR_NAMES),
                    "the literal list has drifted from EVALUATOR_NAMES; "
                    "reference the constant instead",
                )
            else:
                self.assertIn("EVALUATOR_NAMES", ast.unparse(choices))
            return
        self.fail("no --evaluator argument found")


class EveryBackendReportsWhatTheGateReads(unittest.TestCase):
    """I12 — the gate cannot enforce a threshold nobody measured.

    `max_structure` was compared against a `snapshot` key that five of six
    backends never produced, and `check_structure` reads a missing snapshot
    as "no signal", which passes. So the setting was parsed, logged, and
    silently inert. `trigger_f1` had the opposite problem: hard-coded to
    1.0, so the check reported a pass it had never performed.
    """

    def test_full_eval_is_a_template_the_backends_cannot_bypass(self):
        from evaluators import Evaluator

        base = Evaluator.full_eval
        for module in ("evaluators", "evaluator_backends", "grader_evaluator"):
            mod = __import__(module)
            for name in dir(mod):
                obj = getattr(mod, name)
                if not (isinstance(obj, type) and issubclass(obj, Evaluator)):
                    continue
                if obj is Evaluator:
                    continue
                self.assertIs(
                    obj.full_eval, base,
                    f"{obj.__name__} overrides full_eval; it should implement "
                    f"_run_full_eval so the snapshot is attached for it",
                )

    def test_the_gate_is_not_handed_constants(self):
        """A metric filled in by the caller measures nothing.

        `trigger_f1: 1.0` sat in the call to the gate while the evaluator
        computed the real figure and went unread — which also defeated the
        gate's own "was this measured?" check, since a constant is present.
        """
        tree = ast.parse((SCRIPTS / "orchestrator.py").read_text())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not ast.unparse(node.func).endswith("phase_6_gate_decision"):
                continue
            for arg in node.args:
                if not isinstance(arg, ast.Dict):
                    continue
                for key, value in zip(arg.keys, arg.values):
                    if not isinstance(key, ast.Constant):
                        continue
                    if key.value in ("trigger_f1", "regression_pass") and \
                            isinstance(value, ast.Constant):
                        offenders.append(
                            f"line {node.lineno}: {key.value}={value.value!r}")
        self.assertEqual(
            offenders, [],
            "these must be forwarded only when measured:\n  "
            + "\n  ".join(offenders),
        )


class TheCaseWriterStaysALeaf(unittest.TestCase):
    """I13 — `case_store` imports nothing from this package.

    That is the whole reason it exists as a module. The function used to
    live in `evolve_loop`, and four modules imported it from there — a
    two-line file-writing helper pulling in eleven modules and 78 ms, with
    `evolve_loop` importing `evaluators` straight back. Each of those four
    imports therefore had to sit inside a function body, and hoisting any
    one of them to the top of its file raised `ImportError` at load time
    (verified: "cannot import name 'get_evaluator' from partially
    initialized module 'evaluators'", reported from `aggregate_results`,
    which is neither the file edited nor the helper wanted).

    Keeping this module dependency-free is what lets those four import it
    at the top like anything else, so there is nothing left for an
    editor's "optimise imports" to break. A single import added here would
    quietly restore the hazard, which is why it is checked rather than
    merely intended.
    """

    def test_it_has_no_intra_package_imports(self):
        names = {p.stem for p in SCRIPTS.glob("*.py")}
        tree = ast.parse((SCRIPTS / "case_store.py").read_text())
        found = sorted(
            n.module for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module in names
        )
        self.assertEqual(
            found, [],
            f"case_store must stay a leaf; it now imports {found}. Its "
            f"callers import it at module level, which only works while it "
            f"depends on nothing here.",
        )

    def test_its_callers_import_it_at_module_level(self):
        """A function-level import here means someone hit a cycle again."""
        for name in ("evaluators.py", "evaluator_backends.py",
                     "grader_evaluator.py", "evolve_loop.py"):
            tree = ast.parse((SCRIPTS / name).read_text())
            top = [n for n in tree.body
                   if isinstance(n, ast.ImportFrom) and n.module == "case_store"]
            nested = [n for n in ast.walk(tree)
                      if isinstance(n, ast.ImportFrom)
                      and n.module == "case_store" and n not in tree.body]
            self.assertEqual(
                len(top), 1, f"{name} should import case_store at module level")
            self.assertEqual(
                nested, [],
                f"{name} imports case_store inside a function; that is the "
                f"symptom the split was meant to remove",
            )


if __name__ == "__main__":
    unittest.main()
