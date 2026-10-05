"""Array subscripts (``kumosql.nested_array_subscripts``): what is normalised, what is proved, and what is declined.

The property checks run both spellings on DuckDB through KumoSQL's BigQuery translation (``bigquery_on_duckdb``),
on arrays that are empty, short, NULL (an expression can be NULL even though a stored array never is) or hold
NULL elements, and compare the rows or the failure.
"""

from __future__ import annotations

import pytest
import sqlglot

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic
from kumosql.nested_array_subscripts import normalize_array_subscripts
from kumosql.smt_equivalence import SmtStatus

SCHEMA = {"t": ["id", "a", "s", "w"]}
TYPES = {"t": {"id": "INT64", "a": "ARRAY<INT64>", "s": "ARRAY<STRUCT<k INT64, v STRING>>", "w": "ARRAY<STRING>"}}


def norm(sql: str) -> str:
    return normalize_array_subscripts(sqlglot.parse_one(sql, read="bigquery")).sql(dialect="bigquery")


def proved(left: str, right: str) -> bool:
    result = prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect="bigquery")
    return result.status is SmtStatus.PROVEN_EQUIVALENT


# ---------------------------------------------------------------- normalisation


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("SELECT a[ORDINAL(1)] FROM t", "SELECT a[OFFSET(0)] FROM t"),
        ("SELECT a[ORDINAL(3)] FROM t", "SELECT a[OFFSET(2)] FROM t"),
        ("SELECT a[SAFE_ORDINAL(1)] FROM t", "SELECT a[SAFE_OFFSET(0)] FROM t"),
        ("SELECT s[SAFE_ORDINAL(2)].k FROM t", "SELECT s[SAFE_OFFSET(1)].k FROM t"),
        ("SELECT a[OFFSET(0)] FROM t", "SELECT a[OFFSET(0)] FROM t"),
        ("SELECT a[SAFE_OFFSET(2)] FROM t", "SELECT a[SAFE_OFFSET(2)] FROM t"),
    ],
)
def test_ordinal_is_offset_shifted_by_one(source, expected):
    assert norm(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "SELECT a[SAFE_ORDINAL(0)] FROM t",  # out of range for every array
        "SELECT a[ORDINAL(0)] FROM t",
        "SELECT a[SAFE_OFFSET(-1)] FROM t",
        "SELECT a[ORDINAL(id)] FROM t",
        "SELECT a[ORDINAL(id + 1)] FROM t",
        "SELECT a[ORDINAL(1.0)] FROM t",
        "SELECT a[ORDINAL(1 + 1)] FROM t",
        "SELECT a[1] FROM t",  # a bare subscript is not read as OFFSET
    ],
)
def test_unsafe_or_computed_indices_are_left_alone(source):
    assert norm(source) == sqlglot.parse_one(source, read="bigquery").sql(dialect="bigquery")


def test_other_dialects_are_untouched():
    tree = sqlglot.parse_one("SELECT a[SAFE_ORDINAL(1)] FROM t", read="bigquery")
    assert normalize_array_subscripts(tree, "mysql").sql(dialect="bigquery") == tree.sql(dialect="bigquery")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("SELECT (SELECT e FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = 0) FROM t", "SELECT a[SAFE_OFFSET(0)] FROM t"),
        ("SELECT (SELECT e FROM UNNEST(a) AS e WITH OFFSET AS o WHERE 2 = o) AS x FROM t", "SELECT a[SAFE_OFFSET(2)] AS x FROM t"),
        ("SELECT (SELECT x.k FROM UNNEST(s) AS x WITH OFFSET AS i WHERE i = 1) FROM t", "SELECT s[SAFE_OFFSET(1)].k FROM t"),
        ("SELECT (SELECT e AS first FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = 0) + 1 FROM t", "SELECT a[SAFE_OFFSET(0)] + 1 FROM t"),
    ],
)
def test_offset_lookup_subquery_is_the_safe_subscript(source, expected):
    assert norm(source) == norm(expected)
    assert norm(source) == sqlglot.parse_one(expected, read="bigquery").sql(dialect="bigquery")


@pytest.mark.parametrize(
    "source",
    [
        # the projection must keep a missing element NULL
        "SELECT (SELECT COALESCE(e, 0) FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = 0) FROM t",
        "SELECT (SELECT e + 1 FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = 0) FROM t",
        # other filters, other shapes
        "SELECT (SELECT e FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o < 1) FROM t",
        "SELECT (SELECT e FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = id) FROM t",
        "SELECT (SELECT e FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = -1) FROM t",
        "SELECT (SELECT e FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = 0 AND e > 1) FROM t",
        "SELECT (SELECT e FROM UNNEST(a) AS e WITH OFFSET AS o) FROM t",
        "SELECT (SELECT e FROM UNNEST(a) AS e WHERE e = 0) FROM t",
        "SELECT (SELECT MIN(e) FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = 0) FROM t",
        "SELECT (SELECT e FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = 0 ORDER BY e LIMIT 1) FROM t",
        # not a scalar value: a membership test, a derived table
        "SELECT id FROM t WHERE 1 IN (SELECT e FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = 0)",
        "SELECT id FROM t WHERE 1 = ANY (SELECT e FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = 0)",
        "SELECT x.e FROM t, (SELECT e FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = 0) AS x",
    ],
)
def test_other_subqueries_are_left_alone(source):
    assert "SAFE_OFFSET" not in norm(source)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("SELECT [10, 20, 30][OFFSET(1)] AS v", "SELECT 20 AS v"),
        ("SELECT [10, 20, 30][ORDINAL(1)] AS v", "SELECT 10 AS v"),
        ("SELECT [10, 20, 30][SAFE_OFFSET(2)] AS v", "SELECT 30 AS v"),
        ("SELECT ['a', 'b'][OFFSET(1)] AS v", "SELECT 'b' AS v"),
        ("SELECT [1, NULL, 3][OFFSET(2)] AS v", "SELECT 3 AS v"),
        ("SELECT [-1, -2][OFFSET(1)] AS v", "SELECT -2 AS v"),
        ("SELECT [TRUE, FALSE][OFFSET(0)] AS v", "SELECT TRUE AS v"),
    ],
)
def test_literal_array_subscript_is_the_element(source, expected):
    assert norm(source) == norm(expected)


@pytest.mark.parametrize(
    "source",
    [
        "SELECT [10, 20][OFFSET(2)] AS v",  # an error in BigQuery
        "SELECT [10, 20][SAFE_OFFSET(2)] AS v",  # a typed NULL
        "SELECT [10, 20][OFFSET(-1)] AS v",
        "SELECT [1, NULL][OFFSET(1)] AS v",  # NULL needs its type
        "SELECT [1, 2.5][OFFSET(0)] AS v",  # BigQuery reads the 1 as 1.0
        "SELECT [1, 'a'][OFFSET(0)] AS v",
        "SELECT [10, 20][OFFSET(CAST(1 AS INT64))] AS v",
        "SELECT [10 / 0, 20][OFFSET(1)] AS v",  # another element could fail
        "SELECT ARRAY<FLOAT64>[1, 2][OFFSET(0)] AS v",  # typed: the 1 is a float
    ],
)
def test_literal_array_subscript_declines(source):
    assert norm(source) == sqlglot.parse_one(source, read="bigquery").sql(dialect="bigquery")


def test_normalize_registers_the_rule():
    assert "SAFE_OFFSET(0)" in normalize("SELECT a[SAFE_ORDINAL(1)] FROM t", schema=SCHEMA)


# ---------------------------------------------------------------- proofs


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("SELECT id, a[SAFE_OFFSET(0)] AS x FROM t", "SELECT id, a[SAFE_ORDINAL(1)] AS x FROM t"),
        ("SELECT id, a[OFFSET(1)] AS x FROM t", "SELECT id, a[ORDINAL(2)] AS x FROM t"),
        ("SELECT id, s[SAFE_OFFSET(0)].v AS x FROM t", "SELECT id, s[SAFE_ORDINAL(1)].v AS x FROM t"),
        ("SELECT id, a[SAFE_OFFSET(0)] AS x FROM t", "SELECT id, (SELECT e FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = 0) AS x FROM t"),
        ("SELECT id, s[SAFE_OFFSET(0)].v AS x FROM t", "SELECT id, (SELECT e.v FROM UNNEST(s) AS e WITH OFFSET AS o WHERE o = 0) AS x FROM t"),
        ("SELECT [10, 20, 30][OFFSET(1)] AS v", "SELECT 20 AS v"),
        ("SELECT id, a[SAFE_OFFSET(id)] AS x FROM t", "SELECT id, a[SAFE_OFFSET(id)] AS x FROM t"),  # the same text is the same
    ],
)
def test_equal_spellings_are_proved(left, right):
    assert proved(left, right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # offset 1 is the second element, ordinal 1 the first
        ("SELECT id, a[SAFE_OFFSET(1)] AS x FROM t", "SELECT id, a[SAFE_ORDINAL(1)] AS x FROM t"),
        ("SELECT [10, 20, 30][OFFSET(1)] AS v", "SELECT [10, 20, 30][ORDINAL(1)] AS v"),
        # an error on an empty array against NULL: not the same behaviour, and a stored array may be empty
        ("SELECT id, a[OFFSET(0)] AS x FROM t", "SELECT id, a[SAFE_OFFSET(0)] AS x FROM t"),
        ("SELECT id, a[ORDINAL(1)] AS x FROM t", "SELECT id, a[SAFE_ORDINAL(1)] AS x FROM t"),
        # the cross join drops the row of an empty array; the subscript keeps it with NULL
        (
            "SELECT t.id, a[SAFE_OFFSET(0)] AS x FROM t",
            "SELECT t.id, e AS x FROM t, UNNEST(a) AS e WITH OFFSET AS o WHERE o = 0",
        ),
        # a projection that does not keep the missing element NULL
        (
            "SELECT id, a[SAFE_OFFSET(0)] AS x FROM t",
            "SELECT id, (SELECT COALESCE(e, 0) FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = 0) AS x FROM t",
        ),
        # a computed or negative index is not shifted
        ("SELECT id, a[SAFE_OFFSET(id)] AS x FROM t", "SELECT id, a[SAFE_ORDINAL(id + 1)] AS x FROM t"),
        ("SELECT id, a[SAFE_OFFSET(-1)] AS x FROM t", "SELECT id, a[SAFE_ORDINAL(0)] AS x FROM t"),
        # first against last
        ("SELECT id, a[SAFE_OFFSET(0)] AS x FROM t", "SELECT id, ARRAY_REVERSE(a)[SAFE_OFFSET(0)] AS x FROM t"),
        # different fields
        ("SELECT id, s[SAFE_OFFSET(0)].k AS x FROM t", "SELECT id, s[SAFE_OFFSET(0)].v AS x FROM t"),
    ],
)
def test_traps_are_never_proved(left, right):
    assert not proved(left, right)


# ---------------------------------------------------------------- property check on DuckDB

duckdb = pytest.importorskip("duckdb")

ARRAYS = [[], [5], [5, 6], [5, 6, 7], [None], [None, 6], [5, None, 7], None]
STRUCTS = [[], [{"k": 1, "v": "x"}], [{"k": 1, "v": "x"}, {"k": 2, "v": None}], [None, {"k": 3, "v": "z"}], None]


def outcome(connection, sql: str, columns: dict[str, str]):
    from kumosql.bigquery_on_duckdb import bigquery_rows, faithful, is_bigquery_failure

    text = faithful(sqlglot.parse_one(sql, read="bigquery"), columns).sql(dialect="duckdb")
    try:
        return ("rows", sorted(bigquery_rows(connection.execute(text).fetchall()), key=repr))
    except Exception as error:  # noqa: BLE001 - only a BigQuery failure counts as a failure outcome
        if is_bigquery_failure(error):
            return ("fails",)
        raise


@pytest.fixture
def connection():
    from kumosql.bigquery_on_duckdb import configure

    db = duckdb.connect(":memory:")
    configure(db)
    db.execute("CREATE TABLE t (id BIGINT, a BIGINT[], s STRUCT(k BIGINT, v VARCHAR)[])")
    for number, (values, structs) in enumerate(zip(ARRAYS, STRUCTS * 2, strict=False)):
        db.execute("INSERT INTO t VALUES (?, ?, ?)", [number, values, structs])
    yield db
    db.close()


COLUMNS = {"id": "INT64", "a": "ARRAY<INT64>", "s": "ARRAY<STRUCT<k INT64, v STRING>>"}


@pytest.mark.parametrize("k", [0, 1, 2, 3, 6])
def test_ordinal_spellings_agree_on_every_array(connection, k):
    for safe in ("", "SAFE_"):
        left = f"SELECT id, a[{safe}OFFSET({k})] AS x FROM t"
        right = f"SELECT id, a[{safe}ORDINAL({k + 1})] AS x FROM t"
        assert outcome(connection, left, COLUMNS) == outcome(connection, right, COLUMNS)
        assert outcome(connection, normalize(left, schema=SCHEMA), COLUMNS) == outcome(connection, left, COLUMNS)
        assert outcome(connection, normalize(right, schema=SCHEMA), COLUMNS) == outcome(connection, right, COLUMNS)


@pytest.mark.parametrize("k", [0, 1, 2, 3, 6])
def test_offset_lookup_subquery_agrees_with_the_safe_subscript(connection, k):
    subscript = f"SELECT id, a[SAFE_OFFSET({k})] AS x FROM t"
    lookup = f"SELECT id, (SELECT e FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = {k}) AS x FROM t"
    assert outcome(connection, subscript, COLUMNS) == outcome(connection, lookup, COLUMNS)
    assert outcome(connection, normalize(lookup, schema=SCHEMA), COLUMNS) == outcome(connection, lookup, COLUMNS)
    structs = f"SELECT id, s[SAFE_OFFSET({k})].v AS x FROM t"
    struct_lookup = f"SELECT id, (SELECT e.v FROM UNNEST(s) AS e WITH OFFSET AS o WHERE o = {k}) AS x FROM t"
    assert outcome(connection, structs, COLUMNS) == outcome(connection, struct_lookup, COLUMNS)


def test_the_traps_really_differ_on_duckdb(connection):
    differing = [
        ("SELECT id, a[OFFSET(0)] AS x FROM t", "SELECT id, a[SAFE_OFFSET(0)] AS x FROM t"),
        ("SELECT id, a[SAFE_OFFSET(1)] AS x FROM t", "SELECT id, a[SAFE_ORDINAL(1)] AS x FROM t"),
        (
            "SELECT t.id, a[SAFE_OFFSET(0)] AS x FROM t",
            "SELECT t.id, e AS x FROM t, UNNEST(a) AS e WITH OFFSET AS o WHERE o = 0",
        ),
        (
            "SELECT id, a[SAFE_OFFSET(0)] AS x FROM t",
            "SELECT id, (SELECT COALESCE(e, 0) FROM UNNEST(a) AS e WITH OFFSET AS o WHERE o = 0) AS x FROM t",
        ),
    ]
    for left, right in differing:
        assert outcome(connection, left, COLUMNS) != outcome(connection, right, COLUMNS), (left, right)


@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_literal_array_subscript_agrees_with_its_element(connection, k):
    for source in (f"SELECT [10, 20, 30][OFFSET({k})] AS v", f"SELECT [10, 20, 30][SAFE_ORDINAL({k + 1})] AS v", f"SELECT ['a', NULL, 'c'][OFFSET({k})] AS v"):
        assert outcome(connection, normalize(source), {}) == outcome(connection, source, {})
