"""Tests for datasets — where cases come from.

Two properties carry most of the weight here:

- **No business vocabulary.** Every column name is a parameter, so a
  specific project's schema is a configuration of the loader rather than a
  special case inside it. A test asserts the module contains no such name.
- **Deterministic splits.** The engine compares each candidate against a
  baseline, so a split that shifted between runs would let a score change
  come from a different set of cases rather than from the change under
  test.

Stdlib only, no network, no engine imports.
"""

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "plugin" / "skills" / "skill-evolver" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from datasets import (  # noqa: E402
    DEFAULT_SPLITS,
    CaseLoader,
    ColumnMap,
    CsvCaseLoader,
    JsonCaseLoader,
    MissingColumns,
    describe_splits,
    load_cases,
    split_cases,
)


class TempFileTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, text: str, encoding="utf-8") -> Path:
        p = self.tmp / name
        p.write_text(text, encoding=encoding)
        return p


# ─────────────────────────────────────────────
# Generality
# ─────────────────────────────────────────────

class GeneralityTests(unittest.TestCase):
    def test_module_names_no_business_column_in_code(self):
        """I3: a project's vocabulary must not be inside a general tool.

        Checked against the real column names of a dataset this engine is
        meant to load. Prose is excluded and only code is examined —
        explaining *why* a name must not be hard-coded requires naming it,
        and a check that flagged its own rationale would be deleted, which
        would leave the real invariant unguarded.
        """
        tree = ast.parse((SCRIPTS / "datasets.py").read_text())
        literals = {
            node.value.casefold()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        # Docstrings are Constant nodes too, so drop the ones that are
        # statements in their own right.
        docstrings = {
            node.value.value.casefold()
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        code_literals = literals - docstrings
        names = {node.id.casefold() for node in ast.walk(tree)
                 if isinstance(node, ast.Name)}
        names |= {node.attr.casefold() for node in ast.walk(tree)
                  if isinstance(node, ast.Attribute)}
        names |= {a.arg.casefold() for node in ast.walk(tree)
                  if isinstance(node, ast.arguments) for a in node.args if a.arg}

        for business in ("question", "knowledge_points", "gt_answer", "catalog",
                         "file_path", "from_source", "link"):
            for literal in code_literals:
                self.assertNotIn(
                    business, literal,
                    f"business column {business!r} appears in code literal "
                    f"{literal!r}",
                )
            self.assertNotIn(business, names)

    def test_column_map_defaults_are_neutral(self):
        """Defaults must not presume any particular schema."""
        cols = ColumnMap()
        self.assertEqual(cols.id, "id")
        self.assertEqual(list(cols.input), ["input"])
        for optional in (cols.points, cols.expected, cols.expectations,
                         cols.stratify, cols.split):
            self.assertIsNone(optional)

    def test_module_imports_no_engine_code(self):
        """This layer knows nothing about grading."""
        tree = ast.parse((SCRIPTS / "datasets.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("graders", "scoring", "judgment", "target",
                          "evaluators", "llm", "binary_judge"):
            self.assertNotIn(forbidden, imported)


# ─────────────────────────────────────────────
# Loading
# ─────────────────────────────────────────────

class CsvLoadingTests(TempFileTestCase):
    HEADER = "ident,q,ctx,pts,cat,which\n"

    def dataset(self, body: str, name="gt.csv") -> Path:
        return self.write(name, self.HEADER + body)

    def columns(self, **overrides) -> ColumnMap:
        base = dict(id="ident", input=("q",), points="pts")
        base.update(overrides)
        return ColumnMap(**base)

    def test_columns_are_mapped_by_configuration(self):
        p = self.dataset('a1,how do I X,,"[""point one""]",,\n')
        cases = CsvCaseLoader(p, self.columns()).load()
        self.assertEqual(cases[0]["id"], "a1")
        self.assertEqual(cases[0]["input"], "how do I X")
        self.assertEqual(cases[0]["points"], ["point one"])

    def test_several_input_columns_are_kept_individually_and_joined(self):
        """Collapsing them at load time would lose which field was which."""
        p = self.dataset("a1,the question,the context,,,\n")
        cases = CsvCaseLoader(p, self.columns(input=("q", "ctx"))).load()
        self.assertEqual(cases[0]["inputs"], {"q": "the question", "ctx": "the context"})
        self.assertIn("the question", cases[0]["input"])
        self.assertIn("the context", cases[0]["input"])

    def test_empty_input_fields_are_not_joined_as_blank_lines(self):
        p = self.dataset("a1,only this,,,,\n")
        cases = CsvCaseLoader(p, self.columns(input=("q", "ctx"))).load()
        self.assertEqual(cases[0]["input"], "only this")

    def test_missing_columns_are_reported_once_with_what_exists(self):
        """Failing per-row would bury the cause in noise."""
        p = self.dataset("a1,q,,,,\n")
        with self.assertRaises(MissingColumns) as ctx:
            CsvCaseLoader(p, ColumnMap(id="ident", input=("absent",))).load()
        message = str(ctx.exception)
        self.assertIn("absent", message)
        self.assertIn("ident", message)

    def test_every_missing_column_is_named_at_once(self):
        p = self.dataset("a1,q,,,,\n")
        with self.assertRaises(MissingColumns) as ctx:
            CsvCaseLoader(
                p, ColumnMap(id="ident", input=("nope1",), points="nope2")
            ).load()
        self.assertIn("nope1", str(ctx.exception))
        self.assertIn("nope2", str(ctx.exception))

    def test_quoted_multiline_cell_is_one_case(self):
        """A reference answer with paragraph breaks is one field.

        A reader treating its newlines as row boundaries would report
        several times as many cases as exist.
        """
        p = self.dataset('a1,"line one\n\nline two",,,,\n')
        cases = CsvCaseLoader(p, self.columns()).load()
        self.assertEqual(len(cases), 1)
        self.assertIn("line two", cases[0]["input"])

    def test_byte_order_mark_does_not_break_the_first_column(self):
        """Spreadsheet exports carry one, and it attaches to column one."""
        p = self.write("bom.csv", "\ufeff" + self.HEADER + "a1,q,,,,\n")
        cases = CsvCaseLoader(p, self.columns()).load()
        self.assertEqual(cases[0]["id"], "a1")

    def test_a_very_large_cell_is_read(self):
        """The stdlib default cap makes the reader fail on the whole file."""
        big = "x" * 200000
        p = self.dataset(f'a1,"{big}",,,,\n')
        cases = CsvCaseLoader(p, self.columns()).load()
        self.assertEqual(len(cases[0]["input"]), 200000)

    def test_empty_file_yields_no_cases_without_erroring(self):
        p = self.write("empty.csv", self.HEADER)
        self.assertEqual(CsvCaseLoader(p, self.columns()).load(), [])

    def test_a_completely_empty_file_yields_no_cases(self):
        """No header at all: nothing to validate, nothing to load."""
        p = self.write("blank.csv", "")
        self.assertEqual(CsvCaseLoader(p, self.columns()).load(), [])

    def test_tab_delimited_is_supported(self):
        p = self.write("gt.tsv", "ident\tq\na1\tthe question\n")
        cases = CsvCaseLoader(
            p, ColumnMap(id="ident", input=("q",)), delimiter="\t"
        ).load()
        self.assertEqual(cases[0]["input"], "the question")

    def test_file_order_is_preserved(self):
        p = self.dataset("a1,q1,,,,\na2,q2,,,,\na3,q3,,,,\n")
        ids = [c["id"] for c in CsvCaseLoader(p, self.columns()).load()]
        self.assertEqual(ids, ["a1", "a2", "a3"])

    def test_extra_columns_are_carried_through_for_audit(self):
        p = self.dataset("a1,q,,,the-category,\n")
        cases = CsvCaseLoader(p, self.columns(extra=("cat",))).load()
        self.assertEqual(cases[0]["extra"]["cat"], "the-category")

    def test_expected_answer_column(self):
        p = self.dataset("a1,q,the reference answer,,,\n")
        cases = CsvCaseLoader(p, self.columns(expected="ctx")).load()
        self.assertEqual(cases[0]["expected"], "the reference answer")

    def test_expectations_column_parses_json_assertions(self):
        p = self.dataset(
            'a1,q,"[{""type"": ""contains"", ""value"": ""x""}]",,,\n'
        )
        cases = CsvCaseLoader(p, self.columns(expectations="ctx")).load()
        self.assertEqual(cases[0]["expectations"][0]["type"], "contains")

    def test_malformed_expectations_become_empty_not_an_exception(self):
        """One bad row must not abort a run several layers later."""
        p = self.dataset("a1,q,not json at all,,,\n")
        cases = CsvCaseLoader(p, self.columns(expectations="ctx")).load()
        self.assertEqual(cases[0]["expectations"], [])

    def test_non_mapping_expectation_entries_are_dropped(self):
        p = self.dataset('a1,q,"[""just a string""]",,,\n')
        cases = CsvCaseLoader(p, self.columns(expectations="ctx")).load()
        self.assertEqual(cases[0]["expectations"], [])


class CaseIdTests(TempFileTestCase):
    def test_a_missing_id_column_falls_back_to_a_content_digest(self):
        """Not to the row number.

        A row number changes when the file is sorted, and a case whose id
        moves lands in a different split — silently invalidating every
        comparison with earlier runs.
        """
        p = self.write("gt.csv", "q\nthe question\n")
        cases = CsvCaseLoader(p, ColumnMap(input=("q",))).load()
        self.assertTrue(cases[0]["id"].startswith("auto-"))

    def test_the_derived_id_survives_reordering(self):
        a = self.write("a.csv", "q\nfirst\nsecond\n")
        b = self.write("b.csv", "q\nsecond\nfirst\n")
        cols = ColumnMap(input=("q",))
        ids_a = {c["input"]: c["id"] for c in CsvCaseLoader(a, cols).load()}
        ids_b = {c["input"]: c["id"] for c in CsvCaseLoader(b, cols).load()}
        self.assertEqual(ids_a, ids_b)

    def test_different_content_yields_different_ids(self):
        p = self.write("gt.csv", "q\nfirst\nsecond\n")
        cases = CsvCaseLoader(p, ColumnMap(input=("q",))).load()
        self.assertNotEqual(cases[0]["id"], cases[1]["id"])

    def test_a_blank_id_cell_falls_back_too(self):
        p = self.write("gt.csv", "ident,q\n,the question\n")
        cases = CsvCaseLoader(p, ColumnMap(id="ident", input=("q",))).load()
        self.assertTrue(cases[0]["id"].startswith("auto-"))


class PointsParsingTests(TempFileTestCase):
    def load(self, cell, **kwargs):
        p = self.write("gt.csv", "q,pts\n" + f'the question,"{cell}"\n')
        return CsvCaseLoader(
            p, ColumnMap(input=("q",), points="pts"), **kwargs
        ).load()[0]["points"]

    def test_a_json_array(self):
        self.assertEqual(self.load('[""one"", ""two""]'), ["one", "two"])

    def test_a_json_array_of_non_strings(self):
        p = self.write("gt.csv", "q,pts\nq,\"[1, 2]\"\n")
        cases = CsvCaseLoader(p, ColumnMap(input=("q",), points="pts")).load()
        self.assertEqual(cases[0]["points"], ["1", "2"])

    def test_blank_entries_in_the_array_are_dropped(self):
        self.assertEqual(self.load('[""one"", """", ""  ""]'), ["one"])

    def test_plain_text_stays_one_point(self):
        """Guessing a separator would inflate every score's denominator."""
        self.assertEqual(
            self.load("one point, with a comma in it"),
            ["one point, with a comma in it"],
        )

    def test_a_delimiter_is_used_only_when_configured(self):
        self.assertEqual(
            self.load("one;two", points_delimiter=";"), ["one", "two"]
        )

    def test_malformed_json_falls_back_to_the_whole_cell(self):
        self.assertEqual(self.load("[not valid json"), ["[not valid json"])

    def test_a_json_object_becomes_one_point(self):
        self.assertEqual(self.load('{""a"": 1}'), ['{"a": 1}'])

    def test_an_empty_cell_yields_no_points(self):
        self.assertEqual(self.load(""), [])

    def test_a_real_list_passes_straight_through(self):
        """Relevant for the JSON loader, where cells are already typed."""
        loader = CaseLoader("unused.csv", ColumnMap(input=("q",), points="pts"))
        self.assertEqual(loader._parse_points(["a", "b"]), ["a", "b"])

    def test_none_yields_no_points(self):
        loader = CaseLoader("unused.csv")
        self.assertEqual(loader._parse_points(None), [])


class JsonLoadingTests(TempFileTestCase):
    COLS = ColumnMap(input=("q",), points="pts")

    def test_a_bare_array(self):
        p = self.write("gt.json", json.dumps([{"id": "a1", "q": "x", "pts": ["p"]}]))
        cases = JsonCaseLoader(p, self.COLS).load()
        self.assertEqual(cases[0]["points"], ["p"])

    def test_an_object_wrapping_records(self):
        p = self.write("gt.json", json.dumps({"evals": [{"id": "a1", "q": "x"}]}))
        self.assertEqual(len(JsonCaseLoader(p, ColumnMap(input=("q",))).load()), 1)

    def test_the_wrapper_key_is_configurable(self):
        p = self.write("gt.json", json.dumps({"cases": [{"id": "a1", "q": "x"}]}))
        loader = JsonCaseLoader(p, ColumnMap(input=("q",)), records_key="cases")
        self.assertEqual(len(loader.load()), 1)

    def test_json_lines(self):
        p = self.write(
            "gt.jsonl",
            '{"id": "a1", "q": "x"}\n{"id": "a2", "q": "y"}\n',
        )
        self.assertEqual(len(JsonCaseLoader(p, ColumnMap(input=("q",))).load()), 2)

    def test_blank_and_malformed_jsonl_lines_are_skipped(self):
        p = self.write(
            "gt.jsonl",
            '{"id": "a1", "q": "x"}\n\nnot json\n{"id": "a2", "q": "y"}\n',
        )
        self.assertEqual(len(JsonCaseLoader(p, ColumnMap(input=("q",))).load()), 2)

    def test_non_object_entries_are_skipped(self):
        p = self.write("gt.json", json.dumps([{"id": "a1", "q": "x"}, "junk", 42]))
        self.assertEqual(len(JsonCaseLoader(p, ColumnMap(input=("q",))).load()), 1)

    def test_a_lone_object_without_the_wrapper_key_is_one_record(self):
        """Silently loading nothing is the worse failure.

        An empty run looks like a clean one, so an object that does not
        hold records under the wrapper key is treated as a record itself —
        and if it lacks the mapped columns, that is reported.
        """
        p = self.write("gt.json", json.dumps({"q": "x", "id": "a1"}))
        cases_ = JsonCaseLoader(p, ColumnMap(input=("q",))).load()
        self.assertEqual([c["id"] for c in cases_], ["a1"])

    def test_an_unrecognised_object_reports_the_missing_columns(self):
        p = self.write("gt.json", json.dumps({"other": []}))
        with self.assertRaises(MissingColumns):
            JsonCaseLoader(p, ColumnMap(input=("q",))).load()

    def test_an_empty_wrapper_list_yields_no_cases(self):
        """An explicitly empty record list is a real, valid answer."""
        p = self.write("gt.json", json.dumps({"evals": []}))
        self.assertEqual(JsonCaseLoader(p, ColumnMap(input=("q",))).load(), [])


class LoadCasesDispatchTests(TempFileTestCase):
    def test_csv_by_extension(self):
        p = self.write("gt.csv", "q\nx\n")
        self.assertEqual(len(load_cases(p, ColumnMap(input=("q",)))), 1)

    def test_tsv_uses_a_tab_without_being_told(self):
        p = self.write("gt.tsv", "q\tid\nx\ta1\n")
        cases = load_cases(p, ColumnMap(input=("q",)))
        self.assertEqual(cases[0]["id"], "a1")

    def test_json_by_extension(self):
        p = self.write("gt.json", json.dumps([{"q": "x"}]))
        self.assertEqual(len(load_cases(p, ColumnMap(input=("q",)))), 1)

    def test_jsonl_by_extension(self):
        p = self.write("gt.jsonl", '{"q": "x"}\n')
        self.assertEqual(len(load_cases(p, ColumnMap(input=("q",)))), 1)

    def test_an_unknown_extension_names_the_supported_ones(self):
        p = self.write("gt.xlsx", "binary-ish")
        with self.assertRaises(ValueError) as ctx:
            load_cases(p, ColumnMap(input=("q",)))
        self.assertIn(".csv", str(ctx.exception))

    def test_a_string_path_is_accepted(self):
        p = self.write("gt.csv", "q\nx\n")
        self.assertEqual(len(load_cases(str(p), ColumnMap(input=("q",)))), 1)

    def test_an_explicit_delimiter_is_honoured_for_csv(self):
        p = self.write("gt.csv", "q|id\nx|a1\n")
        cases = load_cases(p, ColumnMap(input=("q",)), delimiter="|")
        self.assertEqual(cases[0]["id"], "a1")


# ─────────────────────────────────────────────
# Splitting
# ─────────────────────────────────────────────

def cases(n, stratum=None, prefix="c"):
    out = []
    for i in range(n):
        case = {"id": f"{prefix}{i}", "input": f"q{i}"}
        if stratum is not None:
            case["stratum"] = stratum
        out.append(case)
    return out


class SplitDeterminismTests(unittest.TestCase):
    def test_the_same_input_splits_the_same_way(self):
        """A requirement, not a convenience.

        A split that shifted between runs would let a score change come
        from a different set of cases rather than from the change under
        test.
        """
        a = split_cases(cases(50))
        b = split_cases(cases(50))
        for name in a:
            self.assertEqual([c["id"] for c in a[name]], [c["id"] for c in b[name]])

    def test_adding_a_case_does_not_reshuffle_the_others(self):
        """Otherwise every earlier baseline becomes incomparable."""
        before = split_cases(cases(60))
        after = split_cases(cases(61))
        moved = 0
        placement_before = {c["id"]: n for n, cs in before.items() for c in cs}
        placement_after = {c["id"]: n for n, cs in after.items() for c in cs}
        for case_id, name in placement_before.items():
            if placement_after.get(case_id) != name:
                moved += 1
        self.assertLessEqual(moved, 3, f"{moved} cases changed split")

    def test_input_order_does_not_affect_the_result(self):
        forward = split_cases(cases(40))
        backward = split_cases(list(reversed(cases(40))))
        for name in forward:
            self.assertEqual(
                sorted(c["id"] for c in forward[name]),
                sorted(c["id"] for c in backward[name]),
            )

    def test_split_weight_order_does_not_affect_the_result(self):
        """Two callers writing the same splits differently must agree."""
        a = split_cases(cases(40), {"dev": 0.7, "holdout": 0.3})
        b = split_cases(cases(40), {"holdout": 0.3, "dev": 0.7})
        self.assertEqual(
            sorted(c["id"] for c in a["dev"]),
            sorted(c["id"] for c in b["dev"]),
        )

    def test_a_salt_redraws_the_split(self):
        plain = split_cases(cases(40))
        salted = split_cases(cases(40), salt="round-2")
        self.assertNotEqual(
            [c["id"] for c in plain["dev"]], [c["id"] for c in salted["dev"]]
        )


class SplitProportionTests(unittest.TestCase):
    def test_every_case_lands_in_exactly_one_split(self):
        result = split_cases(cases(100))
        placed = [c["id"] for cs in result.values() for c in cs]
        self.assertEqual(len(placed), 100)
        self.assertEqual(len(set(placed)), 100)

    def test_proportions_are_roughly_respected(self):
        result = split_cases(cases(100))
        self.assertAlmostEqual(len(result["dev"]), 70, delta=3)
        self.assertAlmostEqual(len(result["holdout"]), 20, delta=3)
        self.assertAlmostEqual(len(result["regression"]), 10, delta=3)

    def test_weights_need_not_sum_to_one(self):
        result = split_cases(cases(90), {"dev": 2, "holdout": 1})
        self.assertAlmostEqual(len(result["dev"]), 60, delta=3)

    def test_a_single_split_takes_everything(self):
        result = split_cases(cases(20), {"dev": 1.0})
        self.assertEqual(len(result["dev"]), 20)

    def test_each_case_is_tagged_with_its_split(self):
        result = split_cases(cases(30))
        for name, group in result.items():
            for case in group:
                self.assertEqual(case["split"], name)

    def test_the_source_cases_are_not_mutated(self):
        source = cases(20)
        split_cases(source)
        self.assertNotIn("split", source[0])

    def test_no_cases_yields_empty_splits(self):
        result = split_cases([])
        self.assertEqual(set(result), set(DEFAULT_SPLITS))
        self.assertEqual(sum(len(v) for v in result.values()), 0)

    def test_empty_splits_is_rejected(self):
        with self.assertRaises(ValueError):
            split_cases(cases(10), {})

    def test_a_non_positive_weight_is_rejected(self):
        """A zero-weight split would be silently unreachable."""
        for bad in ({"dev": 0}, {"dev": 1, "holdout": -1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                split_cases(cases(10), bad)


class SplitStratificationTests(unittest.TestCase):
    def mixed(self):
        return (cases(30, stratum="alpha", prefix="a")
                + cases(30, stratum="beta", prefix="b")
                + cases(30, stratum="gamma", prefix="g"))

    def test_every_stratum_appears_in_every_split(self):
        """Unstratified, a whole category can land on one side.

        A candidate would then be measured against material the baseline
        never saw.
        """
        result = split_cases(self.mixed())
        for name, group in result.items():
            strata = {c["stratum"] for c in group}
            self.assertEqual(strata, {"alpha", "beta", "gamma"}, f"{name}: {strata}")

    def test_stratification_can_be_turned_off(self):
        result = split_cases(self.mixed(), stratify=False)
        self.assertEqual(sum(len(v) for v in result.values()), 90)

    def test_a_tiny_stratum_is_not_dropped(self):
        """Ordering then cutting gives each split its share of what exists.

        Hashing into buckets would give only the expected proportions, so a
        two-case stratum could vanish from a split entirely.
        """
        data = cases(60, stratum="big", prefix="b") + cases(2, stratum="rare", prefix="r")
        result = split_cases(data)
        placed = [c["id"] for cs in result.values() for c in cs if c["id"].startswith("r")]
        self.assertEqual(len(placed), 2)

    def test_a_single_case_stratum_lands_somewhere(self):
        data = cases(10, stratum="big", prefix="b") + cases(1, stratum="solo", prefix="s")
        result = split_cases(data)
        placed = [c for cs in result.values() for c in cs if c["stratum"] == "solo"]
        self.assertEqual(len(placed), 1)

    def test_cases_without_a_stratum_are_still_split(self):
        result = split_cases(cases(40))
        self.assertEqual(sum(len(v) for v in result.values()), 40)


class DeclaredSplitTests(unittest.TestCase):
    def test_a_declared_split_is_honoured(self):
        """A curator assigned it for a reason the hash cannot know."""
        data = [{"id": "c1", "split": "holdout"}, {"id": "c2", "split": "dev"}]
        result = split_cases(data)
        self.assertEqual([c["id"] for c in result["holdout"]], ["c1"])
        self.assertEqual([c["id"] for c in result["dev"]], ["c2"])

    def test_a_declared_split_name_outside_the_weights_is_kept(self):
        """Dropping it would silently discard curated data."""
        data = [{"id": "c1", "split": "smoke"}]
        result = split_cases(data, {"dev": 1.0})
        self.assertEqual([c["id"] for c in result["smoke"]], ["c1"])

    def test_declared_and_undeclared_cases_coexist(self):
        data = [{"id": "fixed", "split": "holdout"}] + cases(30)
        result = split_cases(data)
        self.assertIn("fixed", [c["id"] for c in result["holdout"]])
        self.assertEqual(sum(len(v) for v in result.values()), 31)

    def test_a_blank_declared_split_is_treated_as_undeclared(self):
        data = [{"id": "c1", "split": ""}] + cases(20)
        result = split_cases(data)
        self.assertEqual(sum(len(v) for v in result.values()), 21)


class DescribeSplitsTests(unittest.TestCase):
    def test_reports_counts_and_strata(self):
        """So a bad split is noticed before a run is spent on it."""
        result = split_cases(
            cases(30, stratum="alpha", prefix="a") + cases(30, stratum="beta", prefix="b")
        )
        summary = describe_splits(result)
        self.assertEqual(summary["total"], 60)
        self.assertIn("alpha", summary["splits"]["dev"]["strata"])
        self.assertIn("beta", summary["splits"]["dev"]["strata"])

    def test_handles_cases_without_strata(self):
        summary = describe_splits(split_cases(cases(10)))
        self.assertEqual(summary["total"], 10)


# ─────────────────────────────────────────────
# End to end against a realistic file
# ─────────────────────────────────────────────

class RealisticDatasetTests(TempFileTestCase):
    """A dataset shaped like one actually in use — as configuration only.

    The point of this test is that supporting such a file requires no code
    in the loader that knows anything about it.
    """

    CONTENT = (
        "id,question,link,knowledge_points,gt_answer,gt_answer_points,"
        "class,catalog\n"
        'a85c,how do I reclaim a seat?,https://example/1,"a note",'
        '"the reference answer","[""seats are released automatically""]",'
        "R1,licensing\n"
        'b91d,"multi-line\n\nquestion",https://example/2,"another note",'
        '"second answer","[""first point"", ""second point""]",'
        "R0,licensing\n"
        'c72f,third question,https://example/3,"note three",'
        '"third answer","[""only point""]",R1,contacts\n'
    )

    def test_loads_and_splits_with_configuration_alone(self):
        p = self.write("gt.csv", self.CONTENT)
        columns = ColumnMap(
            id="id",
            input=("question", "knowledge_points"),
            points="gt_answer_points",
            expected="gt_answer",
            stratify="catalog",
            extra=("link", "class"),
        )
        loaded = load_cases(p, columns)

        self.assertEqual(len(loaded), 3)
        self.assertEqual(loaded[0]["id"], "a85c")
        self.assertEqual(loaded[0]["points"], ["seats are released automatically"])
        self.assertEqual(loaded[1]["points"], ["first point", "second point"])
        self.assertIn("multi-line", loaded[1]["input"])
        self.assertIn("another note", loaded[1]["input"])
        self.assertEqual(loaded[0]["expected"], "the reference answer")
        self.assertEqual(loaded[0]["stratum"], "licensing")
        self.assertEqual(loaded[0]["extra"]["class"], "R1")

        result = split_cases(loaded, {"dev": 2, "holdout": 1})
        self.assertEqual(sum(len(v) for v in result.values()), 3)

    def test_a_different_schema_needs_only_a_different_map(self):
        """The generality claim, demonstrated rather than asserted."""
        p = self.write(
            "other.jsonl",
            '{"case_id": "x1", "prompt": "do the thing", '
            '"must_cover": ["alpha"], "topic": "t1"}\n',
        )
        cases_ = load_cases(
            p,
            ColumnMap(id="case_id", input=("prompt",), points="must_cover",
                      stratify="topic"),
        )
        self.assertEqual(cases_[0]["id"], "x1")
        self.assertEqual(cases_[0]["points"], ["alpha"])
        self.assertEqual(cases_[0]["stratum"], "t1")


class SplitColumnTests(TempFileTestCase):
    """A dataset may state its own split assignment."""

    def test_a_split_column_is_read_from_the_file(self):
        p = self.write("gt.csv", "q,which\nfirst,holdout\nsecond,dev\n")
        loaded = load_cases(p, ColumnMap(input=("q",), split="which"))
        self.assertEqual(loaded[0]["split"], "holdout")
        self.assertEqual(loaded[1]["split"], "dev")

    def test_a_blank_split_cell_leaves_the_case_unassigned(self):
        """So the hash can place it, rather than inventing a split named ''."""
        p = self.write("gt.csv", "q,which\nfirst,\n")
        loaded = load_cases(p, ColumnMap(input=("q",), split="which"))
        self.assertNotIn("split", loaded[0])

    def test_file_declared_splits_survive_splitting(self):
        p = self.write("gt.csv", "q,which\nfirst,holdout\nsecond,dev\n")
        loaded = load_cases(p, ColumnMap(input=("q",), split="which"))
        result = split_cases(loaded)
        self.assertEqual(len(result["holdout"]), 1)
        self.assertEqual(len(result["dev"]), 1)


class LoaderContractTests(unittest.TestCase):
    def test_the_base_loader_declares_rows_unimplemented(self):
        """Format handling is the one part a subclass must supply."""
        with self.assertRaises(NotImplementedError):
            CaseLoader("nowhere.csv").load()


class ExpectationsNormalisationTests(TempFileTestCase):
    def test_a_real_list_of_mappings_passes_through(self):
        p = self.write(
            "gt.json",
            json.dumps([{"q": "x", "asserts": [{"type": "contains", "value": "a"}]}]),
        )
        loaded = load_cases(p, ColumnMap(input=("q",), expectations="asserts"))
        self.assertEqual(loaded[0]["expectations"][0]["value"], "a")

    def test_a_single_mapping_becomes_a_one_item_list(self):
        p = self.write(
            "gt.json",
            json.dumps([{"q": "x", "asserts": {"type": "contains", "value": "a"}}]),
        )
        loaded = load_cases(p, ColumnMap(input=("q",), expectations="asserts"))
        self.assertEqual(len(loaded[0]["expectations"]), 1)

    def test_a_null_cell_yields_no_expectations(self):
        p = self.write("gt.json", json.dumps([{"q": "x", "asserts": None}]))
        loaded = load_cases(p, ColumnMap(input=("q",), expectations="asserts"))
        self.assertEqual(loaded[0]["expectations"], [])

    def test_a_blank_cell_yields_no_expectations(self):
        p = self.write("gt.csv", "q,asserts\nx,\n")
        loaded = load_cases(p, ColumnMap(input=("q",), expectations="asserts"))
        self.assertEqual(loaded[0]["expectations"], [])

    def test_json_that_decodes_to_a_scalar_yields_no_expectations(self):
        p = self.write("gt.csv", "q,asserts\nx,42\n")
        loaded = load_cases(p, ColumnMap(input=("q",), expectations="asserts"))
        self.assertEqual(loaded[0]["expectations"], [])


class JsonlWithObjectFirstLineTests(TempFileTestCase):
    def test_multi_line_jsonl_starting_with_an_object_is_read_as_lines(self):
        """Invalid as one JSON document, valid as JSON Lines."""
        p = self.write(
            "gt.json",
            '{"id": "a1", "q": "x"}\n{"id": "a2", "q": "y"}\n',
        )
        loaded = JsonCaseLoader(p, ColumnMap(input=("q",))).load()
        self.assertEqual([c["id"] for c in loaded], ["a1", "a2"])

    def test_a_file_starting_with_neither_bracket_is_read_as_lines(self):
        """A leading comment or blank line must not prevent loading."""
        p = self.write(
            "gt.jsonl",
            '\n  \n{"id": "a1", "q": "x"}\n',
        )
        loaded = JsonCaseLoader(p, ColumnMap(input=("q",))).load()
        self.assertEqual([c["id"] for c in loaded], ["a1"])

    def test_non_object_lines_are_skipped(self):
        p = self.write("gt.jsonl", '"a bare string"\n42\n{"id": "a1", "q": "x"}\n')
        loaded = JsonCaseLoader(p, ColumnMap(input=("q",))).load()
        self.assertEqual([c["id"] for c in loaded], ["a1"])


class SplitBoundaryTests(unittest.TestCase):
    def test_the_last_split_absorbs_any_rounding_remainder(self):
        """Float boundaries must not leave a case unassigned."""
        result = split_cases(cases(7), {"a": 1, "b": 1, "c": 1})
        self.assertEqual(sum(len(v) for v in result.values()), 7)

    def test_many_odd_sizes_all_place_every_case(self):
        for n in range(1, 25):
            with self.subTest(n=n):
                result = split_cases(cases(n))
                self.assertEqual(sum(len(v) for v in result.values()), n)

    def test_weights_that_accumulate_short_of_one_still_place_every_case(self):
        """The float-accumulation guard, exercised directly.

        Thirds cannot be represented exactly, so the running total can stop
        a hair below 1.0 and leave the highest-positioned case matching no
        boundary. Losing a case from the run is worse than one case sitting
        a split over from where the arithmetic intended.
        """
        thirds = {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}
        for n in (1, 3, 7, 11, 50, 97):
            with self.subTest(n=n):
                result = split_cases(cases(n), thirds)
                self.assertEqual(sum(len(v) for v in result.values()), n)

    def test_the_guard_returns_the_last_split_by_name_order(self):
        from datasets import _assign, _cumulative

        boundaries = _cumulative({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3})
        # Force a fraction above every boundary to reach the fallback.
        self.assertEqual(_assign(999, 1000, [("a", 0.1), ("b", 0.2)]), "b")
        self.assertEqual(boundaries[-1][0], "c")


if __name__ == "__main__":
    unittest.main()
