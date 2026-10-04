"""The authored SQL fixture gate: every sample, scored against its labelled expectation.

Runs in the default suite:

    python -m pytest tests/test_workbook_fixture.py -s    # -s prints the outcome table

``tests/fixtures/sql_subquery_samples.json`` holds 32 ``{id, sql_text}`` samples and
``sql_subquery_samples.expected.json`` labels each one: whether it parses strictly, whether it is a
valid input (DuckDB binds and runs it on a declared schema), whether the lifter should change it,
how many subqueries it lifts and what the verified API (``apply_rule``) says about the rewrite. The
labels pin the fixture by sha256 and case count, so every row stays in the denominator, and a row
is credited only when it parses strictly, is shown valid, changes, leaves no relational subquery and is
proven. Validity is shown by running the input on a declared schema; a fixture with no schema earns
no credit, and a label that says valid cannot stand in for the run.

Set ``KUMOSQL_TEST_FIXTURE`` to score another CSV or JSON fixture (it must exist, have unique
non-empty ids and non-empty ``sql_text``). Its labels come from ``KUMOSQL_TEST_FIXTURE_EXPECTED``
or a sibling ``<name>.expected.json``; without labels every row must still parse strictly and
leave no relational subquery, and the other outcomes are only reported.
"""

from __future__ import annotations

from collections import Counter
import csv
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping

import pytest

from kumosql import apply_rule, lift_subqueries
from kumosql.engine import FATAL_DIAGNOSTIC_CODES
from kumosql.sqlx import looks_like_sqlx, split_sqlx_sections


FIXTURES = Path(__file__).parent / "fixtures"
DEFAULT_FIXTURE = FIXTURES / "sql_subquery_samples.json"
DEFAULT_EXPECTATIONS = FIXTURES / "sql_subquery_samples.expected.json"
VERIFICATION_STATUSES = {"proven", "unchanged", "unproven", "planner_checked", "failed"}
PARSE_FAILURE_CODES = {"parse_error", "sqlx_parse_error", "output_parse_error"}


class FixtureError(AssertionError):
    """The requested fixture or its labels cannot be scored as given."""


# --- loading and validation -------------------------------------------------------------------


def fixture_sha256(path: Path) -> str:
    """sha256 of the file's bytes with CRLF read as LF, so a Windows checkout hashes the same."""

    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_rows(path: Path) -> object:
    if not path.exists():
        raise FixtureError(f"requested fixture not found: {path}")
    if path.suffix.lower() == ".json":
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise FixtureError(f"{path}: not valid JSON: {exc}") from exc
    csv.field_size_limit(100_000_000)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def row_id(row: Mapping) -> object:
    return row.get("id", row.get("record_id"))


def validate_rows(rows: object, source: object = "fixture") -> list[str]:
    """Problems that stop a fixture from being scored: it must be a non-empty list of rows, each
    with a unique non-empty id (``id``, or ``record_id`` in a CSV) and non-empty ``sql_text``."""

    if not isinstance(rows, list):
        return [f"{source}: expected a list of rows, got {type(rows).__name__}"]
    if not rows:
        return [f"{source}: has no rows"]
    problems: list[str] = []
    seen: Counter[str] = Counter()
    for position, row in enumerate(rows, start=1):
        where = f"{source} row {position}"
        if not isinstance(row, dict):
            problems.append(f"{where}: expected an object, got {type(row).__name__}")
            continue
        identifier = row_id(row)
        if not isinstance(identifier, str) or not identifier.strip():
            problems.append(f"{where}: missing or empty id")
        else:
            seen[identifier] += 1
            where = f"{source} id={identifier}"
        sql = row.get("sql_text")
        if not isinstance(sql, str):
            problems.append(f"{where}: sql_text is missing or not a string")
        elif not sql.strip():
            problems.append(f"{where}: sql_text is empty")
    problems.extend(
        f"{source}: duplicate id {identifier!r} ({count} rows)" for identifier, count in seen.items() if count > 1
    )
    return problems


_LABEL_TYPES = (
    ("strict_parse", bool),
    ("valid_input", bool),
    ("expect_change", bool),
    ("lifted", int),
    ("verification", str),
)


def load_expectations(path: Path, fixture_path: Path, rows: list[dict]) -> dict:
    """Read a labels manifest and check it against the fixture it describes."""

    if not path.exists():
        raise FixtureError(f"expectations not found: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise FixtureError(f"{path}: not valid JSON: {exc}") from exc
    problems: list[str] = []
    if manifest.get("sha256") != fixture_sha256(fixture_path):
        problems.append(f"{path.name}: sha256 does not match {fixture_path.name}; relabel the changed fixture")
    if manifest.get("case_count") != len(rows):
        problems.append(f"{path.name}: declares {manifest.get('case_count')} cases, {fixture_path.name} has {len(rows)}")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise FixtureError("\n".join([*problems, f"{path.name}: cases must be a non-empty list"]))
    by_id: dict[str, dict] = {}
    for case in cases:
        identifier = case.get("id") if isinstance(case, dict) else None
        if not isinstance(identifier, str) or not identifier:
            problems.append(f"{path.name}: a case has no id")
            continue
        if identifier in by_id:
            problems.append(f"{path.name}: duplicate case {identifier}")
        by_id[identifier] = case
        for key, kind in _LABEL_TYPES:
            value = case.get(key)
            if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
                problems.append(f"{path.name} {identifier}: {key} must be a {kind.__name__}")
        if case.get("verification") not in VERIFICATION_STATUSES:
            problems.append(f"{path.name} {identifier}: verification must be one of {sorted(VERIFICATION_STATUSES)}")
        older = case.get("verification_before_sqlglot")
        if older is not None and not (
            isinstance(older, dict)
            and _version_tuple(older.get("version")) is not None
            and older.get("status") in VERIFICATION_STATUSES
            and older.get("reason")
        ):
            problems.append(f"{path.name} {identifier}: verification_before_sqlglot needs a version, a status and a reason")
        if case.get("valid_input") is False and not case.get("invalid_reason"):
            problems.append(f"{path.name} {identifier}: an invalid input needs an invalid_reason")
        if case.get("expect_change") is False and not case.get("unchanged_reason"):
            problems.append(f"{path.name} {identifier}: an unchanged case needs an unchanged_reason")
        remaining = case.get("remaining", 0)
        if not isinstance(remaining, int) or isinstance(remaining, bool) or remaining < 0:
            problems.append(f"{path.name} {identifier}: remaining must be a non-negative int")
        elif remaining and not case.get("remaining_reason"):
            problems.append(f"{path.name} {identifier}: a case that leaves a subquery needs a remaining_reason")
    fixture_ids = {row_id(row) for row in rows}
    if set(by_id) != fixture_ids:
        missing, extra = sorted(fixture_ids - set(by_id)), sorted(set(by_id) - fixture_ids)
        problems.append(f"{path.name}: case ids differ from the fixture (unlabelled {missing}, unknown {extra})")
    schema = manifest.get("duckdb_schema")
    if schema is not None and not (
        isinstance(schema, dict)
        and schema
        and all(
            isinstance(name, str) and name.count(".") == 2 and isinstance(columns, dict) and columns
            for name, columns in schema.items()
        )
    ):
        problems.append(f"{path.name}: duckdb_schema must map catalog.dataset.table names to non-empty column maps")
    if problems:
        raise FixtureError("\n".join(problems))
    return {"cases": by_id, "duckdb_schema": schema}


def _version_tuple(version: object) -> tuple[int, ...] | None:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version) if isinstance(version, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


def expected_verification(case: Mapping, sqlglot_version: str) -> str:
    """The labelled status, or the one labelled for sqlglot releases older than a given version."""

    older = case.get("verification_before_sqlglot")
    installed = _version_tuple(sqlglot_version)
    if older and installed is not None and installed < _version_tuple(older["version"]):
        return older["status"]
    return case["verification"]


def requested_fixture(environ: Mapping[str, str]) -> tuple[Path, Path | None]:
    """The fixture to score and its labels. An explicitly requested file that is missing is an error,
    never a skip: a mistyped path must not quietly drop the evaluation."""

    override = environ.get("KUMOSQL_TEST_FIXTURE")
    fixture = Path(override) if override else DEFAULT_FIXTURE
    if not fixture.exists():
        raise FixtureError(f"requested fixture not found: {fixture}")
    labels = environ.get("KUMOSQL_TEST_FIXTURE_EXPECTED")
    if labels:
        if not Path(labels).exists():
            raise FixtureError(f"requested expectations not found: {labels}")
        return fixture, Path(labels)
    sibling = fixture.with_name(fixture.stem + ".expected.json")
    if sibling.exists():
        return fixture, sibling
    if not override:
        raise FixtureError(f"the default fixture's expectations are missing: {sibling}")
    return fixture, None


# --- DuckDB input check -----------------------------------------------------------------------


_SQLX_INTERPOLATION = re.compile(
    r'\$\{\s*ref\(\s*"(?P<ref>[A-Za-z0-9_]+)"\s*\)\s*\}'
    r'|\$\{\s*when\(\s*incremental\(\)\s*,\s*"(?P<then>[^"]*)"\s*,\s*"(?P<otherwise>[^"]*)"\s*\)\s*\}'
)


def duckdb_source(sql: str) -> str:
    """BigQuery SQL for DuckDB. SQLX keeps its SQL sections (config and operations blocks are not
    checked), ``ref("x")`` reads ``demo.dataform.x`` and ``when(incremental(), a, b)`` is ``b``."""

    if not looks_like_sqlx(sql):
        return sql
    body = "".join(text for kind, text in split_sqlx_sections(sql) if kind == "sql")

    def replace(match: re.Match) -> str:
        return f"`demo.dataform.{match['ref']}`" if match["ref"] else match["otherwise"]

    body = _SQLX_INTERPOLATION.sub(replace, body)
    if "${" in body:
        raise ValueError("unsupported SQLX interpolation")
    return body


class DuckDBChecker:
    """Run statements on a fresh DuckDB database of empty tables from a declared schema. A binder
    error refutes an input; running cleanly does not show that BigQuery accepts it."""

    def __init__(self, schema: Mapping[str, Mapping[str, str]]):
        import sqlglot

        self._sqlglot = sqlglot
        self._setup: list[str] = []
        for catalog in sorted({name.split(".")[0] for name in schema}):
            self._setup.append(f"ATTACH ':memory:' AS \"{catalog}\"")
        for prefix in sorted({name.rsplit(".", 1)[0] for name in schema}):
            catalog, dataset = prefix.split(".")
            self._setup.append(f'CREATE SCHEMA "{catalog}"."{dataset}"')
        for name, columns in schema.items():
            ddl = f"CREATE TABLE `{name}` (" + ", ".join(f"{column} {kind}" for column, kind in columns.items()) + ")"
            self._setup.extend(sqlglot.transpile(ddl, read="bigquery", write="duckdb"))

    def error(self, sql: str) -> str | None:
        import duckdb  # a dev dependency; the gate fails rather than skips without it

        connection = duckdb.connect()
        try:
            for statement in self._setup:
                connection.execute(statement)
            for statement in self._sqlglot.transpile(duckdb_source(sql), read="bigquery", write="duckdb"):
                connection.execute(statement)
            return None
        except Exception as exc:  # noqa: BLE001 - any failure is the outcome being recorded
            first_line = str(exc).splitlines()[0] if str(exc) else ""
            return f"{type(exc).__name__}: {first_line}"
        finally:
            connection.close()


# --- scoring ----------------------------------------------------------------------------------


NOT_CHECKED = "not checked"


@dataclass(frozen=True)
class Outcome:
    id: str
    parse: str  # strict, recovered or failed
    input_error: str | None  # None when DuckDB ran it, NOT_CHECKED without a schema
    output_error: str | None
    changed: bool
    lifted: int
    remaining: int  # relational subqueries left in the output
    structural: bool  # LiftResult.success: no relational subquery left and no fatal diagnostic
    verification: str
    diagnostics: tuple[str, ...]

    @property
    def valid_input(self) -> bool | None:
        return None if self.input_error == NOT_CHECKED else self.input_error is None

    @property
    def credited(self) -> bool:
        return (
            self.parse == "strict"
            and self.valid_input is True  # not checked is not valid: credit needs a schema run
            and self.changed
            and self.structural
            and self.verification == "proven"
        )


def score_row(identifier: str, sql: str, checker: DuckDBChecker | None) -> Outcome:
    result = lift_subqueries(sql)
    codes = tuple(diagnostic.code for diagnostic in result.diagnostics)
    if result.recovered:
        parse = "recovered"
    elif any(code in PARSE_FAILURE_CODES for code in codes):
        parse = "failed"
    else:
        parse = "strict"
    changed = result.sql != sql
    if checker is None:
        input_error = output_error = NOT_CHECKED
    else:
        input_error = checker.error(sql)
        output_error = checker.error(result.sql) if changed else input_error
    return Outcome(
        id=identifier,
        parse=parse,
        input_error=input_error,
        output_error=output_error,
        changed=changed,
        lifted=result.lifted_subqueries,
        remaining=result.remaining_inline_subqueries,
        structural=result.success,
        verification=apply_rule("lift_subqueries", sql).verification.status.value,
        diagnostics=codes,
    )


def expectation_mismatches(outcome: Outcome, case: Mapping) -> list[str]:
    import sqlglot

    expected = {
        **case,
        "remaining": case.get("remaining", 0),
        "verification": expected_verification(case, sqlglot.__version__),
    }
    observed = {
        "strict_parse": outcome.parse == "strict",
        "valid_input": outcome.valid_input,
        "expect_change": outcome.changed,
        "lifted": outcome.lifted,
        "remaining": outcome.remaining,
        "verification": outcome.verification,
    }
    if outcome.valid_input is None:
        del observed["valid_input"]  # no schema to check against: the label stands unverified
    problems = [
        f"{outcome.id}: {key} expected {expected[key]!r}, observed {value!r}"
        + (f" ({outcome.input_error})" if key == "valid_input" and outcome.input_error else "")
        for key, value in observed.items()
        if value != expected[key]
    ]
    # A labelled leftover subquery reports inline_subqueries_remaining; any other fatal diagnostic fails.
    fatal = [code for code in outcome.diagnostics if code in FATAL_DIAGNOSTIC_CODES and code != "inline_subqueries_remaining"]
    if fatal:
        problems.append(f"{outcome.id}: fatal diagnostics {fatal}")
    if outcome.valid_input and outcome.changed and outcome.output_error:
        problems.append(f"{outcome.id}: the input runs in DuckDB but the lifted output does not ({outcome.output_error})")
    return problems


def unlabelled_problems(outcome: Outcome) -> list[str]:
    problems = []
    if outcome.parse != "strict":
        problems.append(f"{outcome.id}: {outcome.parse} parse is not a success {outcome.diagnostics}")
    if not outcome.structural:
        problems.append(f"{outcome.id}: a relational subquery is left or a fatal diagnostic was reported {outcome.diagnostics}")
    return problems


def summarize(outcomes: list[Outcome], declared: int | None) -> str:
    def ids(predicate) -> str:
        chosen = [outcome.id for outcome in outcomes if predicate(outcome)]
        return f"{len(chosen)}" + (f" ({', '.join(chosen)})" if chosen and len(chosen) <= 8 else "")

    statuses = sorted({outcome.verification for outcome in outcomes})
    lines = [
        f"rows {len(outcomes)}" + (f" of {declared} declared" if declared is not None else " (no labels)"),
        f"parse: strict {ids(lambda o: o.parse == 'strict')}, recovered {ids(lambda o: o.parse == 'recovered')}, "
        f"failed {ids(lambda o: o.parse == 'failed')}",
        f"input (DuckDB on the declared schema): valid {ids(lambda o: o.valid_input is True)}, "
        f"invalid {ids(lambda o: o.valid_input is False)}, not checked {ids(lambda o: o.valid_input is None)}",
        f"change: changed {ids(lambda o: o.changed)}, unchanged {ids(lambda o: not o.changed)}",
        f"structural: no relational subquery left {ids(lambda o: o.structural)}, not {ids(lambda o: not o.structural)}; "
        f"{sum(o.lifted for o in outcomes)} lifted, {sum(o.remaining for o in outcomes)} left",
        "verification (apply_rule): "
        + ", ".join(f"{status} {ids(lambda o, s=status: o.verification == s)}" for status in statuses),
        f"credited (strict, valid, changed, structural, proven): {sum(o.credited for o in outcomes)}/{len(outcomes)}",
        "",
        f"{'id':<12} {'parse':<10} {'input':<8} {'changed':<8} {'lifted':>6}  {'structural':<10} verification",
    ]
    for outcome in outcomes:
        valid = {True: "valid", False: "invalid", None: "-"}[outcome.valid_input]
        lines.append(
            f"{outcome.id:<12} {outcome.parse:<10} {valid:<8} {str(outcome.changed).lower():<8} {outcome.lifted:>6}  "
            f"{str(outcome.structural).lower():<10} {outcome.verification}"
        )
    return "\n".join(lines)


def run_gate(fixture: Path, expectations: Path | None) -> tuple[list[Outcome], list[str], str]:
    """Score every row; return the outcomes, the problems that fail the gate and a summary."""

    rows = load_rows(fixture)
    problems = validate_rows(rows, fixture.name)
    if problems:
        raise FixtureError("\n".join(problems))
    labels = load_expectations(expectations, fixture, rows) if expectations else None
    schema = labels["duckdb_schema"] if labels else None
    checker = DuckDBChecker(schema) if schema else None
    outcomes = [score_row(row_id(row), row["sql_text"], checker) for row in rows]
    for outcome in outcomes:
        if labels:
            problems.extend(expectation_mismatches(outcome, labels["cases"][outcome.id]))
        else:
            problems.extend(unlabelled_problems(outcome))
    return outcomes, problems, summarize(outcomes, len(labels["cases"]) if labels else None)


def test_every_fixture_query_lifts_all_relational_subqueries():
    fixture, expectations = requested_fixture(os.environ)
    outcomes, problems, summary = run_gate(fixture, expectations)
    print(summary)

    assert not problems, "\n".join(problems[:50]) + "\n\n" + summary
    if fixture.resolve() == DEFAULT_FIXTURE.resolve():
        # The labels already pin every row; these keep the headline numbers in view.
        # 26 credited on current sqlglot; 25 before sqlglot 28, where q20 is unproven. q18's subquery sits in an
        # EXISTS with an unqualified column and stays in place (docs/rewrite-rules.md#subquery-lifting). q28 holds a
        # dynamic ${when(...)} expression and stays unproven until the SQLX is compiled (docs/proof-safeguards.md).
        assert len(outcomes) == 32
        assert sum(outcome.credited for outcome in outcomes) >= 25
        assert [o.id for o in outcomes if o.valid_input is False] == ["q09", "q21"]
        assert [o.id for o in outcomes if not o.changed] == ["q16", "q17", "q18"]
        assert [o.id for o in outcomes if not o.structural] == ["q17", "q18"]
        assert {o.id for o in outcomes if o.verification != "proven"} <= {"q09", "q16", "q17", "q18", "q20", "q21", "q28"}


# --- the gate's own guards --------------------------------------------------------------------


@pytest.mark.parametrize(
    "rows, message",
    [
        ([], "has no rows"),
        ({"id": "1", "sql_text": "SELECT 1"}, "expected a list"),
        ([{"id": "1"}], "sql_text is missing"),
        ([{"id": "1", "sql_text": ""}], "sql_text is empty"),
        ([{"id": "1", "sql_text": "  \n"}], "sql_text is empty"),
        ([{"id": "1", "sql_text": 7}], "not a string"),
        ([{"sql_text": "SELECT 1"}], "missing or empty id"),
        ([{"id": "", "sql_text": "SELECT 1"}], "missing or empty id"),
        ([{"id": "1", "sql_text": "SELECT 1"}, {"id": "1", "sql_text": "SELECT 2"}], "duplicate id"),
    ],
)
def test_malformed_override_fixture_is_rejected(tmp_path, monkeypatch, rows, message):
    path = tmp_path / "override.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setenv("KUMOSQL_TEST_FIXTURE", str(path))
    monkeypatch.delenv("KUMOSQL_TEST_FIXTURE_EXPECTED", raising=False)

    fixture, expectations = requested_fixture(os.environ)
    with pytest.raises(FixtureError, match=message):
        run_gate(fixture, expectations)


def test_missing_requested_fixture_fails_instead_of_skipping(tmp_path):
    with pytest.raises(FixtureError, match="requested fixture not found"):
        requested_fixture({"KUMOSQL_TEST_FIXTURE": str(tmp_path / "typo.json")})
    with pytest.raises(FixtureError, match="requested expectations not found"):
        requested_fixture({"KUMOSQL_TEST_FIXTURE_EXPECTED": str(tmp_path / "typo.expected.json")})


def test_unlabelled_override_fails_on_recovered_parses(tmp_path):
    path = tmp_path / "override.json"
    path.write_text(json.dumps([
        {"id": "ok", "sql_text": "SELECT * FROM (SELECT 1 AS a) AS q"},
        {"id": "truncated", "sql_text": "SELECT 1 FROM t WHERE 1 ="},
        {"id": "trailing", "sql_text": "SELECT * FROM (SELECT 1 AS a) AS q garbage extra"},
    ]), encoding="utf-8")

    outcomes, problems, summary = run_gate(path, None)

    assert [o.parse for o in outcomes] == ["strict", "recovered", "recovered"]
    assert [p.split(":")[0] for p in problems] == ["truncated", "trailing"]
    assert "recovered 2 (truncated, trailing)" in summary
    assert not any(o.credited for o in outcomes[1:])


def test_labels_must_match_the_fixture_they_describe(tmp_path):
    fixture = tmp_path / "f.json"
    fixture.write_text(json.dumps([{"id": "a", "sql_text": "SELECT * FROM (SELECT 1 AS x) AS q"}]), encoding="utf-8")
    labels = tmp_path / "f.expected.json"
    sha = fixture_sha256(fixture)
    case = {"id": "a", "strict_parse": True, "valid_input": True, "expect_change": True, "lifted": 1, "verification": "proven"}

    def write(**manifest):
        labels.write_text(json.dumps(manifest), encoding="utf-8")

    write(sha256="0" * 64, case_count=1, cases=[case])
    with pytest.raises(FixtureError, match="sha256 does not match"):
        run_gate(fixture, labels)
    write(sha256=sha, case_count=2, cases=[case])
    with pytest.raises(FixtureError, match="declares 2 cases"):
        run_gate(fixture, labels)
    write(sha256=sha, case_count=1, cases=[{**case, "id": "b"}])
    with pytest.raises(FixtureError, match="case ids differ"):
        run_gate(fixture, labels)
    write(sha256=sha, case_count=1, cases=[{**case, "valid_input": False}])
    with pytest.raises(FixtureError, match="needs an invalid_reason"):
        run_gate(fixture, labels)
    write(sha256=sha, case_count=1, cases=[{**case, "expect_change": False}])
    with pytest.raises(FixtureError, match="needs an unchanged_reason"):
        run_gate(fixture, labels)
    write(sha256=sha, case_count=1, cases=[{**case, "remaining": 1}])
    with pytest.raises(FixtureError, match="needs a remaining_reason"):
        run_gate(fixture, labels)

    # An override picks up its sibling labels, and a wrong label fails the gate instead of passing it.
    write(sha256=sha, case_count=1, cases=[{**case, "lifted": 2}])
    assert requested_fixture({"KUMOSQL_TEST_FIXTURE": str(fixture)}) == (fixture, labels)
    _, problems, _ = run_gate(fixture, labels)
    assert problems == ["a: lifted expected 2, observed 1"]


def test_default_labels_explain_every_case_that_earns_no_credit():
    manifest = json.loads(DEFAULT_EXPECTATIONS.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in manifest["cases"]}

    assert manifest["case_count"] == len(cases) == len(manifest["cases"]) == 32
    assert manifest["sha256"] == fixture_sha256(DEFAULT_FIXTURE)
    assert "MERGE USING subquery is not lifted" in cases["q16"]["unchanged_reason"]
    assert {i for i, c in cases.items() if not c["valid_input"]} == {"q09", "q21"}
    assert all(c.get("verification_reason") for c in cases.values() if c["verification"] not in ("proven", "unchanged"))
    assert cases["q17"]["remaining"] == 1 and "UPDATE" in cases["q17"]["remaining_reason"]
    older = {"verification": "proven", "verification_before_sqlglot": {"version": "28.0.0", "status": "unproven", "reason": "r"}}
    assert expected_verification(older, "27.0.0") == "unproven"
    assert expected_verification(older, "30.21.0") == "proven"
    assert "verification_before_sqlglot" not in cases["q20"]  # every supported sqlglot proves it; see window_canonical


INVALID_INPUTS = {
    "missing table": "SELECT q.x FROM (SELECT x FROM demo.sales.nope) AS q",
    "inner column": "SELECT q.x FROM (SELECT missing AS x FROM demo.sales.regions) AS q",
    "outer column": "SELECT q.missing FROM (SELECT 1 AS x) AS q",
    "ambiguous join column": (
        "SELECT region FROM (SELECT region FROM demo.sales.regions) AS a "
        "JOIN (SELECT region FROM demo.sales.regions) AS b ON TRUE"
    ),
}


def test_unchecked_validity_earns_no_credit(tmp_path):
    # Without a schema DuckDB cannot say whether an input runs, so "not checked" must not count
    # as valid: each of these cannot run and used to be credited 1/1.
    fixture = tmp_path / "override.json"
    fixture.write_text(
        json.dumps([{"id": name, "sql_text": sql} for name, sql in INVALID_INPUTS.items()]), encoding="utf-8"
    )

    outcomes, _, summary = run_gate(fixture, None)

    assert [o.valid_input for o in outcomes] == [None] * len(INVALID_INPUTS)
    assert not any(o.credited for o in outcomes)
    assert f"credited (strict, valid, changed, structural, proven): 0/{len(INVALID_INPUTS)}" in summary


def test_explicit_invalid_label_without_a_schema_earns_no_credit(tmp_path):
    fixture = tmp_path / "f.json"
    fixture.write_text(json.dumps([{"id": "a", "sql_text": INVALID_INPUTS["outer column"]}]), encoding="utf-8")
    labels = tmp_path / "f.expected.json"
    case = {
        "id": "a", "strict_parse": True, "valid_input": False, "invalid_reason": "q has no column missing",
        "expect_change": True, "lifted": 1, "verification": "proven",
    }
    labels.write_text(
        json.dumps({"sha256": fixture_sha256(fixture), "case_count": 1, "cases": [case]}), encoding="utf-8"
    )

    outcomes, _, _ = run_gate(fixture, labels)

    assert outcomes[0].valid_input is None
    assert not outcomes[0].credited


def test_declared_schema_decides_validity_credit(tmp_path):
    fixture = tmp_path / "f.json"
    rows = [{"id": "valid", "sql_text": "SELECT q.region FROM (SELECT region FROM demo.sales.regions) AS q"}]
    rows += [{"id": name, "sql_text": sql} for name, sql in INVALID_INPUTS.items()]
    fixture.write_text(json.dumps(rows), encoding="utf-8")
    labels = tmp_path / "f.expected.json"
    cases = [
        {
            "id": row["id"], "strict_parse": True, "valid_input": row["id"] == "valid",
            **({} if row["id"] == "valid" else {"invalid_reason": "cannot run"}),
            "expect_change": True, "lifted": 2 if row["id"] == "ambiguous join column" else 1,
            # A column the derived table does not produce is a refused input, not a proven rewrite.
            "verification": "unproven" if row["id"] == "outer column" else "proven",
            **({"verification_reason": "refused: BigQuery would reject the input"} if row["id"] == "outer column" else {}),
        }
        for row in rows
    ]
    schema = {"demo.sales.regions": {"region": "STRING"}}
    labels.write_text(
        json.dumps({"sha256": fixture_sha256(fixture), "case_count": len(rows), "duckdb_schema": schema, "cases": cases}),
        encoding="utf-8",
    )

    outcomes, problems, _ = run_gate(fixture, labels)

    assert [o.id for o in outcomes if o.credited] == ["valid"]
    assert [o.id for o in outcomes if o.valid_input is False] == list(INVALID_INPUTS)
    assert problems == []
