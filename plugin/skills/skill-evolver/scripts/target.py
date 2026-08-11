"""What is being optimized.

Single responsibility: give the loop a uniform way to read, write and
measure the artifact under optimization — without the loop knowing
whether that artifact is a skill directory, a standalone prompt file, or
one section inside a larger file.

Why this module exists
----------------------
The engine used to take a skill directory as its only possible subject.
Every phase then quietly assumed "the thing being changed is a skill",
which is why a plain prompt file could not be optimized at all. Yet none
of the interesting machinery — the phase loop, the gates, the
commit/revert discipline, the proposer/reviewer separation — actually
depends on that assumption. The dependency was incidental, not
essential.

So this is a dependency inversion, not a rewrite: the engine now depends
on the three operations it truly needs (read, write, measure), and the
knowledge of "what kind of artifact this is" lives here, behind
polymorphism.

Why subclasses instead of a ``kind`` field
------------------------------------------
Reading a whole file and reading one section of a file are not the same
algorithm — the latter has to locate boundaries and write back without
disturbing its neighbours. A ``kind`` field would push that difference
into ``if kind == ...`` branches inside every method, which is
conditional logic wearing polymorphism's clothes. With subclasses the
engine only ever makes a polymorphic call, and adding a fourth artifact
shape touches no existing code.

Stdlib only. No model calls. The only IO is against the target's own
files, which is this module's entire job.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

__all__ = [
    "Target",
    "SkillTarget",
    "PromptFileTarget",
    "SectionTarget",
    "SNAPSHOT_KEYS",
    "SectionNotFound",
    "AmbiguousSection",
    "InvalidBody",
    "resolve_target",
]


class SectionNotFound(ValueError):
    """No heading in the file matches the requested section."""


class AmbiguousSection(ValueError):
    """More than one heading matches the requested section.

    Raised rather than defaulting to the first match: silently picking
    one would mean the loop spends its whole budget rewriting a section
    the operator never intended to touch, and the logs would look
    perfectly normal while it happened.
    """


class InvalidBody(ValueError):
    """A candidate body would corrupt the file it is written into.

    Raised *before* anything is written. A section is addressed by its
    heading and delimited by the next heading of the same or shallower
    level, so a body that leaks a heading — or an unbalanced code fence,
    which makes every later heading invisible — silently moves the
    section's own boundary. The next write then lands on a different span
    of the file and deletes whatever was there.

    That failure is unrecoverable by the loop's usual safety net: it
    happens between two writes, before the accepted-or-reverted commit,
    so there is no earlier state to revert to. Hence the check is a
    precondition on writing rather than a validation after the fact, and
    an offending candidate is rejected as malformed rather than scored.
    """


# ─────────────────────────────────────────────
# Structural metrics
# ─────────────────────────────────────────────

# The keys every snapshot must carry, whatever the target's shape. Named
# so that a gate can assert the contract instead of trusting it.
SNAPSHOT_KEYS =("chars", "lines", "non_empty_lines", "child_units", "child_lines")


def _text_metrics(text: str) -> dict:
    """Size metrics for one block of text.

    Lives at module level rather than as a base-class method because it
    needs no instance state; a shape reporting on two different blocks
    (see :class:`SectionTarget`) can call it twice.

    ``chars`` and ``lines`` are both reported because they catch
    different kinds of bloat: a candidate can hold its line count steady
    while doubling the length of every line.

    ``lines`` counts newline-terminated lines, so ``"a\\n"`` is 1 rather
    than 2 — a file with one line of text should not measure as two, or
    every gate comparing line counts inherits a systematic off-by-one.
    """
    stripped = text[:-1] if text.endswith("\n") else text
    return {
        "chars": len(text),
        "lines": stripped.count("\n") + 1 if stripped else 0,
        "non_empty_lines": sum(1 for ln in text.split("\n") if ln.strip()),
    }


def _snapshot(
    text: str,
    child_units: int = 0,
    child_lines: int = 0,
    **extra,
) -> dict:
    """Assemble a snapshot conforming to :data:`SNAPSHOT_KEYS`.

    ``child_units``/``child_lines`` describe content that belongs to the
    artifact but sits outside the mutable text — a skill's reference
    files, for instance. Every shape reports them (zero when it has
    none) rather than omitting them, because a gate testing whether a key
    exists is a type branch in disguise, and the point of a uniform
    contract is that the gate never has to ask what it is looking at.

    Anything shape-specific goes under ``extra`` and is explicitly not
    part of the contract: recorded, never decided upon.
    """
    metrics = _text_metrics(text)
    metrics["child_units"] = child_units
    metrics["child_lines"] = child_lines
    if extra:
        metrics["extra"] = extra
    return metrics


def _dominant_newline(raw: str) -> str:
    """The newline sequence most used in ``raw``, defaulting to ``"\\n"``.

    CRLF is counted first and its occurrences removed from the other
    tallies, since every CRLF contains both a CR and an LF and would
    otherwise be counted three times.

    Classic Mac CR-only files are recognised too. They are rare, but the
    cost of getting them wrong is the same as for CRLF — every line of the
    file rewritten, and the one real edit lost in the diff.
    """
    crlf = raw.count("\r\n")
    lf = raw.count("\n") - crlf
    cr = raw.count("\r") - crlf
    if crlf >= lf and crlf >= cr and crlf > 0:
        return "\r\n"
    if cr > lf:
        return "\r"
    return "\n"


def _write_text_faithfully(path: Path, text: str) -> None:
    """Replace ``path``'s contents with ``text``, preserving its form.

    Four deliberate behaviours, each of which exists because violating it
    corrupts the record the optimization loop depends on:

    1. **Atomic replace.** The target file *is* the artifact being
       optimized, and at the moment of writing the candidate is not
       committed yet. A write interrupted halfway would leave a truncated
       prompt as the only copy. Writing to a temporary file in the same
       directory and renaming makes the swap all-or-nothing.

    2. **Trailing newline.** Generated text routinely comes back without
       its final newline. Left alone, that adds a "\\ No newline at end
       of file" marker to every single iteration's diff on top of the
       real change — and those diffs are the primary record of what the
       loop actually did.

    3. **Line-ending style.**``Path.read_text`` silently converts CRLF
       to LF, so writing back what was just read would rewrite every line
       of a CRLF file. The diff would show the whole file as changed and
       the one real edit would be unfindable.

       The incoming text is normalised to bare LF *first*, then written
       through ``newline=``. Without normalising, a candidate that already
       contains CRLF — routine, since a model asked to edit a CRLF file
       often echoes its line endings back — would have its CR preserved
       and an LF translated on top of it, yielding ``\\r\\r\\n``.

    4. **File mode.** ``mkstemp`` creates files as``0o600`` and
       ``os.replace`` keeps the *source* file's mode, so a naive
       implementation silently strips the execute bit and any group or
       world permissions from the artifact.

    The original is read unconditionally rather than guarded by an
    ``exists()`` check: every target validates its file at construction,
    so a missing file here means something removed the artifact
    mid-iteration. That deserves to surface as an error rather than be
    papered over by creating a new file.
    """
    with open(path, "r", newline="") as fh:
        original = fh.read()

    newline = _dominant_newline(original)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    if original.endswith(("\n", "\r")) and text and not text.endswith("\n"):
        text += "\n"

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", newline=newline) as fh:
            fh.write(text)
        shutil.copymode(path, tmp_path)
        os.replace(tmp_name, path)
    except BaseException:
        # Leaving a stray temp file behind would pollute the artifact's
        # own directory, which for a skill target is a git working tree
        # whose cleanliness the loop checks as a precondition.
        tmp_path.unlink(missing_ok=True)
        raise


# ─────────────────────────────────────────────
# The abstraction
# ─────────────────────────────────────────────

class Target(ABC):
    """The artifact under optimization.

    Intentionally narrow. A target can be read, written and measured —
    it cannot evaluate itself, score itself, or decide whether a change
    was an improvement. Those belong to the grading and gating layers,
    and keeping them out means a mutation step handed a ``Target`` has
    no way to reach the evaluator and mark its own homework.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier, used for workspace and log naming.

        Must be filesystem-safe: it becomes a directory name.
        """

    @property
    @abstractmethod
    def artifact_path(self) -> Path:
        """The filesystem entity being optimized.

        A directory for a skill, a file for a prompt. Distinct from
        :attr:`vcs_root`, and the distinction is not academic:
        ``vcs_root`` is *where history is kept*, ``artifact_path`` is
        *what is being changed*. For a skill both happen to be the same
        directory, which is exactly why conflating them silently
        misplaces the workspace one level up for every other shape.
        """

    @property
    def vcs_root(self) -> Path:
        """Directory the version-control commands must run in.

        The loop commits after every accepted change and reverts after
        every rejected one, so it needs the actual repository root — the
        nearest ancestor containing ``.git``. Returning the artifact's
        parent directory instead would appear to work (git commands
        resolve upward on their own) right up until the loop tried to
        interpret a path from git output, which is repository-relative.

        Falls back to the artifact's own directory when nothing is under
        version control. That is the honest answer for an artifact
        outside a repository, and it lets the loop's own precondition
        check ("is this under git?") be the thing that reports the
        problem, rather than this property guessing.
        """
        start = (
            self.artifact_path
            if self.artifact_path.is_dir()
            else self.artifact_path.parent
        )
        for candidate in (start, *start.parents):
            if (candidate / ".git").exists():
                return candidate
        return start

    @property
    def vcs_pathspec(self) -> str:
        """The artifact's path *relative to* :attr:`vcs_root`.

        Every version-control command the loop issues must be limited to
        this pathspec. Without one, ``git add -u`` operates on the whole
        working tree rather than the current directory's subtree — a
        documented Git 2.0 behaviour change that reads as a subtlety and
        behaves as data loss:

            a user had uncommitted work in ``src/app.py``; the loop
            changed ``prompts/answer.md``; the commit swept both; the
            gate rejected the candidate; the revert deleted the user's
            work. It was in neither the working tree nor the index
            afterwards, so nothing could bring it back.

        Returned as a string, and as a *relative* path, because that is
        what a pathspec is. An absolute path also works for ``add``, but
        every command that prints paths back (``status --porcelain``,
        ``ls-files``) reports them relative to the repository root, so
        keeping one representation avoids a class of comparisons that
        look right and match nothing.

        ``"."`` when the artifact *is* the repository root: a pathspec
        must name something, and the empty string matches nothing.
        """
        root = self.vcs_root
        artifact = self.artifact_path
        if artifact == root:
            return "."
        try:
            return str(artifact.relative_to(root))
        except ValueError:
            # vcs_root is derived from artifact_path, so it is always an
            # ancestor and this cannot normally happen. Falling back to
            # the artifact's own name rather than an absolute path keeps
            # the "pathspecs are repository-relative" property that the
            # output-parsing callers depend on.
            return artifact.name

    @property
    def vcs_scope_is_tree(self) -> bool:
        """Whether this target's pathspec covers a subtree or a single file.

        The one shape difference the version-control layer genuinely
        needs, exposed as a question the target answers rather than
        something the engine works out by inspecting the path. The
        engine asking ``artifact_path.is_dir()`` would be the same type
        branch as ``isinstance``, just spelled with a filesystem call.

        It matters for new files. A directory target's pathspec covers
        anything the mutation creates inside it, so a newly added helper
        can be committed. A file target's pathspec covers exactly one
        file, so a sibling file the mutation drops next to it is *not*
        in scope — and must not be swept in, because outside the
        artifact there is no longer any basis for believing a new file
        came from the mutation rather than from the user.
        """
        return self.artifact_path.is_dir()

    def structural_checks(self) -> list[dict]:
        """Preconditions the L1 gate enforces before spending an evaluation.

        Each entry is ``{"name", "pass", "detail"}``. Lives on the target
        because "is this artifact well-formed?" is a question only the
        shape can answer, and the gate used to answer it for one shape
        on behalf of all of them: it looked for ``SKILL.md`` inside the
        artifact unconditionally, so every file target failed with
        ``SKILL.md not found`` and the loop aborted at the first gate.

        The default is the checks that are meaningful for *any* artifact —
        it exists, it is readable, it is not empty. Deliberately not "no
        checks for shapes I don't recognise": a gate that waves through
        whatever it cannot classify is decoration, and the emptiness
        check in particular catches the realistic failure of a mutation
        writing nothing at all.

        Scanning for secrets and dangerous commands is *not* here. Those
        rules apply to prose regardless of shape, so keeping them in the
        gate avoids every shape reimplementing them; only the structural
        preconditions, which genuinely differ, are delegated.
        """
        checks = [{
            "name": "artifact_exists",
            "pass": self.artifact_path.exists(),
            "detail": (f"{self.artifact_path} exists"
                       if self.artifact_path.exists()
                       else f"{self.artifact_path} not found"),
        }]
        if not self.artifact_path.exists():
            return checks

        try:
            text = self.read()
        except (OSError, UnicodeDecodeError) as exc:
            checks.append({
                "name": "artifact_readable",
                "pass": False,
                "detail": f"cannot read {self.artifact_path}: {exc}",
            })
            return checks

        non_empty = bool(text.strip())
        checks.append({
            "name": "artifact_non_empty",
            "pass": non_empty,
            "detail": (f"{len(text)} chars of mutable text"
                       if non_empty
                       else f"{self.artifact_path} has no content to optimize"),
        })
        return checks

    def copy_artifact_to(self, dest: Path) -> Path:
        """Copy the artifact to ``dest`` so a later run can restore it.

        Polymorphic because the two shapes need different calls, and the
        engine used to make the directory-shaped one unconditionally:
        ``shutil.copytree(skill_path, ...)`` raises ``NotADirectoryError``
        on a file target, which took out the best-version archive — the
        only record of which candidate scored best — for every
        non-directory artifact.

        ``dest`` is the destination path itself, not a parent to copy
        into, so the caller does not have to know whether a file or a
        directory will appear there. Returns the path actually written,
        which for a file shape is ``dest`` with the artifact's own suffix
        preserved: restoring a prompt needs its extension intact, and a
        reader listing the archive should be able to tell what the entry
        is without opening it.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.artifact_path.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(
                self.artifact_path, dest,
                ignore=shutil.ignore_patterns(".git"),
            )
            return dest
        target = dest.with_suffix(self.artifact_path.suffix)
        if target.is_dir():
            shutil.rmtree(target)
        shutil.copy2(self.artifact_path, target)
        return target

    @property
    def workspace(self) -> Path:
        """Directory holding evolve state (results, plan, best versions).

        Default convention: a sibling of the artifact, named after the
        target. Anchored on ``artifact_path`` rather than ``root`` so
        that a file target's workspace lands beside the file instead of
        one directory further up.

        Kept outside the artifact so that the loop's own bookkeeping
        never shows up in the artifact's diffs — otherwise every
        iteration would commit its own log file alongside the change it
        is trying to measure.

        A subclass overrides this when its layout needs a different
        convention; see :class:`SkillTarget`.
        """
        return self.artifact_path.parent / f"{self.name}-workspace"

    @abstractmethod
    def read(self) -> str:
        """Return the mutable text.

        This is exactly the text a mutation step is allowed to rewrite —
        no more. Anything a target deliberately withholds here (an
        anchor line, a neighbouring section) is thereby protected
        without needing a rule telling the mutator not to touch it.
        """

    @abstractmethod
    def write(self, text: str) -> None:
        """Replace the mutable text with ``text``."""

    def context(self) -> str:
        """Return the full text an evaluation should be scored against.

        Distinct from :meth:`read`, and the distinction is the reason this
        method exists: what a mutation step may *change* is not always
        what an evaluation must *see*. A skill's reference files are read
        at run time and therefore belong in the scored text, yet only
        ``SKILL.md`` is offered for rewriting in a single step; a section
        target may be rewritten in isolation, yet the surrounding
        instructions still shape the behaviour being judged.

        Defaults to :meth:`read` because for most shapes the two
        genuinely coincide — a standalone prompt file is both the whole
        input and the whole mutable text. Shapes where they differ
        override this.

        Having it here rather than letting the evaluator inspect the
        target is what keeps the engine free of type branching: without
        it, an evaluator wanting a skill's corpus would have toask
        "which kind of target is this?", which is the ``isinstance``
        the whole abstraction exists to avoid.
        """
        return self.read()

    @abstractmethod
    def snapshot(self) -> dict:
        """Structural metrics, for gates and for the record.

        Numbers only, and every shape returns the **same keys** — see
        :func:`_text_metrics` for the contract. A gate must be able to
        ask "did this candidate get more bloated?" without knowing what
        it is looking at; if the key set varied by shape, the gate would
        have to test for key presence, which is a type branch wearing a
        dictionary lookup as a disguise.

        Shape-specific detail goes under the ``extra`` key, which gates
        do not read. That is where a skill reports its file counts and a
        section reports its share of the containing file: useful in the
        record, never load-bearing for a decision.
        """

    def summary(self) -> str:
        """One-line human description, for plan headers and logs.

        Default: the first non-blank line of the mutable text. Overridden
        where the artifact carries a purpose-built description field.
        """
        for line in self.read().split("\n"):
            stripped = line.strip()
            if stripped:
                return stripped[:200]
        return ""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, path={str(self.artifact_path)!r})"


# ─────────────────────────────────────────────
# A skill directory
# ─────────────────────────────────────────────

class SkillTarget(Target):
    """A skill directory whose ``SKILL.md`` is the mutable text.

    The whole file is mutable, frontmatter included: the description
    field is a legitimate thing to optimize, since it decides whether
    the skill gets selected in the first place.

    Note that ``read()`` and ``context()`` deliberately differ here —
    only ``SKILL.md`` may be rewritten in one step, but the whole prose
    corpus is what an evaluation sees.
    """

    def __init__(self, path: Path):
        from common import SKILL_FILE  # local import: avoids a cycle

        self.path = Path(path).resolve()
        if not self.path.is_dir():
            raise FileNotFoundError(f"Skill directory not found: {self.path}")
        self.skill_md = self.path / SKILL_FILE
        if not self.skill_md.is_file():
            # A stricter precondition than the code this replaced, which
            # scaffolded a workspace for a directory with no SKILL.md and
            # wrote "(could not parse SKILL.md)" into the plan. There is
            # nothing to optimize in that state, so every later phase
            # would fail anyway — with a message about whatever it
            # happened to touch first rather than about the actual cause.
            raise FileNotFoundError(
                f"No {SKILL_FILE} in {self.path}. A skill target needs an "
                f"existing {SKILL_FILE} to optimize; create one first, or "
                f"pass a prompt file directly if that is the artifact."
            )

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def artifact_path(self) -> Path:
        return self.path

    @property
    def workspace(self) -> Path:
        """Delegate to the established skill-workspace convention.

        ``common.find_workspace`` already encodes a non-obvious rule (a
        plugin-hosted skill puts its workspace beside the plugin repo,
        not beside the skill body, so the plugin source tree stays
        clean). Re-deriving that here would create a second definition
        of the same convention, and the two would drift.
        """
        from common import find_workspace  # local import: avoids a cycle

        return find_workspace(self.path)

    def read(self) -> str:
        return self.skill_md.read_text()

    def write(self, text: str) -> None:
        _write_text_faithfully(self.skill_md, text)

    def context(self) -> str:
        """The concatenated prose corpus, not just ``SKILL.md``.

        This is the shape that motivates ``context()`` existing at all.
        The reference files are read at run time and therefore shape the
        behaviour being judged, so an evaluation that scored``SKILL.md``
        alone would attribute their content to nothing. Yet only
        ``SKILL.md`` is offered to a mutation step, because rewriting
        several files at once would make it impossible to attribute a
        score change to any one of them.

        Delegates to ``common.build_skill_corpus``: the ``### <path> ###``
        header format it emits is parsed downstream to map a match back
        to a file and line, so reimplementing the concatenation here
        would silently break failure reporting.
        """
        from common import build_skill_corpus  # local import: avoids a cycle

        return build_skill_corpus(self.path)

    def summary(self) -> str:
        """Prefer the frontmatter description over the first line.

        For a skill the first line is always ``---``, so the inherited
        default would report nothing useful.
        """
        from common import parse_skill_md  # local import: avoids a cycle

        try:
            _, description, _ = parse_skill_md(self.path)
        except (ValueError, FileNotFoundError, OSError):
            return super().summary()
        return description[:200] if description else super().summary()

    def structural_checks(self) -> list[dict]:
        """The inherited checks plus the ones only a skill has.

        A skill's entry point is a specific file with a required
        frontmatter contract, and that contract decides whether the skill
        gets selected at all — so a malformed one is worth rejecting
        before an evaluation is spent on it. The inherited checks already
        establish that ``SKILL.md`` exists and is non-empty (``read()``
        returns it), which is why this only adds what is skill-specific
        rather than restating them.
        """
        from common import SKILL_FILE, validate_frontmatter

        checks = super().structural_checks()
        if not all(c["pass"] for c in checks):
            # Frontmatter validation on an unreadable or empty SKILL.md
            # would report a parse error, which describes a symptom of
            # the failure already reported above rather than a new fact.
            return checks

        valid, msg = validate_frontmatter(self.path)
        checks.append({
            "name": "frontmatter_valid",
            "pass": valid,
            "detail": msg,
        })

        body = self.read()
        body_start = body.find("---", 3)
        has_body = len(body[body_start + 3:].strip()) > 10 if body_start > 0 else False
        checks.append({
            "name": "has_body",
            "pass": has_body,
            "detail": (f"{SKILL_FILE} has body content" if has_body
                       else f"{SKILL_FILE} body is empty or too short"),
        })
        return checks

    def snapshot(self) -> dict:
        """``SKILL.md`` size, plus the supporting files that ship with it.

        Supporting files are counted because a candidate can keep
        ``SKILL.md`` small by moving bulk into ``references/`` — a real
        improvement when it makes the entry point scannable, pure
        relocation when it just moves the same words. A gate can only
        tell those apart if it sees both numbers, which is why the
        uniform contract carries ``child_units``/``child_lines`` for
        every shape rather than letting this one add its own keys.
        """
        from common import SKILL_CODE_DIRS, SKILL_PROSE_DIRS

        per_dir: dict[str, int] = {}
        child_units = 0
        child_lines = 0
        for subdir in SKILL_PROSE_DIRS + SKILL_CODE_DIRS:
            dir_path = self.path / subdir
            files = (
                [p for p in sorted(dir_path.rglob("*")) if p.is_file()]
                if dir_path.is_dir()
                else []
            )
            per_dir[subdir] = len(files)
            child_units += len(files)
            for f in files:
                try:
                    child_lines += _text_metrics(f.read_text())["lines"]
                except (UnicodeDecodeError, OSError):
                    # A binary or unreadable helper still exists and is
                    # counted above; only its line count is unknown.
                    # Skipping beats aborting: a gate missing one file's
                    # lines still works, a gate that raised has nothing
                    # to compare.
                    continue
        return _snapshot(
            self.read(),
            child_units=child_units,
            child_lines=child_lines,
            files_per_dir=per_dir,
        )


# ─────────────────────────────────────────────
# Shared base for file-backed shapes
# ─────────────────────────────────────────────

class _FileBackedTarget(Target):
    """Commonground for targets whose artifact is a single file.

    Holds only what is genuinely identical between them: the path, its
    validation, and the two anchors derived from it. Deliberately does
    NOT implement ``read``/``write`` — that is precisely where the two
    shapes differ, and providing a default here would tempt a subclass
    to inherit the wrong one.

    This sits between :class:`Target` and the concrete shapes rather
    than one of them inheriting from the other: a section of a file is
    not a special case of a whole file (their ``read`` contracts are not
    substitutable), so making one the parent of the other would violate
    Liskov substitution for the sake of saving four lines.
    """

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            # Named after the concrete subclass rather than hard-coded:
            # "Prompt file not found" from a SectionTarget would send the
            # reader looking for the wrong mistake.
            raise FileNotFoundError(
                f"{type(self).__name__} needs an existing file: {self.path}"
            )

    @property
    def artifact_path(self) -> Path:
        return self.path


# ─────────────────────────────────────────────
# A standalone prompt file
# ─────────────────────────────────────────────

class PromptFileTarget(_FileBackedTarget):
    """Any single text file that is entirely a prompt.

    The simplest shape, and the one the engine previously could not
    accept. No structural assumptions are made about the contents: it
    may be Markdown, plain text, or a template with placeholders.
    """

    @property
    def name(self) -> str:
        """Derived from the filename, extension dropped.

        ``system_prompt.md`` yields workspace ``system_prompt-workspace``.
        The suffix is dropped because it describes the file format, not
        the artifact, and keeping it would produce
        ``system_prompt.md-workspace``.
        """
        return self.path.stem or self.path.name

    def read(self) -> str:
        return self.path.read_text()

    def write(self, text: str) -> None:
        _write_text_faithfully(self.path, text)

    def snapshot(self) -> dict:
        return _snapshot(self.read())


# ─────────────────────────────────────────────
# One section inside a file
# ─────────────────────────────────────────────

# Heading recognition, deliberately limited to ATX headings (``## Title``),
# optionally indented up to three spaces and optionally closed
# (``## Title ##``) — the same tolerances CommonMark specifies, and the
# same indent allowance as the fence pattern below, so the two agree on
# what counts as "at the start of a line".
#
# Setext headings (a title underlined with ``===`` or ``---``) are NOT
# recognised, and this is a decision rather than an oversight: ``---`` is
# also the YAML frontmatter delimiter and a thematic break, so treating
# it as a heading would make a skill's frontmatter fence look like a
# section boundary. A file using setext headings simply has no addressable
# sections, and :class:`SectionTarget` reports that as "heading not found"
# with the headings it did see — which is the accurate answer.
#
# Fences are tracked so that a '#' comment inside a shell example is not
# mistaken for a heading. Without this, a prompt file containing a snippet
# would have its section boundaries computed wrongly and a write would
# overwrite the wrong span.
_FENCE_RE = re.compile(r"^ {0,3}(?:```|~~~)")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(\S.*?)(?:\s+#+)?\s*$")

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Length of the disambiguating digest appended to a slug. Six hex chars
# is short enough to keep the directory name readable and wide enough
# that a collision needs ~16 million distinct headings.
_DIGEST_CHARS = 6

# Cap on the readable part of a slug. A heading can be a whole sentence,
# and the slug becomes a directory name — most filesystems reject a
# component over 255 bytes, so an uncapped slug turns a long heading into
# a workspace that cannot be created at all. Truncation is safe here
# precisely because uniqueness rests on the digest, not on the prose.
_SLUG_CHARS = 40


def _slugify(text: str) -> str:
    """Reduce a heading to a readable, collision-free directory token.

    The slug alone is lossy by design — it lowercases, folds every run of
    punctuation into a single dash, and truncates — so ``"Rule Set"`` and
    ``"Rule/Set"`` reduce to the same thing. That matters because the slug
    names a workspace: two sections sharing one would overwrite each
    other's results, plan and best-version history, and the logs would
    look entirely normal while it happened.

    A short digest of the original heading is therefore appended. It
    carries the uniqueness, which is what lets the readable part be
    truncated freely, and it rescues headings that slugify to nothing at
    all (a fully non-ASCII title) — those would otherwise have no name.
    """
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    slug = _SLUG_RE.sub("-", text.casefold())[:_SLUG_CHARS].strip("-")
    return f"{slug}-{digest}" if slug else digest


def _scan(lines: list[str]) -> tuple[list[tuple[int, int, str]], bool]:
    """Return ``(headings, fences_balanced)`` from a single pass.

    ``headings`` holds ``(index, level, title)`` for every ATX heading
    that is *outside* a code fence — a ``#`` comment inside a shell
    example is not a section boundary, and treating it as one would make
    a write land on the wrong span of the file.

    ``fences_balanced`` reports whether every opened fence was closed.
    Both facts come from the same pass because they are two readings of
    one state machine; computing them separately would mean two copies
    that can disagree about where a section ends, which is precisely the
    corruption this is meant to prevent.
    """
    headings: list[tuple[int, int, str]] = []
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            headings.append((i, len(m.group(1)), m.group(2)))
    return headings, not in_fence


def _duplicate_titles(lines: list[str]) -> set[str]:
    """Case-folded titles appearing more than once outside code fences.

    Case-folded because :meth:`SectionTarget._locate` matches that way; a
    check that compared case-sensitively would miss exactly the collisions
    the locator goes on to reject.
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    headings, _ = _scan(lines)
    for _, _, title in headings:
        folded = title.casefold()
        if folded in seen:
            duplicates.add(folded)
        seen.add(folded)
    return duplicates


class SectionTarget(_FileBackedTarget):
    """One heading's body inside a larger file.

    The reason this shape exists: a long instruction file usually has
    exactly one part worth optimizing, and letting a mutation step
    rewrite the entire file would (a) put unrelated, already-tuned
    content at risk on every iteration and (b) make each diff too large
    to attribute a score change to anything specific.

    The heading line itself is deliberately **not** part of the mutable
    text. It is the anchor used to find the section again next
    iteration; if a rewrite could change or delete it, the target would
    lose its own address. Withholding it from``read()`` makes that
    impossible by construction rather than by instruction.
    """

    def __init__(self, path: Path, section: str):
        super().__init__(path)
        self.section = section.strip().lstrip("#").strip()
        if not self.section:
            raise ValueError("section must be a non-empty heading title")
        # Locate once, eagerly: a target that cannot find its own
        # section is unusable, and finding out now beats finding out
        # after the loop has already spent a mutation on it.
        self._locate(self.path.read_text().split("\n"))

    def _locate(self, lines: list[str]) -> tuple[int, int, int]:
        """Return ``(level, body_start, body_end)`` for the section.

        ``body_end`` is exclusive and stops before the next heading at
        the same or a shallower level, which is what makes a section
        include its own subsections. ``level`` is returned rather than
        remembered on the instance because it is a property of the file's
        current contents, and a cached copy would go stale the moment the
        heading's depth changed.

        Recomputed on every access for the same reason: the file is
        edited between iterations, so a remembered line number would
        address the wrong span as soon as anything above the heading
        changed length.

        Refuses outright on a file whose code fences are unbalanced. Such
        a file has no well-defined section boundaries at all — every
        heading after the unclosed fence is invisible, so a section
        appears to run to the end of the file and a write would delete
        whatever follows. Returning a boundary anyway is the more
        dangerous option precisely because the numbers look plausible;
        the loop cannot distinguish a section that genuinely reaches the
        end from one that only appears to.
        """
        wanted = self.section.casefold()
        headings, balanced = _scan(lines)
        if not balanced:
            raise InvalidBody(
                f"{self.path} has an unclosed code fence, so section "
                f"boundaries cannot be determined: every heading after the "
                f"fence is invisible and {self.section!r} would appear to "
                f"run to the end of the file. Close the fence first."
            )
        matches = [h for h in headings if h[2].casefold() == wanted]
        if not matches:
            available = ", ".join(repr(h[2]) for h in headings[:12]) or "none"
            raise SectionNotFound(
                f"no heading {self.section!r} in {self.path}; "
                f"headings found: {available}"
            )
        if len(matches) > 1:
            at = ", ".join(f"line {h[0] + 1}" for h in matches)
            raise AmbiguousSection(
                f"heading {self.section!r} appears {len(matches)} times in "
                f"{self.path} ({at}); section titles must be unique to be "
                f"addressable"
            )

        index, level, _ = matches[0]
        body_start = index + 1
        body_end = len(lines)
        for other_index, other_level, _ in headings:
            if other_index > index and other_level <= level:
                body_end = other_index
                break
        return level, body_start, body_end

    def _verify_round_trip(
        self, candidate_lines: list[str], body_start: int, body_end: int
    ) -> None:
        """Raise :class:`InvalidBody` unless the merged file still addresses
        this section at exactly the span just written.

        This is deliberately a **postcondition on the whole file**, checked
        before the write lands, rather than a set of rules about the
        candidate body. Validating the body alone was the earlier design
        and it leaked in both directions:

        - It missed corruption the *existing* file contributes. A fence
          opened in this section and closed in a later one is balanced
          from the file's point of view but unbalanced from the body's;
          replacing the body then closed nothing, every later heading
          went invisible, and the neighbouring section was deleted on the
          very first write.
        - It missed corruption visible only in combination. A deeper
          heading is legitimate in a body, yet if a *different* section
          already has a subsection of the same name, adding it makes the
          title ambiguous — and an ambiguous section can never be read or
          written again, with no earlier state to revert to because the
          damage happened before the commit.

        Re-locating in the merged text collapses all of those into one
        question: *would the next iteration find exactly what this one
        wrote?* Any answer other than yes means the section has lost its
        own address. Using the real``_locate`` rather than a reimplemented
        approximation is what makes the guarantee hold — a separate
        validator could disagree with the locator, and then the check
        would pass while the boundary still moved.
        """
        try:
            _, new_start, new_end = self._locate(candidate_lines)
        except InvalidBody:
            # Already describes the unbalanced fence and how to fix it.
            raise
        except (SectionNotFound, AmbiguousSection) as exc:
            raise InvalidBody(
                f"the resulting file would no longer address section "
                f"{self.section!r}: {exc}"
            ) from exc

        if (new_start, new_end) != (body_start, body_end):
            raise InvalidBody(
                f"the resulting file would locate section {self.section!r} at "
                f"lines {new_start + 1}-{new_end} instead of "
                f"{body_start + 1}-{body_end}, because a heading in the new "
                f"body re-delimits it. Writing it would move the section's "
                f"boundary, so the next write would land on a different span "
                f"of the file and delete what is there."
            )

        self._reject_new_duplicate_titles(candidate_lines)

    def _reject_new_duplicate_titles(self, candidate_lines: list[str]) -> None:
        """Refuse a write that would make some *other* heading ambiguous.

        Addressability is a property of the file, not of one section, so a
        write can be harmless to its own boundary and still destroy a
        neighbour's. Adding a subsection whose title already exists
        elsewhere does exactly that: the duplicated title can no longer be
        located, so whichever section carries it becomes permanently
        unreadable and unwritable — and since the damage lands before the
        commit, there is no earlier state to revert to.

        Only titles that are newly duplicated are rejected. Duplicates the
        file already contained are left alone: they were somebody else's
        decision, they are already unaddressable, and refusing to write
        because of a pre-existing flaw elsewhere in the file would block
        legitimate work on a section that is perfectly well-formed.
        """
        before = _duplicate_titles(self.path.read_text().split("\n"))
        after = _duplicate_titles(candidate_lines)
        newly_ambiguous = after - before
        if newly_ambiguous:
            shown = ", ".join(sorted(repr(t) for t in newly_ambiguous))
            raise InvalidBody(
                f"the new body would duplicate heading(s) {shown}, which "
                f"already appear elsewhere in {self.path}. A duplicated "
                f"title cannot be addressed, so those sections would become "
                f"permanently unreadable. Rename the heading(s) in the body."
            )

    @property
    def name(self) -> str:
        """``<file-stem>-<section-slug>-<digest>``.

        No empty-slug fallback is needed: :func:`_slugify` always returns
        something, so two sections of one file can never collapse onto
        the same workspace.
        """
        stem = self.path.stem or self.path.name
        return f"{stem}-{_slugify(self.section)}"

    def read(self) -> str:
        lines = self.path.read_text().split("\n")
        _, body_start, body_end = self._locate(lines)
        return "\n".join(lines[body_start:body_end])

    def write(self, text: str) -> None:
        """Replace the section's body, refusing writes that would corrupt it.

        The merged file is assembled in memory and verified before
        anything reaches the disk, so a rejected candidate leaves the file
        byte-identical. Order matters: verifying after the write would
        mean the corrupting text is already the only copy by the time
        anyone objects, and this particular corruption happens between two
        writes — before the accept-or-revert commit — where version
        control has nothing to restore.
        """
        lines = self.path.read_text().split("\n")
        _, body_start, body_end = self._locate(lines)
        new_body = text.split("\n")
        candidate_lines = lines[:body_start] + new_body + lines[body_end:]
        self._verify_round_trip(
            candidate_lines, body_start, body_start + len(new_body)
        )
        _write_text_faithfully(self.path, "\n".join(candidate_lines))

    def context(self) -> str:
        """The whole containing file, not just the section.

        The surrounding instructions still shape the behaviour being
        judged even though only this section is being rewritten. Scoring
        the section in isolation would credit or blame it for an effect
        that the rest of the file produced.
        """
        return self.path.read_text()

    def snapshot(self) -> dict:
        """Section metrics; the containing file's size goes in ``extra``.

        The section's share of the file is recorded because "it grew 20%"
        means something different in a file it dominates than in one
        where it is a footnote. It sits in ``extra`` rather than the
        contract because no gate should branch on it — a gate compares
        ``chars`` before and after, and that comparison is valid for
        every shape.

        No division guard is needed: reaching this line means ``_locate``
        found a heading, so the file necessarily has characters in it.
        """
        full_text = self.path.read_text()
        lines = full_text.split("\n")
        _, body_start, body_end = self._locate(lines)
        body = "\n".join(lines[body_start:body_end])
        file_metrics = _text_metrics(full_text)

        return _snapshot(
            body,
            section=self.section,
            file_chars=file_metrics["chars"],
            file_lines=file_metrics["lines"],
            share_of_file=round(len(body) / file_metrics["chars"], 4),
        )


# ─────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────

def resolve_target(path: Path, section: str | None = None) -> Target:
    """Build the right :class:`Target` for ``path``.

    This is the one place in the codebase allowed to decide which shape
    an artifact has. Concentrating that decision here is what lets every
    other module make purely polymorphic calls — the alternative, each
    caller inspecting the path itself, would scatter the same three-way
    branch across the engine and guarantee the copies disagree
    eventually.

    Args:
        path: a skill directory, or a file.
        section: when given, restrict optimization to that heading's body.

    Raises:
        FileNotFoundError: nothing exists at ``path``, or it is a
            directory with no ``SKILL.md`` — a directory is only a valid
            target as a skill, so "which shape did you mean?" has no
            reasonable default and guessing would be worse than failing.
        ValueError: ``section`` was given for a skill directory, where it
            would be ambiguous which of the skill's files it refers to.
    """
    path = Path(path)
    if path.is_dir():
        if section is not None:
            from common import SKILL_FILE  # local import: avoids a cycle

            raise ValueError(
                "section is not supported for a skill directory: it would be "
                "ambiguous which file the heading belongs to. Pass the "
                "specific file instead, e.g. "
                f"{path / SKILL_FILE}"
            )
        return SkillTarget(path)
    if path.is_file():
        return SectionTarget(path, section) if section else PromptFileTarget(path)
    raise FileNotFoundError(f"Target not found: {path}")
