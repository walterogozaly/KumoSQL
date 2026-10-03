"""Wrong output properties found on master, kept as regression cases.

Each fact checked here was claimed by ``infer_properties`` or ``profile_query`` about a query whose
DuckDB result violates it. None of them made a prover return a wrong verdict on its own, but each one
is a premise a prover rests on (``at_most_one_row`` decides whether a scalar-subquery proof lists its
"at most one row" assumption). Controls keep the ordinary facts next to each case, so a fix cannot
just give up on them.
"""

from collections import Counter

import pytest

from kumosql import Pipeline, Target, profile_query
from kumosql.output_properties import infer_properties
from kumosql.smt_equivalence import TableConstraints

SCHEMA = {"t": ["x", "s", "y"], "u": ["x", "s", "y"]}
DDL = {"t": "CREATE TABLE t (x INTEGER, s VARCHAR, y INTEGER)", "u": "CREATE TABLE u (x INTEGER, s VARCHAR, y INTEGER)"}
KEYED = {
    "t": TableConstraints(not_null=frozenset({"x", "s"}), keys=(("x",),)),
    "u": TableConstraints(not_null=frozenset({"x"}), keys=(("x",),)),
}
NULL_ROW = {"t": [(None, None, None)], "u": [(None, None, None)]}


def _rows(sql: str, rows: dict[str, list[tuple]] | None = None) -> Counter:
    """The DuckDB result bag of ``sql`` (DuckDB syntax) on tables ``t`` and ``u`` holding ``rows``."""

    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    for name, create in DDL.items():
        db.execute(create)
        for row in (rows or {}).get(name, []):
            db.execute(f"INSERT INTO {name} VALUES (?, ?, ?)", row)
    return Counter(db.execute(sql).fetchall())


def _props(sql: str, constraints=None, dialect: str = "bigquery"):
    return infer_properties(sql, constraints or {}, SCHEMA, dialect=dialect)


def _supported(sql: str, constraints=None, dialect: str = "bigquery"):
    result = _props(sql, constraints, dialect)
    assert not result.unsupported, result.unsupported
    return result


def _profile(sql: str, declared_grain=None):
    table = Target("proj", "raw", "t")
    pipeline = Pipeline({}, sources={table.key: table}, source_schema={table.key: {"x": "INT64", "s": "STRING", "y": "INT64"}})
    return profile_query(pipeline, sql, declared_grain=declared_grain)


# ---------------------------------------------------------------- FULL JOIN keys


def test_full_join_does_not_combine_the_side_keys():
    sql = "SELECT a.x AS ax, b.x AS bx FROM (SELECT DISTINCT x FROM t) a FULL JOIN (SELECT DISTINCT x FROM u) b ON FALSE"
    # Each side's unmatched NULL key is padded with NULLs: (NULL, NULL) twice.
    assert _rows(sql, NULL_ROW) == Counter({(None, None): 2})
    assert not _supported(sql).is_unique("ax", "bx")


def test_full_join_of_two_single_rows_can_return_two_rows():
    sql = "SELECT a.x AS z FROM (SELECT 1 AS x) a FULL JOIN (SELECT 2 AS y) b ON FALSE"
    assert sum(_rows(sql).values()) == 2
    result = _supported(sql)
    assert not result.at_most_one_row and result.scalar_subquery == "unknown"


def test_a_scalar_subquery_over_a_full_join_keeps_its_assumption():
    pytest.importorskip("z3")
    from kumosql import scalar_subqueries
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    sql = "SELECT (SELECT a.x AS z FROM (SELECT 1 AS x) a FULL JOIN (SELECT 2 AS y) b ON FALSE) AS z"
    result = prove_equivalent_algebraic(sql, sql, schema=SCHEMA)
    # The proof replaces the subquery by one value; it may only skip saying so when one row is proven.
    assert not result.proven or scalar_subqueries.ASSUMPTION in result.assumptions
    single = "SELECT (SELECT a.x AS z FROM (SELECT 1 AS x) a LEFT JOIN (SELECT 2 AS y) b ON FALSE) AS z"
    assert _supported(single.removeprefix("SELECT (").removesuffix(") AS z")).at_most_one_row
    control = prove_equivalent_algebraic(single, single, schema=SCHEMA)
    assert control.proven and scalar_subqueries.ASSUMPTION not in control.assumptions


def test_full_join_keeps_the_pair_key_when_a_key_column_is_never_null():
    sql = "SELECT a.x AS ax, b.x AS bx FROM (SELECT DISTINCT x FROM t WHERE x IS NOT NULL) a FULL JOIN (SELECT DISTINCT x FROM u) b ON a.x = b.x"
    bag = _rows(sql, {"t": [(1, "a", 1), (2, "b", 2)], "u": [(None, None, None), (1, "a", 1), (3, "c", 3)]})
    assert sum(bag.values()) == 4 and set(bag.values()) == {1}
    assert _supported(sql).is_unique("ax", "bx")
    declared = _supported("SELECT t.x AS tx, u.x AS ux FROM t FULL JOIN u ON u.x = t.y", KEYED)
    assert declared.is_unique("tx", "ux") and not declared.is_unique("tx")
    (key,) = [k for k in declared.keys if set(k.columns) == {"tx", "ux"}]
    assert "t.x is NOT NULL" in key.assumptions or "u.x is NOT NULL" in key.assumptions


def test_inner_and_left_join_keys_are_still_inferred():
    inner = _supported("SELECT t.x AS tx, u.x AS ux FROM t JOIN u ON t.y = u.y", KEYED)
    assert inner.is_unique("tx", "ux") and not inner.is_unique("tx")
    lookup = _supported("SELECT t.x AS tx, u.s FROM t JOIN u ON u.x = t.y", KEYED)
    assert lookup.is_unique("tx")
    left = _supported("SELECT t.x AS tx, u.s FROM t LEFT JOIN u ON u.x = t.y", KEYED)
    assert left.is_unique("tx") and left.non_null("tx") and not left.non_null("s")
    right = _supported("SELECT t.s, u.x AS ux FROM t RIGHT JOIN u ON t.x = u.y", KEYED)
    assert right.is_unique("ux")


# ---------------------------------------------------------------- function arguments


@pytest.mark.parametrize(
    "sql, duck",
    [
        ("SELECT SUBSTR('abc', x) AS z FROM t", "SELECT SUBSTRING('abc', x) AS z FROM t"),
        ("SELECT REPLACE('abc', 'a', s) AS z FROM t", "SELECT REPLACE('abc', 'a', s) AS z FROM t"),
        ("SELECT ROUND(CAST(1.25 AS FLOAT64), x) AS z FROM t", "SELECT ROUND(CAST(1.25 AS DOUBLE), x) AS z FROM t"),
        ("SELECT SUBSTR('abc', 1, x) AS z FROM t", "SELECT SUBSTRING('abc', 1, x) AS z FROM t"),
    ],
    ids=["substr-start", "replace-replacement", "round-decimals", "substr-length"],
)
def test_a_null_argument_outside_this_makes_the_result_null(sql, duck):
    assert _rows(duck, NULL_ROW) == Counter({(None,): 1})
    assert not _supported(sql).non_null("z")


def test_grouping_sets_key_needs_every_argument_non_null():
    sql = "SELECT SUBSTR('abc', x) AS z FROM t GROUP BY GROUPING SETS ((SUBSTR('abc', x)), ())"
    duck = "SELECT SUBSTRING('abc', x) AS z FROM t GROUP BY GROUPING SETS ((SUBSTRING('abc', x)), ())"
    # The group of x = NULL and the grand total both show z = NULL.
    assert _rows(duck, NULL_ROW) == Counter({(None,): 2})
    result = _supported(sql)
    assert not result.non_null("z") and not result.is_unique("z")
    assert not _supported("SELECT SUBSTR(s, y) AS z FROM t", KEYED).non_null("z")  # s is NOT NULL, y is not


def test_strict_functions_of_non_null_arguments_stay_non_null():
    result = _supported(
        "SELECT SUBSTR(s, 2) AS a, SUBSTR(s, 1, 2) AS b, REPLACE(s, 'a', 'b') AS c, ROUND(x, 2) AS d, UPPER(s) AS e FROM t",
        KEYED,
    )
    assert all(result.non_null(name) for name in "abcde")
    assert result.assumptions_for("a") == ("t.s is NOT NULL",)
    grouped = _supported("SELECT SUBSTR(s, 1) AS z FROM t GROUP BY GROUPING SETS ((SUBSTR(s, 1)), ())", KEYED)
    assert grouped.is_unique("z")


# ---------------------------------------------------------------- star modifiers


def test_star_replace_uses_the_replacement():
    sql = "SELECT * REPLACE (NULL AS x) FROM (SELECT 1 AS x UNION DISTINCT SELECT 2 AS x)"
    assert _rows("SELECT * REPLACE (NULL AS x) FROM (SELECT 1 AS x UNION SELECT 2 AS x)") == Counter({(None,): 2})
    result = _supported(sql)
    assert [c.name for c in result.columns] == ["x"]
    assert not result.non_null("x") and not result.is_unique("x")


def test_star_except_drops_the_column():
    sql = "SELECT * EXCEPT (x) FROM (SELECT 1 AS x, 7 AS y UNION DISTINCT SELECT 2 AS x, 7 AS y)"
    assert _rows("SELECT * EXCLUDE (x) FROM (SELECT 1 AS x, 7 AS y UNION SELECT 2 AS x, 7 AS y)") == Counter({(7,): 2})
    result = _supported(sql)
    assert [c.name for c in result.columns] == ["y"]
    assert not result.keys


def test_plain_stars_keep_their_facts():
    plain = _supported("SELECT * FROM (SELECT 1 AS x UNION DISTINCT SELECT 2 AS x)")
    assert plain.non_null("x") and plain.is_unique("x")
    keyed = _supported("SELECT * FROM t", KEYED)
    assert [c.name for c in keyed.columns] == ["x", "s", "y"] and keyed.is_unique("x") and keyed.non_null("s")
    joined = _supported("SELECT t.*, u.s AS us FROM t JOIN u ON u.x = t.y", KEYED)
    assert [c.name for c in joined.columns] == ["x", "s", "y", "us"] and joined.is_unique("x")


def test_star_modifiers_keep_the_facts_that_still_hold():
    dropped = _supported("SELECT * EXCEPT (y) FROM t", KEYED)
    assert [c.name for c in dropped.columns] == ["x", "s"] and dropped.is_unique("x")
    replaced = _supported("SELECT * REPLACE (UPPER(s) AS s, y + 1 AS y) FROM t", KEYED)
    assert [c.name for c in replaced.columns] == ["x", "s", "y"]
    assert replaced.is_unique("x") and replaced.non_null("s") and not replaced.non_null("y")
    qualified = _supported("SELECT t.* EXCEPT (s), u.s FROM t JOIN u ON u.x = t.y", KEYED)
    assert [c.name for c in qualified.columns] == ["x", "y", "s"] and qualified.is_unique("x")
    assert _props("SELECT * RENAME (x AS z) FROM t", KEYED, dialect="snowflake").unsupported


# ---------------------------------------------------------------- set-returning select lists


def test_select_list_unnest_is_not_a_single_row():
    sql = "SELECT UNNEST([1, 1]) AS z"
    assert _rows(sql) == Counter({(1,): 2})
    result = _props(sql, dialect="duckdb")
    assert not result.exactly_one_row and not result.at_most_one_row
    profile = _profile(sql)
    assert not profile.grain.known and not profile.complete


def test_select_list_unnest_repeats_the_input_key():
    sql = "SELECT x, UNNEST([1, 2]) AS z FROM t"
    assert _rows(sql, {"t": [(1, "a", 1)]}) == Counter({(1, 1): 1, (1, 2): 1})
    assert not _props(sql, KEYED, dialect="duckdb").is_unique("x")
    assert not _profile("SELECT x, UNNEST([1, 2]) AS z FROM proj.raw.t", {"proj.raw.t": ["x"]}).grain.known


def test_queries_without_set_returning_items_keep_their_row_facts():
    one = _supported("SELECT 1 AS z", dialect="duckdb")
    assert one.exactly_one_row
    assert _profile("SELECT 1 AS z").grain.keys == () and _profile("SELECT 1 AS z").grain.known
    member = _supported("SELECT x, x IN UNNEST([1, 2]) AS b, (SELECT COUNT(*) FROM UNNEST([1, 2])) AS n FROM t", KEYED)
    assert member.is_unique("x")
    grain = _profile("SELECT x, (SELECT COUNT(*) FROM UNNEST([1, 2])) AS n FROM proj.raw.t", {"proj.raw.t": ["x"]}).grain
    assert grain.known and grain.keys == ("x",)
    assert _props("SELECT * FROM UNNEST([1, 2]) AS x").unsupported  # FROM UNNEST stays declined


# ---------------------------------------------------------------- DISTINCT ON


def test_distinct_on_does_not_make_the_output_unique():
    rows = {"t": [(1, "a", 7), (2, "b", 7)]}
    assert _rows("SELECT DISTINCT ON (x) y AS z FROM t", rows) == Counter({(7,): 2})
    profile = _profile("SELECT DISTINCT ON (x) y AS z FROM proj.raw.t")
    assert profile.grain.keys != ("z",) and not profile.complete
    assert not _props("SELECT DISTINCT ON (x) y AS z FROM t", dialect="duckdb").is_unique("z")


def test_distinct_on_keeps_the_grain_of_its_input():
    keyed = _profile("SELECT DISTINCT ON (y) x, y FROM proj.raw.t", {"proj.raw.t": ["x"]})
    assert keyed.grain.known and keyed.grain.keys == ("x",)  # a subset of the rows keeps the key


def test_plain_distinct_keeps_its_grain():
    distinct = _profile("SELECT DISTINCT y AS z FROM proj.raw.t")
    assert distinct.grain.known and distinct.grain.keys == ("z",) and distinct.complete
    assert _supported("SELECT DISTINCT y AS z FROM t").is_unique("z")
