import duckdb
import sqlite3
import pytest
import sqlglot
from sqlglot import exp

from kumosql.ast_utils import FROM_KEY
from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.projected_in_rules import normalize_projected_in
from kumosql.smt_equivalence import TableConstraints


NN = {"t": frozenset({"x"}), "u": frozenset({"y"})}


@pytest.mark.parametrize("sql", [
    "SELECT (t.X) IN (SELECT u.Y FROM u) AS d FROM t",
    "SELECT X IN (SELECT Y FROM u t WHERE Y > 0) AS d FROM t",
    "SELECT t.X IN (SELECT u.Y FROM u WHERE u.id < t.id) AS d FROM t",
])
@pytest.mark.parametrize("values", [[], [(1,), (1,), (2,), (4,)]])
def test_nonnull_membership_preserves_empty_input_and_duplicate_rows(sql, values):
    tree = sqlglot.parse_one(sql, read="mysql")
    rewritten = normalize_projected_in(tree, NN)
    assert rewritten is not None
    assert rewritten.find(exp.Exists) is not None
    with duckdb.connect() as db:
        db.execute("SET threads=1")
        db.execute("CREATE TABLE t(id INTEGER, x INTEGER NOT NULL)")
        db.execute("CREATE TABLE u(id INTEGER, y INTEGER NOT NULL)")
        db.execute("INSERT INTO t VALUES (1, 1), (2, 2), (3, 4), (4, 4)")
        if values:
            db.executemany("INSERT INTO u VALUES (1, ?)", values)
        left, right = run_unoptimized(db, sql, rewritten.sql(dialect="duckdb"))
        assert left == right


@pytest.mark.parametrize("sql, facts", [
    ("SELECT x IN (SELECT y FROM u) AS d FROM t", {"t": {"x"}}),
    ("SELECT x IN (SELECT y FROM u) AS d FROM t", {"u": {"y"}}),
    ("SELECT x IN (SELECT y FROM u LIMIT 1) AS d FROM t", NN),
    ("SELECT x IN (SELECT MAX(y) FROM u) AS d FROM t", NN),
    ("SELECT x IN (SELECT y FROM u GROUP BY y) AS d FROM t", NN),
    ("SELECT x IN (SELECT DISTINCT y FROM u) AS d FROM t", NN),
    ("SELECT x IN (SELECT y AS z FROM u ORDER BY z) AS d FROM t", NN),
    ("SELECT COUNT(*), (x) IN (SELECT y FROM u) AS d FROM t", NN),
    ("SELECT (x) IN (SELECT y FROM u) AS d FROM t GROUP BY x", NN),
    ("SELECT (x) IN (SELECT y FROM u) AS d FROM t GROUP BY ROLLUP(x)", NN),
    ("SELECT t.x IN (SELECT u.y FROM u) AS d FROM t LEFT JOIN u ON FALSE", NN),
    ("SELECT t.x IN (SELECT u.y FROM u) AS d FROM u RIGHT JOIN t ON FALSE", NN),
    ("SELECT x IN (SELECT y FROM u LEFT JOIN t ON FALSE) AS d FROM t", NN),
    ("SELECT (x, id) IN (SELECT y, id FROM u) AS d FROM t", NN),
    ("SELECT x + 0 IN (SELECT y FROM u) AS d FROM t", NN),
    ("SELECT x IN (SELECT y FROM u) AS d FROM t z(x,id)", NN),
])
def test_scope_or_nullability_uncertainty_is_refused(sql, facts):
    tree = sqlglot.parse_one(sql, read="mysql")
    assert normalize_projected_in(tree, facts) is None


def test_nullable_projected_in_is_not_exists():
    left = "SELECT x IN (SELECT y FROM u) AS d FROM t"
    right = "SELECT EXISTS(SELECT 1 FROM u WHERE u.y=t.x) AS d FROM t"
    result = prove_equivalent_algebraic(
        left, right, schema={"t": ["x"], "u": ["y"]},
        constraints={"t": TableConstraints(not_null=frozenset({"x"}))},
        compare_names=False,
    )
    assert not result.proven
    with duckdb.connect() as db:
        db.execute("CREATE TABLE t(x INTEGER); CREATE TABLE u(y INTEGER)")
        db.execute("INSERT INTO t VALUES (2); INSERT INTO u VALUES (NULL)")
        assert run_unoptimized(db, left, right) == [[(None,)], [(False,)]]


def test_empty_global_aggregate_does_not_reuse_base_not_null():
    original = "SELECT COUNT(*), (x) IN (SELECT y FROM u) AS d FROM t"
    incorrect = "SELECT COUNT(*), EXISTS(SELECT 1 FROM u WHERE y=t.x) AS d FROM t"
    assert normalize_projected_in(sqlglot.parse_one(original, read="sqlite"), NN) is None
    assert not prove_equivalent_algebraic(
        original, incorrect, schema={"t": ["x"], "u": ["y"]},
        constraints={name: TableConstraints(not_null=cols) for name, cols in NN.items()},
        compare_names=False, dialect="sqlite",
    ).proven
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE t(x INTEGER NOT NULL)")
        db.execute("CREATE TABLE u(y INTEGER NOT NULL)")
        db.execute("INSERT INTO u VALUES (1)")
        assert db.execute(original).fetchall() == [(0, None)]
        assert db.execute(incorrect).fetchall() == [(0, 0)]


def test_identity_projection_is_kept_under_aggregate_ancestor():
    original = "WITH q AS (SELECT x AS x FROM t) SELECT COUNT(*), x IN (SELECT y FROM u) AS d FROM q"
    incorrect = "SELECT COUNT(*), EXISTS(SELECT 1 FROM u WHERE y=q.x) AS d FROM t q"
    schema = {"t": ["x"], "u": ["y"]}
    normalized = normalize(original, schema=schema, not_null=NN, dialect="sqlite")
    assert " IN " in normalized
    assert not prove_equivalent_algebraic(
        original, incorrect, schema=schema,
        constraints={name: TableConstraints(not_null=cols) for name, cols in NN.items()},
        compare_names=False, dialect="sqlite",
    ).proven
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE t(x INTEGER NOT NULL)")
        db.execute("CREATE TABLE u(y INTEGER NOT NULL)")
        db.execute("INSERT INTO u VALUES (1)")
        assert db.execute(original).fetchall() == db.execute(normalized).fetchall() == [(0, None)]
        assert db.execute(incorrect).fetchall() == [(0, 0)]


def test_identity_column_aliases_unlock_cte_membership_without_capture():
    left = "WITH q AS (SELECT id, X AS X FROM t) SELECT id, X IN (SELECT X FROM q WHERE id<3) AS d FROM q"
    right = "SELECT t.id, EXISTS(SELECT 1 FROM t u WHERE u.id<3 AND u.x=t.x) AS d FROM t"
    result = prove_equivalent_algebraic(
        left, right, schema={"t": ["id", "x"]},
        constraints={"t": TableConstraints(not_null=frozenset({"x"}))},
        compare_names=False, dialect="mysql",
    )
    assert result.proven


@pytest.mark.parametrize("item", ["x AS other", "x AS X", "`x` AS x", "x AS `x`"])
def test_nonidentity_or_differently_quoted_alias_is_kept(item):
    tree = sqlglot.parse_one(f"SELECT d.x FROM (SELECT {item} FROM t) AS d", read="mysql")
    assert normalize_projected_in(tree.args[FROM_KEY].this.this, NN) is None


def test_identity_alias_cleanup_preserves_output_names_and_hidden_column_capture():
    tree = sqlglot.parse_one("SELECT d.x FROM (SELECT x AS x FROM t) AS d", read="mysql")
    inner = tree.args[FROM_KEY].this.this
    assert normalize_projected_in(inner, NN) is inner
    assert inner.expressions[0].sql() == "x"
    # Existing inlining must still refuse to expose the base table's hidden y
    # where an unqualified nested reference previously belonged to its parent.
    left = "SELECT o.y FROM u o WHERE EXISTS(SELECT 1 FROM (SELECT x AS x FROM t) d WHERE y=1)"
    right = "SELECT o.y FROM u o WHERE EXISTS(SELECT 1 FROM t d WHERE d.y=1)"
    assert not prove_equivalent_algebraic(
        left, right, schema={"t": ["x", "y"], "u": ["y"]},
        compare_names=False, dialect="mysql",
    ).proven
    with duckdb.connect() as db:
        db.execute("CREATE TABLE t(x INTEGER, y INTEGER); CREATE TABLE u(y INTEGER)")
        db.execute("INSERT INTO t VALUES (9,0); INSERT INTO u VALUES (1)")
        assert run_unoptimized(db, left, right) == [[(1,)], []]
