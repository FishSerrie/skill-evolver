"""Where cases come from.

Single responsibility: turn an external dataset into the plain case dicts
the rest of the engine consumes, and split them into evaluation sets.

Why every column name is a parameter
------------------------------------
Column names belong to whoever produced the data, not to this engine. A
loader with ``question_col="question"`` baked in works for exactly one
file and silently fails on the next; worse, it makes a specific project's
vocabulary part of a general tool. So the mapping from "this file's
columns" to "the fields a grader reads" is configuration, supplied per
dataset, and this module holds no knowledge of any particular schema.

A concrete dataset is therefore a *configuration* of the loader, never a
subclass or a special case of it.

Splitting, and why it is deterministic
--------------------------------------
The engine compares a candidate against a baseline across iterations. If
the split shifted between runs, a score change could come from a different
set of cases rather than from the change being tested, and the whole
comparison would be meaningless. Splits are therefore derived from a hash
of the case id: the same dataset always yields the same partition, without
storing it anywhere, and adding a case does not reshuffle the others.

Stdlib only. No LLM, no engine imports — this layer knows nothing about
grading.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

__all__ = [
    "ColumnMap",
    "CaseLoader",
    "CsvCaseLoader",
    "JsonCaseLoader",
    "load_cases",
    "parse_points",
    "read_records",
    "split_cases",
    "DEFAULT_SPLITS",
]

# Fractions, not counts, because a dataset's size is not known here. Names
# match the evaluation stages the engine already runs: `dev` is read every
# iteration, `holdout` is withheld from whatever proposes changes so that
# improvements have to generalise, and `regression` guards against
# re-breaking what already worked.
DEFAULT_SPLITS: dict[str, float] = {"dev": 0.7, "holdout": 0.2, "regression": 0.1}

# Raised the CSV field cap because a single ground-truth cell routinely
# holds a whole reference answer, and the stdlib default (128 KiB) makes
# the reader fail on the *file* rather than reporting the oversized field —
# an error that points at the wrong thing.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


@dataclass(frozen=True)
class ColumnMap:
    """Which source columns supply which case fields.

    Only ``id`` has a default, and only because a dataset without stable
    identifiers cannot be split reproducibly at all — so one is derived
    from the row's content when the column is absent (see
    :meth:`CaseLoader._case_id`).

    Attributes:
        id: column holding a stable per-case identifier.
        input: columns forming the input handed to the thing being
            optimized. A sequence rather than one column because a task
            may take several fields (a question plus supporting material),
            and collapsing them here would lose which was which.
        points: column holding expected points, pre-split. Either a JSON
            array or a delimited string.
        expected: column holding a full reference answer, for graders that
            compare against text rather than enumerated points.
        expectations: column holding machine-checkable assertions as JSON.
        stratify: column whose values should be represented proportionally
            in every split.
        split: column that already states which split a row belongs to. When
            present it is honoured verbatim — a curator's deliberate
            assignment outranks any hash.
        extra: columns to carry through untouched, for feedback and audit.
    """

    id: str = "id"
    input: Sequence[str] = ("input",)
    points: str | None = None
    expected: str | None = None
    expectations: str | None = None
    stratify: str | None = None
    split: str | None = None
    extra: Sequence[str] = ()

    def required(self) -> list[str]:
        """Columns that must exist for the mapping to be usable.

        ``id`` is excluded: it has a documented fallback. The rest are
        named explicitly by the caller, so their absence is a configuration
        error worth reporting up front rather than a row-by-row surprise.
        """
        names = list(self.input)
        for optional in (self.points, self.expected, self.expectations,
                         self.stratify, self.split):
            if optional:
                names.append(optional)
        names.extend(self.extra)
        return names


class MissingColumns(ValueError):
    """The dataset lacks columns the mapping names.

    Reported once, listing every missing name alongside what the file does
    contain. Failing per-row instead would bury the cause in noise, and
    failing on only the first missing column would take several runs to
    resolve.
    """


class CaseLoader:
    """Turns rows into cases. Format-agnostic; subclasses supply the rows.

    The row-to-case translation is identical whatever the file format, so
    it lives here once. A subclass implements only :meth:`_rows`, which is
    the sole part that differs between CSV, JSON, and anything added later.
    """

    def __init__(
        self,
        path: Path | str,
        columns: ColumnMap | None = None,
        points_delimiter: str | None = None,
    ):
        """
        Args:
            path: dataset file.
            columns: which columns supply which fields.
            points_delimiter: when a points cell is not JSON, split it on
                this string. Left as ``None`` by default so that a
                non-JSON cell stays a single point rather than being
                silently chopped at a separator the author never intended.
        """
        self.path = Path(path)
        self.columns = columns or ColumnMap()
        self.points_delimiter = points_delimiter

    # ── format-specific ──────────────────────

    def _rows(self) -> Iterable[Mapping]:
        raise NotImplementedError

    # ── shared ───────────────────────────────

    def load(self) -> list[dict]:
        """Read every row and return cases, in file order.

        Order is preserved so that a human comparing a case to its source
        row can find it, and so a run is reproducible.
        """
        rows = list(self._rows())
        if rows:
            self._check_columns(rows[0])
        return [self._to_case(row, i) for i, row in enumerate(rows)]

    def _check_columns(self, sample: Mapping) -> None:
        missing = [name for name in self.columns.required() if name not in sample]
        if missing:
            raise MissingColumns(
                f"{self.path} is missing column(s) {missing}; "
                f"it has {sorted(sample)}"
            )

    def _to_case(self, row: Mapping, index: int) -> dict:
        cols = self.columns
        case: dict = {"id": self._case_id(row, index)}

        # Inputs are kept both individually and joined. The individual
        # fields let a caller build its own prompt; the joined form is what
        # a target that takes one blob of text receives. Providing both
        # here stops each caller from choosing its own join and producing
        # subtly different inputs from the same data.
        inputs = {name: _text(row.get(name)) for name in cols.input}
        case["inputs"] = inputs
        case["input"] = "\n\n".join(v for v in inputs.values() if v)

        if cols.points:
            case["points"] = self._parse_points(row.get(cols.points))
        if cols.expected:
            case["expected"] = _text(row.get(cols.expected))
        if cols.expectations:
            case["expectations"] = _parse_expectations(row.get(cols.expectations))
        if cols.stratify:
            case["stratum"] = _text(row.get(cols.stratify))
        if cols.split:
            declared = _text(row.get(cols.split))
            if declared:
                case["split"] = declared
        if cols.extra:
            case["extra"] = {name: row.get(name) for name in cols.extra}
        return case

    def _case_id(self, row: Mapping, index: int) -> str:
        """Stable identifier for a row.

        Falls back to a digest of the row's own content rather than to the
        row number, because a row number changes when the file is sorted or
        a row is inserted — and a case whose id moves lands in a different
        split, which silently invalidates every comparison against earlier
        runs.
        """
        declared = _text(row.get(self.columns.id))
        if declared:
            return declared
        payload = json.dumps(
            {k: _text(v) for k, v in sorted(row.items())},
            ensure_ascii=False,
        )
        return "auto-" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

    def _parse_points(self, raw) -> list[str]:
        """Normalise a points cell into a list of strings.

        Thin wrapper over :func:`parse_points`, which is module-level so
        that anything needing the same normalisation shares this exact
        behaviour instead of reimplementing it. Two readings of one cell
        would disagree about how many expectations it holds, and that count
        is the denominator of every score computed from it.
        """
        return parse_points(raw, self.points_delimiter)


class CsvCaseLoader(CaseLoader):
    """Loads cases from a delimited text file.

    Handles the quoted multi-line cells that ground-truth files invariably
    contain: a reference answer with paragraph breaks is one field, and a
    reader that treated its newlines as row boundaries would report several
    times as many cases as exist.
    """

    def __init__(self, path, columns=None, delimiter: str = ",",
                 encoding: str = "utf-8-sig", **kwargs):
        """
        Args:
            encoding: defaults to ``utf-8-sig`` because spreadsheet exports
                routinely carry a byte-order mark, and with plain ``utf-8``
                it attaches itself to the first column's name — so the
                mapping fails on a column that is visibly present.
        """
        super().__init__(path, columns, **kwargs)
        self.delimiter = delimiter
        self.encoding = encoding

    def _rows(self):
        with open(self.path, newline="", encoding=self.encoding) as fh:
            yield from csv.DictReader(fh, delimiter=self.delimiter)


class JsonCaseLoader(CaseLoader):
    """Loads cases from JSON or JSON Lines.

    Accepts a bare array, an object wrapping one under a named key, or one
    object per line, because all three are in circulation and requiring a
    specific one would push a conversion step onto every caller.
    """

    def __init__(self, path, columns=None, records_key: str = "evals", **kwargs):
        super().__init__(path, columns, **kwargs)
        self.records_key = records_key

    def _rows(self):
        text = self.path.read_text(encoding="utf-8")
        stripped = text.lstrip()
        if stripped.startswith("["):
            records = json.loads(text)
        elif stripped.startswith("{"):
            records = self._rows_from_object_text(text)
        else:
            records = _read_jsonl(text)
        for record in records:
            if isinstance(record, Mapping):
                yield record

    def _rows_from_object_text(self, text: str) -> list:
        """Interpret text that starts with ``{``.

        Three shapes begin that way and they are genuinely ambiguous, so the
        decision is made on evidence rather than on the extension:

        - a wrapper object holding records under a named key,
        - JSON Lines, where the first line merely happens to be an object,
        - a single record on its own.

        The wrapper is only accepted when the key is actually present and
        holds a list. Trusting the shape alone would turn a one-record
        JSON Lines file into "a wrapper with no records" and silently load
        nothing — the worst outcome, because an empty run looks like a
        clean one.
        """
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            # Several objects, one per line: valid JSON Lines, invalid JSON.
            return _read_jsonl(text)
        # Valid JSON that started with `{` is necessarily an object, so no
        # further type check is reachable here.
        wrapped = decoded.get(self.records_key)
        if isinstance(wrapped, (list, tuple)):
            return list(wrapped)
        # A lone object with no records key is one record.
        return [decoded]


def _read_jsonl(text: str) -> list:
    """Decode one JSON value per line, skipping blank and unparseable lines.

    Does not filter by type: :meth:`JsonCaseLoader._rows` already keeps only
    mappings, and filtering in both places would be two rules to keep in
    step for no gain.
    """
    records = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def load_cases(path: Path | str, columns: ColumnMap | None = None,
               **kwargs) -> list[dict]:
    """Load cases, picking a loader from the file extension.

    The one place that maps a suffix to a loader. Concentrating it here is
    what lets callers stay format-agnostic; the alternative — each caller
    checking the extension — would scatter the same branch and guarantee
    the copies eventually disagree.
    """
    return _loader_for(Path(path), columns, **kwargs).load()


def read_records(path: Path | str, **kwargs) -> list[dict]:
    """Read a dataset as its own raw rows, with no column mapping applied.

    Same format handling as :func:`load_cases` — CSV quoting, the
    byte-order mark spreadsheets add, JSON versus JSON Lines — but the rows
    come back with the column names the file actually uses, and no column
    is required to be present.

    This exists for callers that must inspect a dataset *before* anyone has
    decided how its columns map onto case fields. The L1 gate is the case in
    point: it runs before an evaluator is configured, on whatever file the
    user pointed at. Using :func:`load_cases` there made the gate reject the
    engine's own historical format — every existing ``evals.json`` uses
    ``prompt`` / ``assertions`` — by reporting a missing ``input`` column,
    because ``load_cases`` applies the default mapping's requirements.

    A caller that knows the mapping should use :func:`load_cases` instead.
    Raw rows put the burden of interpreting column names back on the
    caller, which is exactly what ``ColumnMap`` exists to avoid.
    """
    return [dict(row) for row in _loader_for(Path(path), None, **kwargs)._rows()]


def _loader_for(path: Path, columns: ColumnMap | None, **kwargs) -> CaseLoader:
    """Pick the loader for a file's extension.

    Extracted so that reading mapped cases and reading raw rows cannot
    disagree about which formats are supported — two copies of this branch
    would eventually accept different sets of extensions, and the error a
    caller saw would depend on which entry point it happened to use.
    """
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        delimiter = "\t" if suffix == ".tsv" else kwargs.pop("delimiter", ",")
        return CsvCaseLoader(path, columns, delimiter=delimiter, **kwargs)
    if suffix in (".json", ".jsonl", ".ndjson"):
        return JsonCaseLoader(path, columns, **kwargs)
    raise ValueError(
        f"unsupported dataset format {suffix!r} for {path}; "
        f"expected .csv, .tsv, .json, or .jsonl"
    )



# ─────────────────────────────────────────────
# Splitting
# ─────────────────────────────────────────────

def split_cases(
    cases: Sequence[Mapping],
    splits: Mapping[str, float] | None = None,
    stratify: bool = True,
    salt: str = "",
) -> dict[str, list[dict]]:
    """Partition cases into named evaluation sets.

    Deterministic by construction: a case's set is a function of its id, so
    the same dataset always splits the same way. That is a requirement, not
    a convenience — the engine compares each candidate against a baseline,
    and a split that shifted between runs would let a score change come
    from a different set of cases rather than from the change under test.

    Cases that already declare a ``split`` keep it. A curator who assigned
    one did so for a reason the hash cannot know.

    Args:
        cases: the cases to partition.
        splits: name → fraction. Fractions need not sum to 1; they are
            treated as relative weights, so ``{"dev": 2, "holdout": 1}``
            works as expected.
        stratify: distribute each ``stratum`` value proportionally across
            splits. On by default: with a mixed dataset, an unstratified
            split can leave a whole category on one side, and a candidate
            would then be measured against material the baseline never saw.
        salt: changes the assignment. For deliberately re-drawing a split;
            leave empty for the reproducible default.

    Raises:
        ValueError: if ``splits`` is empty or holds a non-positive weight —
            a zero-weight split would be silently unreachable, and a
            negative one has no meaning.
    """
    # `is None` rather than a falsy test: an explicitly empty mapping is a
    # configuration mistake and must be reported, whereas a falsy test
    # would silently substitute the defaults and produce a partition the
    # caller never asked for.
    weights = dict(DEFAULT_SPLITS if splits is None else splits)
    if not weights:
        raise ValueError("splits must name at least one split")
    for name, weight in weights.items():
        if weight <= 0:
            raise ValueError(
                f"split {name!r} has weight {weight}; weights must be positive"
            )

    result: dict[str, list[dict]] = {name: [] for name in weights}

    declared, undeclared = [], []
    for case in cases:
        (declared if case.get("split") else undeclared).append(case)

    for case in declared:
        result.setdefault(str(case["split"]), []).append(dict(case))

    groups: dict[str, list[Mapping]] = defaultdict(list)
    for case in undeclared:
        key = str(case.get("stratum", "")) if stratify else ""
        groups[key].append(case)

    boundaries = _cumulative(weights)
    for key in sorted(groups):
        # Ordering by hash rather than by file position: within a stratum
        # the sequence must not depend on how the file happened to be
        # sorted, or re-sorting the source would redraw the split.
        ordered = sorted(
            groups[key],
            key=lambda c: _digest(f"{salt}|{key}|{c.get('id', '')}"),
        )
        for position, case in enumerate(ordered):
            name = _assign(position, len(ordered), boundaries)
            result[name].append({**case, "split": name})

    return result


def _cumulative(weights: Mapping[str, float]) -> list[tuple[str, float]]:
    """Split names with their cumulative share of the whole, in a fixed order.

    Sorted by name so the boundaries do not depend on dict insertion order:
    two callers passing the same splits written in a different order must
    get the same partition, or "deterministic" would only hold within one
    caller.
    """
    total = sum(weights.values())
    running = 0.0
    boundaries = []
    for name in sorted(weights):
        running += weights[name]
        boundaries.append((name, running / total))
    return boundaries


def _assign(position: int, count: int, boundaries: Sequence[tuple[str, float]]) -> str:
    """Which split the case at ``position`` of ``count`` belongs to.

    Assigns by position within a hash-ordered sequence rather than by
    hashing into a bucket directly. The difference matters on small
    datasets: hashing into buckets gives only the *expected* proportions,
    so a 10-case stratum can easily leave a split empty, whereas ordering
    then cutting gives each split its share of whatever is available.
    """
    fraction = (position + 0.5) / count if count else 0.0
    for name, upper in boundaries:
        if fraction <= upper:
            return name
    # Reached only if the cumulative shares stop just short of 1.0 through
    # floating-point accumulation. Returning the last split rather than
    # raising because losing a case from the run is a worse outcome than a
    # single case sitting one split over from where the arithmetic intended.
    return boundaries[-1][0]


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def describe_splits(splits: Mapping[str, Sequence[Mapping]]) -> dict:
    """Summarise a partition, including how strata landed.

    Exists so a caller can check the split *before* spending a run on it:
    a stratum that ended up entirely in one split makes every comparison
    against that split suspect, and that is far cheaper to notice here than
    to infer from confusing results later.
    """
    summary: dict = {"total": sum(len(v) for v in splits.values()), "splits": {}}
    for name in sorted(splits):
        cases = splits[name]
        strata = Counter(str(c.get("stratum", "")) for c in cases)
        summary["splits"][name] = {
            "count": len(cases),
            "strata": dict(sorted(strata.items())),
        }
    return summary


def parse_points(raw, delimiter: str | None = None) -> list[str]:
    """Normalise expected points into a list of strings.

    Accepts a real list, a JSON array (how a spreadsheet stores one), or
    delimited text. A cell that is neither JSON nor delimited stays a single
    point: guessing a separator would split one expectation into several and
    inflate the denominator of every score computed from it.

    Module-level and public because this is the **only** definition of how
    many expectations a value holds. A grader once carried its own version,
    and the two disagreed on six of nine inputs — including two where the
    count itself differed, which silently changed recall's denominator
    depending on which path a value took. Whether they agreed was decided by
    call order rather than by design.

    Every entry is rendered to text, so a caller receives strings whatever
    the JSON contained. Blank entries are dropped: an empty expectation
    cannot be met or missed, and counting it would make a perfect answer
    look incomplete.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [_text(item) for item in raw if _text(item)]
    text = _text(raw)
    if not text:
        return []
    if text[0] in "[{":
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(decoded, (list, tuple)):
                return [_text(item) for item in decoded if _text(item)]
            return [_text(decoded)]
    if delimiter:
        return [part for part in (_text(p) for p in text.split(delimiter))
                if part]
    return [text]


def _text(value) -> str:
    """Render a cell as trimmed text.

    Centralised because a loader that let each field decide would produce
    ``"None"`` for a missing value in one place and ``""`` in another, and
    a grader comparing against either would be right half the time.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _parse_expectations(raw) -> list[dict]:
    """Normalise an expectations cell into a list of assertion mappings.

    Non-mapping entries are dropped rather than passed on: an assertion the
    grader cannot read would raise mid-run, and the cause — one malformed
    row in the dataset — would be several layers away from where it
    surfaced.

    Decoding is attempted exactly once and only the decoded value is
    examined. Recursing on any decoded value looped forever on a scalar
    cell: ``json.loads("42")`` yields ``42``, which renders back to the
    string ``"42"``, which decodes to ``42`` again.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return []
    return _as_mappings(raw)


def _as_mappings(value) -> list[dict]:
    """Every mapping in ``value``, as plain dicts. Anything else dropped."""
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, (list, tuple)):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []
