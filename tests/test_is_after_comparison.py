"""``a = b IS TRUE`` read as the engines read it, ``(a = b) IS TRUE`` (``ast_utils.read_is_after_comparison``)."""

import itertools

import duckdb
import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.ast_utils import UnmodeledConstruct, canonical_negation, check_modeled
from kumosql.duckdb_load import run_unoptimized
from kumosql.smt_equivalence import prove_equivalent_smt

SCHEMA = {"s": ["a", "b", "c"]}
PROVERS = (prove_equivalent_algebraic, prove_equivalent_smt)


def _read(sql: str) -> sqlglot.exp.Expression:
    return check_modeled(canonical_negation(sqlglot.parse_one(sql, read="mysql")))


@pytest.mark.parametrize(
    "bare, parenthesized",
    [
        ("SELECT a = b IS TRUE FROM s", "SELECT (a = b) IS TRUE FROM s"),
        ("SELECT a < b IS NULL FROM s", "SELECT (a < b) IS NULL FROM s"),
        ("SELECT a <=> b IS FALSE FROM s", "SELECT (a <=> b) IS FALSE FROM s"),
        ("SELECT a = b IS NULL IS TRUE FROM s", "SELECT (a = b) IS NULL IS TRUE FROM s"),
        ("SELECT a = b IS TRUE = c IS FALSE FROM s", "SELECT ((a = b) IS TRUE = c) IS FALSE FROM s"),
        ("SELECT NOT a = b IS TRUE FROM s", "SELECT NOT (a = b) IS TRUE FROM s"),
        ("SELECT a = (b IS TRUE) FROM s", "SELECT a = (b IS TRUE) FROM s"),
    ],
)
def test_the_tree_is_the_parenthesized_reading(bare, parenthesized):
    expected = canonical_negation(sqlglot.parse_one(parenthesized, read="mysql"))
    assert _read(bare).sql("mysql") == expected.sql("mysql")


@pytest.mark.parametrize(
    "condition",
    ["a = b IS TRUE", "a = b IS FALSE", "a = b IS NULL", "a < b IS NULL", "a <> b IS TRUE OR c", "NOT a = b IS TRUE",
     "a = b IS TRUE = c IS FALSE", "a IS NULL AND b IS NULL OR a = b IS TRUE"],
)
def test_duckdb_runs_the_text_as_the_tree_reads_it(condition):
    # The DuckDB oracle runs sqlglot's printing of the text, which keeps it unparenthesized.
    db = duckdb.connect()
    db.execute("CREATE TABLE s (a BOOLEAN, b BOOLEAN, c BOOLEAN)")
    values = ("TRUE", "FALSE", "NULL")
    db.execute("INSERT INTO s VALUES " + ", ".join(f"({a}, {b}, {c})" for a, b, c in itertools.product(values, repeat=3)))
    text = f"SELECT a, b, c, {condition} FROM s"
    oracle = sqlglot.parse_one(text, read="mysql").sql("duckdb")
    engine, tree = run_unoptimized(db, oracle, _read(text).sql("duckdb"))
    assert sorted(engine, key=repr) == sorted(tree, key=repr)


def test_the_engines_reading_differs_from_sqlglots():
    db = duckdb.connect()
    db.execute("CREATE TABLE s (a INT, b INT, c INT)")
    db.execute("INSERT INTO s VALUES (NULL, 1, 0)")
    engine, sqlglots = run_unoptimized(db, "SELECT * FROM s WHERE a = b IS NULL", "SELECT * FROM s WHERE a = (b IS NULL)")
    assert engine == [(None, 1, 0)] and sqlglots == []


@pytest.mark.parametrize("prove", PROVERS)
def test_calcite_null_safe_equality_is_proved(prove):
    # VeriEQL Calcite 389: Calcite spells IS NOT DISTINCT FROM as this disjunction.
    left = "SELECT * FROM s WHERE a IS NULL AND b IS NULL OR a = b IS TRUE"
    right = "SELECT * FROM s WHERE a IS NOT DISTINCT FROM b"
    assert prove(left, right, schema=SCHEMA, dialect="mysql").proven


@pytest.mark.parametrize("prove", PROVERS)
def test_is_true_on_a_filter_comparison_is_proved(prove):
    assert prove("SELECT a FROM s WHERE a = b IS TRUE", "SELECT a FROM s WHERE a = b", schema=SCHEMA, dialect="mysql").proven


@pytest.mark.parametrize("prove", PROVERS)
@pytest.mark.parametrize(
    "left, right",
    [
        # sqlglot's own reading: (a = b) IS NULL keeps a NULL a, a = (b IS NULL) does not (DuckDB witness above)
        ("SELECT * FROM s WHERE a = b IS NULL", "SELECT * FROM s WHERE a = (b IS NULL)"),
        ("SELECT * FROM s WHERE a = b IS TRUE", "SELECT * FROM s WHERE a = (b IS TRUE)"),
        # in a projection IS TRUE turns a NULL comparison into FALSE
        ("SELECT a = b IS TRUE FROM s", "SELECT a = b FROM s"),
        # IS NULL is not the same filter as the comparison
        ("SELECT a FROM s WHERE a = b IS NULL", "SELECT a FROM s WHERE a = b"),
        # 389 without the IS NULL half: not null-safe equality
        ("SELECT * FROM s WHERE a = b IS TRUE", "SELECT * FROM s WHERE a IS NOT DISTINCT FROM b"),
    ],
)
def test_near_misses_are_not_proved(prove, left, right):
    assert not prove(left, right, schema=SCHEMA, dialect="mysql").proven


@pytest.mark.parametrize("sql", ["SELECT a = b IS NOT TRUE FROM s", "SELECT a = NOT b IS TRUE FROM s", "SELECT a = b IS NOT NULL IS TRUE FROM s"])
def test_is_not_after_a_comparison_is_declined(sql):
    # a = b IS NOT TRUE and a = NOT b IS TRUE parse to one tree; DuckDB reads them differently.
    with pytest.raises(UnmodeledConstruct):
        _read(sql)


@pytest.mark.parametrize("prove", PROVERS)
@pytest.mark.parametrize("right", ["SELECT * FROM s WHERE (a = b) IS NOT TRUE", "SELECT * FROM s WHERE a = NOT (b IS TRUE)"])
def test_is_not_after_a_comparison_proves_nothing(prove, right):
    assert not prove("SELECT * FROM s WHERE a = b IS NOT TRUE", right, schema=SCHEMA, dialect="mysql").proven
