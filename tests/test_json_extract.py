"""Tests for json_extract — the single JSON-extraction implementation.

Imports only the module under test plus stdlib, per the design's
independent-testability invariant: if this file ever needs to import
another new module to exercise json_extract, the responsibility split has
leaked.
"""

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "plugin" / "skills" / "skill-evolver" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from json_extract import extract_json_object  # noqa: E402


class HappyPathTests(unittest.TestCase):
    def test_extracts_a_single_json_line(self):
        self.assertEqual(
            extract_json_object('{"a": 1}'), {"a": 1})

    def test_ignores_prose_before_the_json(self):
        text = 'Let me think about this.\nHere is my answer:\n{"a": 1}'
        self.assertEqual(extract_json_object(text), {"a": 1})

    def test_ignores_trailing_prose_after_the_json(self):
        """Reverse scan must still find the object when the model keeps talking."""
        text = '{"a": 1}\nHope that helps!'
        self.assertEqual(extract_json_object(text), {"a": 1})

    def test_tolerates_surrounding_whitespace(self):
        self.assertEqual(
            extract_json_object('   \n\t  {"a": 1}   \n  '), {"a": 1})

    def test_returns_last_object_when_several_are_present(self):
        """The prompt's example JSON often precedes the real answer."""
        text = '{"a": 1}\n{"a": 2}'
        self.assertEqual(extract_json_object(text), {"a": 2})


class RequiredKeyTests(unittest.TestCase):
    def test_accepts_object_containing_the_required_key(self):
        self.assertEqual(
            extract_json_object('{"verdict": "pass"}', required_key="verdict"),
            {"verdict": "pass"})

    def test_skips_objects_lacking_the_required_key(self):
        """This is the disambiguation the required_key parameter exists for."""
        text = '{"unrelated": true}\n{"verdict": "pass"}\n{"other": 1}'
        self.assertEqual(
            extract_json_object(text, required_key="verdict"),
            {"verdict": "pass"})

    def test_returns_none_when_required_key_never_appears(self):
        self.assertIsNone(
            extract_json_object('{"a": 1}', required_key="verdict"))

    def test_required_key_check_is_on_parsed_keys_not_substring(self):
        """A key name appearing only inside a VALUE must not count as a match.

        The substring pre-filter would pass this line; the post-parse
        membership test is what makes the check correct.
        """
        text = '{"reason": "the verdict was unclear"}'
        self.assertIsNone(extract_json_object(text, required_key="verdict"))


class DegradationTests(unittest.TestCase):
    """Every failure mode must return None, never raise: one malformed
    model response must not be able to abort an optimization run."""

    def test_empty_string(self):
        self.assertIsNone(extract_json_object(""))

    def test_no_json_at_all(self):
        self.assertIsNone(extract_json_object("I could not complete the task."))

    def test_malformed_json_is_skipped(self):
        self.assertIsNone(extract_json_object('{"a": 1,}'))

    def test_falls_back_to_earlier_valid_object_when_last_is_malformed(self):
        text = '{"a": 1}\n{"a": broken}'
        self.assertEqual(extract_json_object(text), {"a": 1})

    def test_json_array_is_not_an_object(self):
        self.assertIsNone(extract_json_object('[1, 2, 3]'))

    def test_json_scalar_is_not_an_object(self):
        """A bare scalar does not start with '{' and must not be returned."""
        self.assertIsNone(extract_json_object('42'))

    def test_none_input(self):
        self.assertIsNone(extract_json_object(None))

    def test_non_string_input(self):
        for bad in (123, [], {}, object()):
            self.assertIsNone(extract_json_object(bad))

    def test_empty_object_is_a_valid_result(self):
        """{} parses and is a dict — distinct from None (nothing found)."""
        self.assertEqual(extract_json_object('{}'), {})


class MultilineObjectTests(unittest.TestCase):
    """Pretty-printed replies must parse.

    An earlier version pinned the opposite as intended behaviour, on the
    reasoning that the prompt asks for one line so a multi-line object means
    the model broke its contract. That reasoning was wrong in practice: a
    model that pretty-prints is emitting perfectly valid JSON, and rejecting
    it reported "the classifier returned nothing" for a good reply — turning
    a cosmetic difference into a failed case. The line scan still runs first,
    since it is exact about where the answer ends.
    """

    PRETTY = '{\n  "matched": [1, 2],\n  "missed": []\n}'

    def test_a_pretty_printed_object_is_found(self):
        self.assertEqual(
            extract_json_object(self.PRETTY), {"matched": [1, 2], "missed": []}
        )

    def test_a_pretty_printed_object_after_prose_is_found(self):
        text = "Here is my analysis.\n\n" + self.PRETTY
        self.assertEqual(extract_json_object(text)["matched"], [1, 2])

    def test_a_pretty_printed_object_inside_a_fence_is_found(self):
        text = "```json\n" + self.PRETTY + "\n```"
        self.assertEqual(extract_json_object(text)["matched"], [1, 2])

    def test_prose_after_the_object_does_not_hide_it(self):
        text = self.PRETTY + "\n\nHope this helps."
        self.assertEqual(extract_json_object(text)["matched"], [1, 2])

    def test_the_last_of_several_objects_wins(self):
        text = '{\n  "matched": [1]\n}\n{\n  "matched": [9]\n}'
        self.assertEqual(extract_json_object(text)["matched"], [9])

    def test_required_key_still_filters(self):
        text = '{\n  "other": 1\n}\n{\n  "matched": [1]\n}'
        self.assertEqual(
            extract_json_object(text, required_key="matched")["matched"], [1]
        )

    def test_no_object_with_the_required_key_yields_none(self):
        self.assertIsNone(
            extract_json_object('{\n  "other": 1\n}', required_key="matched")
        )

    def test_a_brace_inside_a_string_does_not_end_the_object(self):
        """Otherwise a value like "{x}" would truncate the span."""
        text = '{\n  "matched": [1],\n  "note": "a } brace"\n}'
        self.assertEqual(extract_json_object(text)["note"], "a } brace")

    def test_an_escaped_quote_does_not_close_the_string(self):
        text = '{\n  "note": "say \\"hi\\" now",\n  "matched": [1]\n}'
        self.assertEqual(extract_json_object(text)["matched"], [1])

    def test_a_brace_in_a_string_before_the_object_is_ignored(self):
        text = 'The model said "{ oops" first.\n{\n  "matched": [2]\n}'
        self.assertEqual(extract_json_object(text)["matched"], [2])

    def test_unbalanced_braces_yield_none(self):
        self.assertIsNone(extract_json_object('{\n  "matched": [1]\n'))

    def test_a_multiline_array_is_not_an_object(self):
        """Only objects qualify; a bare array is not a valid result."""
        self.assertIsNone(extract_json_object('[\n  1,\n  2\n]'))

    def test_prose_only_still_yields_none(self):
        self.assertIsNone(extract_json_object("It covered most of the points."))

    def test_nested_objects_return_the_outermost(self):
        text = '{\n  "matched": [1],\n  "inner": {"a": 1}\n}'
        result = extract_json_object(text)
        self.assertEqual(result["matched"], [1])
        self.assertEqual(result["inner"], {"a": 1})

    def test_the_single_line_scan_takes_precedence(self):
        """A one-line answer must not be overridden by an earlier block."""
        text = '{\n  "matched": [9]\n}\n{"matched": [1]}'
        self.assertEqual(extract_json_object(text)["matched"], [1])

    def test_a_stray_closing_brace_does_not_derail_the_scan(self):
        """Depth cannot go negative; the real object is still found."""
        text = 'oops }\n{\n  "matched": [3]\n}'
        self.assertEqual(extract_json_object(text)["matched"], [3])

    def test_balanced_braces_holding_invalid_json_are_skipped(self):
        """Balance does not imply validity — a trailing comma still fails."""
        text = '{\n  "matched": [1],\n}\n{\n  "matched": [4]\n}'
        self.assertEqual(extract_json_object(text)["matched"], [4])

    def test_only_invalid_balanced_spans_yields_none(self):
        self.assertIsNone(extract_json_object('{\n  "matched": [1],\n}'))


if __name__ == "__main__":
    unittest.main()


class PositionBeatsShapeTests(unittest.TestCase):
    """"Last" means last by position, not "single-line if there is one".

    A version that preferred single-line matches outright had a specific and
    likely failure: the coverage prompt *prints a one-line template*, so a
    model restating it and then pretty-printing its real answer had the empty
    template chosen, and conservation rejected the case. `required_key`
    cannot separate the two — the template carries the same keys.
    """

    S1, S2 = '{"a": 1}', '{"a": 2}'
    M1 = '{\n  "a": 1\n}'
    M2 = '{\n  "a": 2\n}'

    def test_both_single_line(self):
        self.assertEqual(extract_json_object(f"{self.S1}\n{self.S2}"), {"a": 2})

    def test_first_multiline_last_single(self):
        self.assertEqual(extract_json_object(f"{self.M1}\n{self.S2}"), {"a": 2})

    def test_first_single_last_multiline(self):
        """The regression: shape must not outrank position."""
        self.assertEqual(extract_json_object(f"{self.S1}\n{self.M2}"), {"a": 2})

    def test_both_multiline(self):
        self.assertEqual(extract_json_object(f"{self.M1}\n{self.M2}"), {"a": 2})

    def test_a_restated_template_does_not_beat_the_real_answer(self):
        """The exact scenario the prompt's own template makes likely."""
        reply = (
            "Following the required format:\n"
            '{"matched": [], "partial": [], "missed": [], "extra": []}\n\n'
            "My actual classification:\n"
            '{\n  "matched": [1, 2, 3],\n  "partial": [],\n'
            '  "missed": [],\n  "extra": []\n}'
        )
        result = extract_json_object(reply, required_key="matched")
        self.assertEqual(result["matched"], [1, 2, 3])

    def test_a_pretty_template_then_a_single_line_answer(self):
        """The mirror case, which a naive priority flip would break."""
        reply = (
            'Template:\n{\n  "matched": []\n}\n\n'
            'Answer:\n{"matched": [1]}'
        )
        self.assertEqual(
            extract_json_object(reply, required_key="matched")["matched"], [1]
        )
