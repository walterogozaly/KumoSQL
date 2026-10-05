"""ARRAY(SELECT .. ORDER BY offset) and UNNEST round trips (``kumosql.nested_array_roundtrip``).

The rule folds only what BigQuery guarantees: the round trip of an array that cannot be NULL (a literal, or a column
a table stores), the UNNEST of a round trip, and the UNNEST of scalar literals as a UNION ALL when no row order can
show. Each soundness condition has a case that must NOT fold, and the folds are checked on DuckDB over data with empty
arrays, NULL arrays (where the array is computed) and repeated elements.
"""

from __future__ import annotations

import pytest

duckdb = pytest.importorskip("duckdb")
sqlglot = pytest.importorskip("sqlglot")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic  # noqa: E402
from kumosql.bigquery_on_duckdb import bigquery_rows, configure, faithful  # noqa: E402
from kumosql.nested_array_roundtrip import ASSUMPTION  # noqa: E402

TYPES = {
    "orders": {"order_id": "INT64", "customer_id": "INT64", "tags": "ARRAY<STRING>", "label": "STRING"},
    "customers": {"customer_id": "INT64", "name": "STRING"},
}
SCHEMA = {table: list(columns) for table, columns in TYPES.items()}

ROUND = "ARRAY(SELECT t FROM UNNEST({a}) AS t WITH OFFSET AS off ORDER BY off)"


def norm(sql: str, types=TYPES, assumptions: set | None = None) -> str:
    return normalize(sql, schema=SCHEMA, types=types, _assumptions=assumptions)


def folds(sql: str, expected: str, types=TYPES) -> None:
    assert norm(sql, types) == norm(expected, types)


def proven(left: str, right: str, **kwargs) -> bool:
    return prove_equivalent_algebraic(
        left, right, schema=SCHEMA, types=TYPES, dialect="bigquery", timeout_ms=10000, **kwargs
    ).proven


# --- the round trip of an array that cannot be NULL -------------------------------------------------


def test_stored_column_round_trip_is_the_column():
    seen: set = set()
    out = norm(f"SELECT order_id, {ROUND.format(a='tags')} AS tags FROM orders", assumptions=seen)
    assert "ARRAY(SELECT" not in out and "UNNEST" not in out
    assert out == norm("SELECT order_id, tags AS tags FROM orders")
    assert seen == {ASSUMPTION}


def test_qualified_and_aliased_columns_fold():
    folds(f"SELECT o.order_id, {ROUND.format(a='o.tags')} AS tags FROM orders AS o", "SELECT o.order_id, o.tags AS tags FROM orders AS o")


def test_element_and_offset_names_are_free():
    sql = "SELECT ARRAY(SELECT e FROM UNNEST(tags) AS e WITH OFFSET AS pos ORDER BY pos ASC) AS a FROM orders"
    folds(sql, "SELECT tags AS a FROM orders")


def test_unnamed_offset_defaults_to_offset():
    sql = "SELECT ARRAY(SELECT e FROM UNNEST(tags) AS e WITH OFFSET ORDER BY offset) AS a FROM orders"
    folds(sql, "SELECT tags AS a FROM orders")


def test_the_array_may_sit_in_a_nested_select_that_reads_the_outer_row():
    sql = f"SELECT order_id FROM orders AS o WHERE EXISTS (SELECT 1 FROM customers AS c WHERE c.customer_id = o.customer_id AND ARRAY_LENGTH({ROUND.format(a='o.tags')}) > 1)"
    assert "ARRAY(SELECT" not in norm(sql)


def test_literal_array_round_trip_folds_with_no_assumption():
    seen: set = set()
    out = norm("SELECT ARRAY(SELECT x FROM UNNEST([3, 1, 2]) AS x WITH OFFSET AS o ORDER BY o) AS a", assumptions=seen)
    assert out == norm("SELECT [3, 1, 2] AS a")
    assert not seen


def test_literal_array_with_null_element_keeps_it():
    # the round trip keeps NULL elements, order and length
    folds("SELECT ARRAY(SELECT x FROM UNNEST([1, NULL, 3]) AS x WITH OFFSET AS o ORDER BY o) AS a", "SELECT [1, NULL, 3] AS a")


@pytest.mark.parametrize(
    "select",
    [
        "SELECT t FROM UNNEST(tags) AS t WITH OFFSET AS off ORDER BY off DESC",  # reversed
        "SELECT t FROM UNNEST(tags) AS t WITH OFFSET AS off ORDER BY t",  # sorted by value, not position
        "SELECT t FROM UNNEST(tags) AS t WITH OFFSET AS off",  # no order at all
        "SELECT t FROM UNNEST(tags) AS t WITH OFFSET AS off WHERE t <> 'x' ORDER BY off",  # filtered
        "SELECT DISTINCT t FROM UNNEST(tags) AS t WITH OFFSET AS off ORDER BY off",  # deduplicated
        "SELECT UPPER(t) FROM UNNEST(tags) AS t WITH OFFSET AS off ORDER BY off",  # computed element
        "SELECT off FROM UNNEST(tags) AS t WITH OFFSET AS off ORDER BY off",  # the offsets, not the elements
        "SELECT t FROM UNNEST(tags) AS t WITH OFFSET AS off ORDER BY off LIMIT 2",  # truncated
        "SELECT AS STRUCT t, off FROM UNNEST(tags) AS t WITH OFFSET AS off ORDER BY off",  # structs
        "SELECT t FROM UNNEST(tags) AS t WITH OFFSET AS off ORDER BY off, t",  # a second key
    ],
)
def test_other_subqueries_are_left_alone(select):
    assert "ARRAY(SELECT" in norm(f"SELECT ARRAY({select}) AS a FROM orders")


def test_unordered_unnest_without_offset_is_left_alone():
    assert "ARRAY(SELECT" in norm("SELECT ARRAY(SELECT t FROM UNNEST(tags) AS t) AS a FROM orders")


def test_a_computed_array_may_be_null_so_it_is_left_alone():
    # ARRAY_CONCAT(NULL, ..) is NULL, while ARRAY(SELECT .. FROM UNNEST(NULL)) is []
    assert "ARRAY(SELECT" in norm(f"SELECT {ROUND.format(a='ARRAY_CONCAT(tags, tags)')} AS a FROM orders")
    assert "ARRAY(SELECT" in norm(f"SELECT {ROUND.format(a='CAST(NULL AS ARRAY<STRING>)')} AS a FROM orders")


def test_a_column_the_schema_does_not_type_as_array_is_left_alone():
    assert "ARRAY(SELECT" in norm(f"SELECT {ROUND.format(a='label')} AS a FROM orders")
    # no types at all: nothing is known about the column
    assert "ARRAY(SELECT" in normalize(f"SELECT {ROUND.format(a='tags')} AS a FROM orders", schema=SCHEMA)


def test_the_null_extended_side_of_an_outer_join_is_left_alone():
    # an unmatched customer reads o.tags as NULL, and ARRAY(..) of that is []
    left = f"SELECT c.customer_id, {ROUND.format(a='o.tags')} AS a FROM customers AS c LEFT JOIN orders AS o ON o.customer_id = c.customer_id"
    assert "ARRAY(SELECT" in norm(left)
    right_join = f"SELECT c.customer_id, {ROUND.format(a='o.tags')} AS a FROM orders AS o RIGHT JOIN customers AS c ON o.customer_id = c.customer_id"
    assert "ARRAY(SELECT" in norm(right_join)
    full = f"SELECT {ROUND.format(a='o.tags')} AS a FROM orders AS o FULL JOIN customers AS c ON o.customer_id = c.customer_id"
    assert "ARRAY(SELECT" in norm(full)


def test_the_preserved_side_of_a_left_join_still_folds():
    sql = f"SELECT o.order_id, {ROUND.format(a='o.tags')} AS a FROM orders AS o LEFT JOIN customers AS c ON o.customer_id = c.customer_id"
    assert "ARRAY(SELECT" not in norm(sql)


def test_a_derived_table_column_is_left_alone():
    # the derived table may compute its column; the rule does not read through it
    unqualified = f"SELECT {ROUND.format(a='tags')} AS a FROM (SELECT ARRAY_CONCAT(tags, tags) AS tags FROM orders) AS d"
    assert "ARRAY(SELECT" in norm(unqualified)


def test_an_ambiguous_unqualified_column_is_left_alone():
    types = {**TYPES, "other": {"tags": "ARRAY<STRING>"}}
    sql = f"SELECT {ROUND.format(a='tags')} AS a FROM orders, other"
    assert "ARRAY(SELECT" in normalize(sql, schema={t: list(c) for t, c in types.items()}, types=types)


# --- UNNEST of a round trip ---------------------------------------------------------------------------


def test_unnest_of_a_round_trip_is_the_unnest_even_for_a_computed_array():
    sql = f"SELECT o.order_id, t, off FROM orders AS o, UNNEST({ROUND.format(a='ARRAY_CONCAT(o.tags, o.tags)')}) AS t WITH OFFSET AS off"
    folds(sql, "SELECT o.order_id, t, off FROM orders AS o, UNNEST(ARRAY_CONCAT(o.tags, o.tags)) AS t WITH OFFSET AS off")


def test_unnest_of_a_filtered_or_reordered_subquery_is_kept():
    sql = "SELECT t FROM orders AS o, UNNEST(ARRAY(SELECT t FROM UNNEST(o.tags) AS t WITH OFFSET AS off ORDER BY off DESC)) AS t WITH OFFSET AS pos"
    assert "ARRAY(SELECT" in norm(sql)


def test_in_unnest_of_a_computed_round_trip_is_not_folded():
    # NULL IN UNNEST(<NULL array>) is not known to equal NULL IN UNNEST([]) here: leave the test alone
    sql = f"SELECT order_id FROM orders WHERE label IN UNNEST({ROUND.format(a='ARRAY_CONCAT(tags, tags)')})"
    assert "ARRAY(SELECT" in norm(sql)


# --- UNNEST of literals ------------------------------------------------------------------------------------


def test_unnest_of_literals_is_a_union_all():
    assert "UNNEST" not in norm("SELECT x FROM UNNEST([1, 2, 3]) AS x")
    assert proven("SELECT x FROM UNNEST([1, 2, 3]) AS x", "SELECT 1 AS x UNION ALL SELECT 2 UNION ALL SELECT 3")


def test_with_offset_counts_from_zero():
    assert proven("SELECT x, o FROM UNNEST(['a', 'b']) AS x WITH OFFSET AS o", "SELECT 'a' AS x, 0 AS o UNION ALL SELECT 'b', 1")
    assert not proven("SELECT x, o FROM UNNEST(['a', 'b']) AS x WITH OFFSET AS o", "SELECT 'a' AS x, 1 AS o UNION ALL SELECT 'b', 2")


def test_null_elements_and_negative_numbers_are_rows():
    assert "UNNEST" not in norm("SELECT x FROM UNNEST([-1, NULL, 2]) AS x")
    assert proven("SELECT x FROM UNNEST([-1, NULL, 2]) AS x", "SELECT -1 AS x UNION ALL SELECT NULL UNION ALL SELECT 2")


def test_literals_joined_to_a_table_are_a_cross_join():
    sql = "SELECT o.order_id, x FROM orders AS o, UNNEST([1, 2]) AS x"
    assert "UNNEST" not in norm(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x FROM UNNEST([1, 'a']) AS x",  # mixed kinds are not a plain literal list
        "SELECT x FROM UNNEST([1, 2.5]) AS x",  # integers and decimals coerce; not touched
        "SELECT s.a FROM UNNEST([STRUCT(1 AS a)]) AS s",  # structs
        "SELECT x FROM UNNEST([NULL, NULL]) AS x",  # no type to give the rows
        "SELECT x FROM UNNEST(ARRAY<INT64>[1, 2]) AS x",
        "SELECT x FROM UNNEST(ARRAY<INT64>[]) AS x",
    ],
)
def test_unnest_of_other_arrays_is_kept(sql):
    assert "UNNEST" in norm(sql)


def test_a_left_joined_unnest_is_kept():
    assert "UNNEST" in norm("SELECT o.order_id, x FROM orders AS o LEFT JOIN UNNEST([1, 2]) AS x ON x = o.order_id")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x, ROW_NUMBER() OVER () AS n FROM UNNEST([1, 2, 3]) AS x",  # a window numbers rows in arrival order
        "SELECT x FROM UNNEST([1, 2, 3]) AS x LIMIT 1",  # which row survives is unspecified
        "SELECT ARRAY_AGG(x) FROM UNNEST([1, 2, 3]) AS x",  # the array's order is unspecified
        "SELECT STRING_AGG(x) FROM UNNEST(['a', 'b']) AS x",
        "SELECT ANY_VALUE(x) FROM UNNEST([1, 2]) AS x",
    ],
)
def test_a_result_that_can_depend_on_row_order_is_not_rewritten(sql):
    assert "UNNEST" in norm(sql)


# --- the prover -----------------------------------------------------------------------------------------


def test_proves_the_round_trip_pair():
    assert proven(f"SELECT order_id, {ROUND.format(a='tags')} AS tags FROM orders", "SELECT order_id, tags FROM orders")


def test_proof_lists_the_stored_array_assumption():
    result = prove_equivalent_algebraic(
        f"SELECT order_id, {ROUND.format(a='tags')} AS tags FROM orders",
        "SELECT order_id, tags FROM orders",
        schema=SCHEMA, types=TYPES, dialect="bigquery", timeout_ms=10000,
    )
    assert result.proven and ASSUMPTION in result.assumptions


def test_proves_unnest_literals_against_union_all():
    assert proven("SELECT x FROM UNNEST([1, 2, 3]) AS x", "SELECT 1 AS x UNION ALL SELECT 2 UNION ALL SELECT 3")
    assert proven("SELECT x, o FROM UNNEST(['a', 'b']) AS x WITH OFFSET AS o", "SELECT 'a' AS x, 0 AS o UNION ALL SELECT 'b', 1")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # a repeated element is a repeated row
        ("SELECT x FROM UNNEST([1, 2, 2]) AS x", "SELECT 1 AS x UNION DISTINCT SELECT 2"),
        ("SELECT x FROM UNNEST([1, 2, 2]) AS x", "SELECT 1 AS x UNION ALL SELECT 2"),
        # offsets start at 0
        ("SELECT x, o FROM UNNEST(['a', 'b']) AS x WITH OFFSET AS o", "SELECT 'a' AS x, 1 AS o UNION ALL SELECT 'b', 2"),
        # the round trip of a NULL-extended array is [], not NULL
        (
            f"SELECT c.customer_id, {ROUND.format(a='o.tags')} AS a FROM customers AS c LEFT JOIN orders AS o ON o.customer_id = c.customer_id",
            "SELECT c.customer_id, o.tags AS a FROM customers AS c LEFT JOIN orders AS o ON o.customer_id = c.customer_id",
        ),
        # an array computed with ARRAY_CONCAT can be NULL
        (f"SELECT {ROUND.format(a='ARRAY_CONCAT(tags, tags)')} AS a FROM orders", "SELECT ARRAY_CONCAT(tags, tags) AS a FROM orders"),
        # reversed order
        (
            "SELECT ARRAY(SELECT t FROM UNNEST(tags) AS t WITH OFFSET AS off ORDER BY off DESC) AS a FROM orders",
            "SELECT tags AS a FROM orders",
        ),
        # one element dropped
        (
            "SELECT ARRAY(SELECT t FROM UNNEST(tags) AS t WITH OFFSET AS off WHERE off > 0 ORDER BY off) AS a FROM orders",
            "SELECT tags AS a FROM orders",
        ),
    ],
)
def test_traps_are_never_proven(left, right):
    assert not proven(left, right)
    assert not proven(right, left)
    assert not proven(left, right, search_counterexample=True)


# --- the folds on DuckDB ----------------------------------------------------------------------------------


@pytest.fixture
def db():
    connection = duckdb.connect(":memory:")
    configure(connection)
    connection.execute("CREATE TABLE orders(order_id INTEGER, tags VARCHAR[], more VARCHAR[])")
    connection.execute(
        "INSERT INTO orders VALUES (1, ['b', 'a'], ['z']), (2, [], NULL), (3, ['a', 'a', 'c'], []), (4, ['c'], ['q', 'q'])"
    )
    yield connection
    connection.close()


COLUMNS = {"tags": "ARRAY<STRING>", "more": "ARRAY<STRING>", "order_id": "INT64"}


def rows(db, sql: str):
    statement = faithful(sqlglot.parse_one(sql, read="bigquery"), COLUMNS).sql(dialect="duckdb")
    return sorted(bigquery_rows(db.execute(statement).fetchall()), key=repr)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x FROM UNNEST([1, 2, 2, 3]) AS x",
        "SELECT x, o FROM UNNEST(['a', NULL, 'a']) AS x WITH OFFSET AS o",
        "SELECT o.order_id, x FROM orders AS o, UNNEST([10, 20]) AS x WHERE o.order_id > 1",
        "SELECT x, o FROM UNNEST([1.5, 2.5]) AS x WITH OFFSET AS o WHERE o = 1",
        "SELECT x FROM UNNEST([TRUE, FALSE, TRUE]) AS x",
        "SELECT order_id, t, off FROM orders, UNNEST(ARRAY(SELECT t FROM UNNEST(ARRAY_CONCAT(tags, more)) AS t WITH OFFSET AS o ORDER BY o)) AS t WITH OFFSET AS off",
        "SELECT order_id, t, off FROM orders, UNNEST(ARRAY(SELECT t FROM UNNEST(tags) AS t WITH OFFSET AS o ORDER BY o)) AS t WITH OFFSET AS off",
    ],
)
def test_the_normalized_query_returns_the_same_rows_on_duckdb(db, sql):
    normalized = normalize(sql, schema={"orders": ["order_id", "tags", "more"]}, types={"orders": COLUMNS})
    assert normalized != sqlglot.parse_one(sql, read="bigquery").sql("bigquery")
    assert rows(db, normalized) == rows(db, sql)


def test_the_round_trip_of_a_stored_array_is_the_array_and_of_a_null_array_is_empty(db):
    # BigQuery: ARRAY(SELECT .. FROM UNNEST(NULL)) is [], so only a never-NULL array may be folded to itself
    stored = db.execute("SELECT order_id, tags FROM orders ORDER BY order_id").fetchall()
    assert all(tags is not None for _, tags in stored)
    # ARRAY_CONCAT is NULL when an argument is NULL
    computed = db.execute("SELECT order_id, CASE WHEN more IS NULL THEN NULL ELSE list_concat(tags, more) END FROM orders ORDER BY order_id").fetchall()
    assert any(value is None for _, value in computed)  # a computed array can be NULL: the fold is declined for it
