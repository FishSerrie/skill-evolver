"""Extract a JSON object from an LLM's raw text output.

Single responsibility: locate and parse the JSON object an LLM was asked
to emit, and nothing else. This module holds no business semantics — it
does not know what a diagnosis, a mutation, or a verdict is. Callers own
the meaning of the fields; this module only hands back a ``dict``.

Why it exists as its own module: the "find the last JSON object" convention
was implemented five separate times (twice in ``isolation.py``, once in
``verifier_panel.py``, twice in ``llm.py``). Five copies of a parsing
convention means five places for it to drift — and any new grader needing
the same capability would have made a sixth. The convention itself is a
contract between prompt and parser, so it must have exactly one definition.

Stdlib only, no IO, no LLM calls: this module is a leaf in the dependency
graph and can be tested exhaustively without any external process.
"""

from __future__ import annotations

import json
import re

__all__ = ["extract_json_object"]

# A line that is nothing but a JSON object, allowing surrounding whitespace.
# Matched with the multiline flag so each line is considered independently.
_LINE_OBJECT_RE = re.compile(r"^[ \t]*(\{.*\})[ \t]*$", re.MULTILINE)


def extract_json_object(text: str, required_key: str | None = None) -> dict | None:
    """Return the last JSON object in ``text``, or ``None`` if there is none.

    "Last" means last **by position**, whichever way the object was written.
    A single-line object and a pretty-printed one are equally valid
    candidates, and the one that starts later wins.

    That ordering rule is the whole contract, and it is load-bearing.
    Prompts instruct the model to emit the object at the end, so anything
    earlier is preamble — very often a restatement of the *template printed
    in the prompt*, which has the same keys as a real answer and therefore
    survives any key-based filter. An earlier version of this function
    preferred single-line matches outright; a restated one-line template
    then beat the pretty-printed real answer that followed it, the empty
    template was scored as the model's classification, and the case was
    rejected as inconsistent.

    Args:
        text: raw model output. ``None`` or non-``str`` input is treated
            as "nothing to parse" rather than raising, because callers
            receive this straight from a subprocess or an agent call and
            a missing response is an expected condition, not a bug.
        required_key: when given, only objects containing this key are
            accepted. This narrows the field when a reply holds several
            objects, but it cannot separate a restated template from a real
            answer — both carry the same keys — which is why position, not
            shape, decides.

    Returns:
        The parsed ``dict``, or ``None`` when nothing parses as a JSON
        object satisfying ``required_key``. Returning ``None`` rather
        than raising is deliberate: every caller degrades to a safe
        default (an empty diagnosis, "no change", an ``error`` verdict)
        so that one malformed response cannot abort an optimization run.
        Callers that need to distinguish "absent" from "empty" can, since
        ``None`` is never a successful parse result.
    """
    if not isinstance(text, str):
        return None

    best: dict | None = None
    best_offset = -1
    for offset, candidate in _candidates(text):
        if offset < best_offset:
            # A later object already won; nothing earlier can beat it.
            continue
        if required_key is not None and required_key not in candidate:
            # Cheap substring pre-filter before the parse. The membership
            # test after parsing is the authoritative one — this only skips
            # spans that cannot possibly qualify.
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            # Balanced braces do not guarantee valid JSON: a trailing comma
            # or an unquoted key still fails here.
            continue
        # No isinstance check: every candidate starts with '{', so a
        # successful parse is necessarily an object.
        if required_key is not None and required_key not in parsed:
            continue
        best, best_offset = parsed, offset
    return best


def _candidates(text: str):
    """Yield ``(start_offset, span)`` for every plausible JSON object.

    Two shapes are collected, because both occur in practice and neither
    subsumes the other:

    - a line that is an object on its own, which is what prompts ask for;
    - a brace-balanced run spanning several lines, which is what a model
      that decides to pretty-print produces.

    Both carry their offsets so that one ordering rule applies to all of
    them. Collecting only one shape — or preferring one over the other —
    is what allowed a restated template to outrank the real answer.
    """
    for match in _LINE_OBJECT_RE.finditer(text):
        yield match.start(1), match.group(1).strip()
    yield from _braced_spans(text)


def _braced_spans(text: str):
    """Yield ``(start_offset, span)`` for each brace-balanced object.

    Walks the text tracking brace depth, ignoring braces inside strings so
    that a value like ``"{not an object}"`` cannot end a span early, and
    honouring backslash escapes so an escaped quote does not appear to
    close one.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0:
                    yield start, text[start:index + 1]
