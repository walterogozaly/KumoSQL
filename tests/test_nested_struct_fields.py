"""Folding the field of a STRUCT the query builds (``kumosql.nested_struct_fields``).

Positive cases fold and prove; every soundness condition has a case that must be left alone; the property checks
run the query and its normal form on DuckDB, through the BigQuery translation, over rows that include NULL structs,
NULL fields and repeated keys.
"""

from __future__ import annotations

import pytest

sqlglot = pytest.importorskip("sqlglot")
duckdb = pytest.importorskip("duckdb")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic  # noqa: E402
from kumosql.bigquery_on_duckdb import Unfaithful, bigquery_rows, configure, faithful  # noqa: E402
from kumosql.smt_equivalence import SmtStatus  # noqa: E402

TYPES = {
    "t": {
        "id": "INT64",
        "a": "INT64",
        "b": "INT64",
        "s": "STRUCT<f INT64, g INT64>",
        "tags": "ARRAY<STRING>",
    },
    "u": {"id": "INT64", "a": "INT64", "b": "INT64", "k": "INT64"},
}
SCHEMA = {table: list(columns) for table, columns in TYPES.items()}


def norm(sql: str) -> str:
    return normalize(sql, schema=SCHEMA, types=TYPES)


def proves(left: str, right: str) -> bool:
    result = prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect="bigquery")
    return result.status is SmtStatus.PROVEN_EQUIVALENT


# --- folds -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql, plain",
    [
        ("SELECT STRUCT(t.a AS x, t.b AS y).y AS v FROM t", "SELECT t.b AS v FROM t"),
        ("SELECT STRUCT(t.a AS x, t.b AS Y).y AS v FROM t", "SELECT t.b AS v FROM t"),  # field names ignore case
        ("SELECT STRUCT(t.a AS X, t.b AS y).x AS v FROM t", "SELECT t.a AS v FROM t"),
        ("SELECT STRUCT(STRUCT(t.a AS p) AS q).q.p AS v FROM t", "SELECT t.a AS v FROM t"),
        ("SELECT (STRUCT(t.a AS x, t.b AS y)).x AS v FROM t", "SELECT t.a AS v FROM t"),
        ("SELECT STRUCT(t.a + 1 AS x, t.b AS y).x * 2 AS v FROM t", "SELECT (t.a + 1) * 2 AS v FROM t"),
        ("SELECT STRUCT(t.s.f AS x, t.s.g AS y).x AS v FROM t", "SELECT t.s.f AS v FROM t"),  # a stored struct, field by field
        ("SELECT (SELECT AS STRUCT t.a AS x, t.b AS y).y AS v FROM t", "SELECT t.b AS v FROM t"),
        ("SELECT (SELECT AS VALUE STRUCT(t.a AS x, t.b AS y)).y AS v FROM t", "SELECT t.b AS v FROM t"),
        ("SELECT (SELECT AS STRUCT u.a AS x, u.b AS y FROM u WHERE u.k = t.id).y FROM t", "SELECT (SELECT u.b FROM u WHERE u.k = t.id) FROM t"),
        (
            "SELECT (SELECT AS STRUCT u.a AS x FROM u WHERE u.k = t.id ORDER BY x LIMIT 1).x FROM t",
            "SELECT (SELECT u.a AS x FROM u WHERE u.k = t.id ORDER BY x LIMIT 1) FROM t",
        ),  # the alias stays when a clause reads it
        ("SELECT (t.s).f AS v FROM t", "SELECT t.s.f AS v FROM t"),
        ("SELECT (s).f AS v FROM t", "SELECT s.f AS v FROM t"),
    ],
)
def test_field_reads_fold_to_the_plain_query(sql, plain):
    assert norm(sql) == norm(plain)
    assert "STRUCT" not in norm(sql).upper()


def test_derived_struct_column_is_read_through_a_new_column():
    sql = "SELECT d.s.f AS v FROM (SELECT t.id AS id, STRUCT(t.a AS f, t.b AS g) AS s FROM t) AS d"
    assert "STRUCT" not in norm(sql).upper()
    assert proves(sql, "SELECT t.a AS v FROM t")


def test_grouped_derived_struct_column_folds_too():
    sql = "SELECT d.s.n AS n FROM (SELECT STRUCT(t.a AS g, COUNT(*) AS n) AS s FROM t GROUP BY t.a) AS d"
    assert "STRUCT" not in norm(sql).upper()
    assert proves(sql, "SELECT COUNT(*) AS n FROM t GROUP BY t.a")


def test_value_table_derived_source_is_an_ordinary_derived_table():
    sql = "SELECT d.y AS v FROM (SELECT AS STRUCT t.a AS x, t.b AS y FROM t) AS d"
    assert "STRUCT" not in norm(sql).upper()
    assert proves(sql, "SELECT t.b AS v FROM t")


def test_pairs_prove_through_the_fold():
    assert proves("SELECT STRUCT(t.id AS i, t.b AS c).c AS c FROM t", "SELECT t.b AS c FROM t")
    assert proves("SELECT (SELECT AS STRUCT t.id AS i, t.b AS c).i AS c FROM t", "SELECT t.id AS c FROM t")
    assert proves("SELECT (s).f AS v FROM t", "SELECT t.s.f AS v FROM t")


def test_the_other_field_is_a_different_query():
    assert not proves("SELECT STRUCT(t.id AS i, t.b AS c).i AS c FROM t", "SELECT t.b AS c FROM t")
    assert not proves("SELECT (SELECT AS STRUCT t.id AS i, t.b AS c).i AS c FROM t", "SELECT t.b AS c FROM t")


# --- declines ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT STRUCT(t.a, t.b AS y).y AS v FROM t",  # an unnamed field
        "SELECT STRUCT(t.a AS x, t.b AS X).x AS v FROM t",  # the same name twice (names ignore case)
        "SELECT STRUCT(t.a AS x, t.b AS y).z AS v FROM t",  # no such field
        "SELECT STRUCT<x INT64, y INT64>(t.a, t.b).y AS v FROM t",  # a typed constructor
        "SELECT STRUCT(t.a AS x, COUNT(*) AS n).x AS v FROM t",  # a dropped aggregate sets the row count
        "SELECT STRUCT(t.a AS x, SUM(t.b) OVER () AS n).x AS v FROM t",
        "SELECT STRUCT(t.a AS x, (SELECT MAX(u.b) FROM u) AS n).x AS v FROM t",
        "SELECT (SELECT AS STRUCT u.a AS x, COUNT(*) AS n FROM u).x AS v FROM t",
        "SELECT (SELECT DISTINCT AS STRUCT u.a AS x, u.b AS y FROM u WHERE u.k = t.id).x AS v FROM t",
        "SELECT (SELECT AS STRUCT u.a AS x, u.b AS y FROM u WHERE u.k = t.id ORDER BY y LIMIT 1).x AS v FROM t",
        "SELECT (SELECT AS STRUCT u.a AS x, u.b AS y FROM u WHERE u.k = t.id ORDER BY 2 LIMIT 1).x AS v FROM t",
        "SELECT (SELECT AS STRUCT u.a, u.b AS y FROM u WHERE u.k = t.id).y AS v FROM t",
        "SELECT (SELECT AS STRUCT u.a AS x, u.b AS y FROM u WHERE u.k = t.id).z AS v FROM t",
        "SELECT (s).f AS v FROM t AS s",  # ``s`` is a table alias here: ``(s)`` is the whole row
    ],
)
def test_conditions_that_do_not_hold_leave_the_struct_in_place(sql):
    from kumosql.nested_struct_fields import fold_struct_fields

    tree = sqlglot.parse_one(sql, read="bigquery")
    assert fold_struct_fields(tree.copy()).sql(dialect="bigquery") == tree.sql(dialect="bigquery")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT d.s.f AS v FROM (SELECT DISTINCT t.id AS id, STRUCT(t.a AS f) AS s FROM t) AS d",
        "SELECT d.s.f AS v FROM (SELECT t.a AS f FROM t UNION ALL SELECT t.b FROM t) AS d",
        "SELECT d.* FROM (SELECT STRUCT(t.a AS f) AS s FROM t) AS d WHERE d.s.f > 1",
        "SELECT * FROM (SELECT STRUCT(t.a AS f) AS s FROM t) AS d WHERE d.s.f > 1",
        "SELECT d.s.f FROM (SELECT STRUCT(RAND() AS f, 1 AS g) AS s FROM t) AS d",
        "SELECT d.f FROM (SELECT AS STRUCT t.a AS f FROM t) AS d WHERE d IS NOT NULL",
        "SELECT d.* FROM (SELECT AS STRUCT t.a AS f FROM t) AS d",
        "SELECT * FROM (SELECT AS STRUCT t.a AS f FROM t) AS d",
        "SELECT d.q FROM (SELECT AS STRUCT t.a AS f FROM t) AS d",
        "SELECT d.f FROM (SELECT AS STRUCT t.a FROM t) AS d",
    ],
)
def test_derived_tables_that_cannot_take_a_new_column_are_left_alone(sql):
    from kumosql.nested_struct_fields import fold_struct_fields

    tree = sqlglot.parse_one(sql, read="bigquery")
    assert fold_struct_fields(tree.copy()).sql(dialect="bigquery") == tree.sql(dialect="bigquery")


# --- NULL structs are not their fields ---------------------------------------------------------------------------


def test_struct_null_is_not_the_nulls_of_its_fields():
    assert not proves("SELECT t.id FROM t WHERE t.s IS NULL", "SELECT t.id FROM t WHERE t.s.f IS NULL AND t.s.g IS NULL")
    assert not proves("SELECT t.id FROM t WHERE t.s IS NOT NULL", "SELECT t.id FROM t WHERE t.s.f IS NOT NULL")
    assert not proves("SELECT t.id FROM t WHERE t.s IS NOT NULL", "SELECT t.id FROM t WHERE t.s.f IS NOT NULL OR t.s.g IS NOT NULL")


def test_a_struct_rebuilt_from_its_fields_is_not_the_struct():
    # STRUCT(s.f, s.g) is not NULL even when s is
    assert not proves("SELECT STRUCT(t.s.f AS f, t.s.g AS g) AS v FROM t", "SELECT t.s AS v FROM t")
    left = "SELECT t.id FROM t WHERE STRUCT(t.s.f AS f, t.s.g AS g) IS NULL"
    assert norm(left).upper().count("STRUCT") == 1  # not folded to anything about s


def test_whole_struct_equality_is_never_proven_or_rewritten():
    left = "SELECT t.id FROM t WHERE STRUCT(t.a AS x, t.b AS y) = STRUCT(1 AS x, 2 AS y)"
    assert "STRUCT" in norm(left).upper()  # not read as t.a = 1 AND t.b = 2 (a NULL field makes it NULL, not FALSE)
    assert not proves("SELECT t.id FROM t WHERE t.s = t.s", "SELECT t.id FROM t")
    assert not proves(left, "SELECT t.id FROM t WHERE t.a = 1 OR t.b = 2")


# --- running both sides ------------------------------------------------------------------------------------------

DUCK_TYPES = {"id": "INT64", "a": "INT64", "b": "INT64", "s": "STRUCT<f INT64, g INT64>"}


@pytest.fixture
def db():
    connection = duckdb.connect(":memory:")
    configure(connection)
    connection.execute("CREATE TABLE t (id INT, a INT, b INT, s STRUCT(f INT, g INT))")
    connection.execute(
        "INSERT INTO t VALUES (1, 1, 2, {'f': 1, 'g': NULL}), (2, NULL, 3, NULL), (3, 4, NULL, {'f': NULL, 'g': NULL}), (4, 4, 5, {'f': 7, 'g': 8})"
    )
    connection.execute("CREATE TABLE u (id INT, a INT, b INT, k INT)")
    # key 1 repeats (a scalar subquery on it errors), key 2 has one row, key 3 and 4 have none
    connection.execute("INSERT INTO u VALUES (10, 5, 6, 1), (11, 7, NULL, 1), (12, NULL, 9, 2)")
    yield connection
    connection.close()


def outcome(db, sql: str):
    try:
        text = faithful(sqlglot.parse_one(sql, read="bigquery"), DUCK_TYPES).sql(dialect="duckdb")
    except Unfaithful:
        return "unfaithful"
    try:
        return sorted(bigquery_rows(db.execute(text).fetchall()), key=repr)
    except duckdb.Error as error:
        return f"error: {type(error).__name__}"


QUERIES = [
    "SELECT t.id, STRUCT(t.a AS x, t.b AS y).y AS v FROM t",
    "SELECT t.id, STRUCT(t.a AS x, t.b AS Y).x AS v FROM t",
    "SELECT t.id, STRUCT(t.s.f AS x, t.s.g AS y).y AS v FROM t",
    "SELECT t.id FROM t WHERE STRUCT(t.a AS x, t.b AS y).x > 1",
    "SELECT t.id, (SELECT AS STRUCT t.a AS x, t.b AS y).y AS v FROM t",
    "SELECT t.id, (SELECT AS STRUCT u.a AS x, u.b AS y FROM u WHERE u.k = t.id).y AS v FROM t WHERE t.id <> 1",
    "SELECT t.id, (SELECT AS STRUCT u.a AS x, u.b AS y FROM u WHERE u.k = t.id).y AS v FROM t",  # key 1 repeats: both error
    "SELECT t.id, (SELECT AS STRUCT u.a AS x, u.b AS y FROM u WHERE u.k = t.id AND u.a IS NOT NULL).x AS v FROM t",
    "SELECT t.id, (SELECT AS STRUCT u.a AS x, u.b AS y FROM u WHERE u.k = t.id ORDER BY u.id LIMIT 1).y AS v FROM t",
    "SELECT d.id, d.s.f AS v FROM (SELECT t.id AS id, STRUCT(t.a AS f, t.b AS g) AS s FROM t) AS d",
    "SELECT d.id, d.s.g AS v FROM (SELECT t.id AS id, STRUCT(t.a AS f, t.b + 1 AS g) AS s FROM t) AS d WHERE d.s.f IS NOT NULL",
    "SELECT d.s.n AS n FROM (SELECT STRUCT(t.a AS g, COUNT(*) AS n) AS s FROM t GROUP BY t.a) AS d",
]


@pytest.mark.parametrize("sql", QUERIES)
def test_query_and_normal_form_agree_on_duckdb(db, sql):
    folded = norm(sql)
    assert folded != sql  # the rule fired
    assert "STRUCT(" not in folded.upper().replace("STRUCT<", "")
    original = outcome(db, sql)
    assert original != "unfaithful"
    assert outcome(db, folded) == original


# BigQuery reads these, DuckDB's translation does not: the folded query must match the plain spelling instead
PLAIN = [
    ("SELECT t.id, STRUCT(STRUCT(t.a + t.b AS p) AS q).q.p AS v FROM t", "SELECT t.id, t.a + t.b AS v FROM t"),
    ("SELECT t.id, (SELECT AS VALUE STRUCT(t.a AS x, t.b AS y)).x AS v FROM t", "SELECT t.id, t.a AS v FROM t"),
    (
        "SELECT d.y AS v FROM (SELECT AS STRUCT t.a AS x, t.b AS y FROM t) AS d WHERE d.x IS NOT NULL",
        "SELECT d.y AS v FROM (SELECT t.a AS x, t.b AS y FROM t) AS d WHERE d.x IS NOT NULL",
    ),
    ("SELECT t.id, (t.s).f AS v FROM t", "SELECT t.id, t.s.f AS v FROM t"),
]


@pytest.mark.parametrize("sql, plain", PLAIN)
def test_normal_form_matches_the_plain_spelling_on_duckdb(db, sql, plain):
    expected = outcome(db, plain)
    assert expected != "unfaithful" and not str(expected).startswith("error")
    assert outcome(db, norm(sql)) == expected


def test_the_error_case_is_really_an_error(db):
    sql = "SELECT t.id, (SELECT AS STRUCT u.a AS x, u.b AS y FROM u WHERE u.k = t.id).y AS v FROM t"
    assert str(outcome(db, sql)).startswith("error")
    assert str(outcome(db, norm(sql))).startswith("error")


def test_struct_null_differs_from_null_fields_on_duckdb(db):
    # the data the rule must not be confused by: row 2 has a NULL struct, row 3 a struct of NULL fields
    by_struct = outcome(db, "SELECT t.id FROM t WHERE t.s IS NULL")
    by_fields = outcome(db, "SELECT t.id FROM t WHERE t.s.f IS NULL AND t.s.g IS NULL")
    assert by_struct == [(2,)]
    assert by_fields == [(2,), (3,)]
    assert norm("SELECT t.id FROM t WHERE t.s IS NULL") != norm("SELECT t.id FROM t WHERE t.s.f IS NULL AND t.s.g IS NULL")
