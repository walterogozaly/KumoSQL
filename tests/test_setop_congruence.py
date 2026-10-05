"""Reading a model that is UNION ALL, INTERSECT ALL or EXCEPT ALL, and proving set operations equal
branch by branch (``kumosql.setop_congruence`` and ``kumosql.setop_views``).

Every rewrite is also run against the original on a small database that has repeated rows and NULLs, and
every trap is shown to differ on that database where a plain-looking replacement exists."""

import duckdb
import pytest

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.model_reuse import rewrite_over_model  # noqa: E402
from kumosql.setop_congruence import prove_by_congruence  # noqa: E402

SCHEMA = {"emps": ["empid", "deptno", "name"], "depts": ["deptno", "name"], "deps": ["empid", "name"]}
SETUP = [
    "CREATE TABLE emps (empid INT, deptno INT, name TEXT)",
    "CREATE TABLE depts (deptno INT, name TEXT)",
    "CREATE TABLE deps (empid INT, name TEXT)",
    # repeated rows, NULLs, and rows that only some tables have
    "INSERT INTO emps VALUES (1, 10, 'Bill'), (1, 10, 'Bill'), (1, 10, 'Bill'), (2, 20, 'Eric'), (3, NULL, NULL), (4, NULL, NULL), (5, 10, 'Sebastian'), (6, 30, NULL)",
    "INSERT INTO depts VALUES (10, 'Bill'), (10, 'Bill'), (20, 'Eric'), (20, 'Eric'), (NULL, NULL), (40, 'Sales'), (30, NULL)",
    "INSERT INTO deps VALUES (1, 'Bill'), (2, 'Eric'), (2, 'Eric'), (3, NULL), (4, NULL), (4, NULL), (5, 'Bill')",
]


def _rows(sql):
    con = duckdb.connect()
    for statement in SETUP:
        con.execute(statement)
    return sorted(map(repr, con.execute(sql).fetchall()))


def _prove(a, b, **kwargs):
    return prove_equivalent_algebraic(a, b, schema=SCHEMA, compare_names=False, dialect="postgres", **kwargs)


def _congruent(a, b, types=None):
    return prove_by_congruence(a, b, _prove, types=types, dialect="postgres")


def _inline(replacement, model, name="mv0"):
    return replacement.replace(f"FROM {name}", f"FROM ({model}) AS {name}")


def _reuse(query, model):
    return rewrite_over_model(query, model, schema=SCHEMA, timeout_ms=8000)


def _agrees(query, model, reuse):
    assert reuse.status == "rewritten", reuse.reason
    assert _rows(query) == _rows(reuse.inlined_sql)


# ---- the prover alone declines INTERSECT ALL and EXCEPT ALL ---------------------------------------


def test_prover_alone_declines_a_filter_through_except_all():
    left = "SELECT name FROM emps WHERE name = 'Bill' EXCEPT ALL SELECT name FROM deps"
    right = "SELECT t.name FROM (SELECT name FROM emps EXCEPT ALL SELECT name FROM deps) AS t WHERE t.name = 'Bill'"
    assert not _prove(left, right).proven


# ---- congruence --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "left, right",
    [
        # same operator, operands equal one by one
        ("SELECT name FROM emps WHERE deptno = 10 EXCEPT ALL SELECT name FROM deps", "SELECT name FROM emps WHERE 10 = deptno EXCEPT ALL SELECT name FROM deps"),
        ("SELECT name FROM emps INTERSECT ALL SELECT name FROM deps", "SELECT name FROM deps INTERSECT ALL SELECT name FROM emps"),
        # a filter on the left operand of an EXCEPT ALL only needs the right one where the left one has rows
        (
            "SELECT name FROM emps WHERE name = 'Bill' EXCEPT ALL SELECT name FROM deps",
            "SELECT t.name FROM (SELECT name FROM emps EXCEPT ALL SELECT name FROM deps) AS t WHERE t.name = 'Bill'",
        ),
        # INTERSECT ALL: filtering either operand filters the result
        (
            "SELECT name FROM emps INTERSECT ALL SELECT name FROM deps WHERE name = 'Bill'",
            "SELECT t.name FROM (SELECT name FROM emps INTERSECT ALL SELECT name FROM deps) AS t WHERE t.name = 'Bill'",
        ),
        # NULLs are equal to each other in a set operation, so a filter on NULL moves through it
        (
            "SELECT name FROM emps WHERE name IS NULL EXCEPT ALL SELECT name FROM deps",
            "SELECT t.name FROM (SELECT name FROM emps EXCEPT ALL SELECT name FROM deps) AS t WHERE t.name IS NULL",
        ),
        # an OR moves as one conjunct, and the right operand is filtered by it too
        (
            "SELECT name FROM emps WHERE name = 'Bill' OR name IS NULL EXCEPT ALL SELECT name FROM deps",
            "SELECT t.name FROM (SELECT name FROM emps EXCEPT ALL SELECT name FROM deps) AS t WHERE t.name = 'Bill' OR t.name IS NULL",
        ),
        # a reordering of the columns of the whole set operation
        (
            "SELECT name, deptno FROM depts INTERSECT ALL SELECT name, deptno FROM emps",
            "SELECT t.name, t.deptno FROM (SELECT deptno, name FROM emps INTERSECT ALL SELECT deptno, name FROM depts) AS t",
        ),
    ],
)
def test_congruent_pairs_are_proven_and_return_the_same_rows(left, right):
    assert _rows(left) == _rows(right)
    result = _congruent(left, right)
    assert result is not None and result.proven


@pytest.mark.parametrize(
    "left, right",
    [
        # EXCEPT ALL is not commutative: the operands must stay in order
        ("SELECT name FROM deps EXCEPT ALL SELECT name FROM emps", "SELECT name FROM emps EXCEPT ALL SELECT name FROM deps"),
        # the ALL form keeps duplicates that the plain form drops
        ("SELECT name FROM emps INTERSECT ALL SELECT name FROM deps", "SELECT name FROM emps INTERSECT SELECT name FROM deps"),
        ("SELECT name FROM emps EXCEPT ALL SELECT name FROM deps", "SELECT name FROM emps EXCEPT SELECT name FROM deps"),
        # a filter on the subtracted operand only is not a filter of the result
        (
            "SELECT name FROM emps EXCEPT ALL SELECT name FROM deps WHERE name = 'Bill'",
            "SELECT t.name FROM (SELECT name FROM emps EXCEPT ALL SELECT name FROM deps) AS t WHERE t.name = 'Bill'",
        ),
        # a join over the derived set operation changes how often its rows appear
        (
            "SELECT name FROM emps INTERSECT ALL SELECT name FROM deps",
            "SELECT t.name FROM (SELECT name FROM emps INTERSECT ALL SELECT name FROM deps) AS t JOIN depts d ON d.name = t.name",
        ),
        # a filter that is not a function of the row's own values
        (
            "SELECT name FROM emps WHERE name = 'Bill' EXCEPT ALL SELECT name FROM deps",
            "SELECT t.name FROM (SELECT name FROM emps EXCEPT ALL SELECT name FROM deps) AS t WHERE t.name = 'Bill' AND t.name IN (SELECT name FROM depts)",
        ),
        # another operator
        ("SELECT name FROM emps INTERSECT ALL SELECT name FROM deps", "SELECT name FROM emps EXCEPT ALL SELECT name FROM deps"),
        # a different second operand
        ("SELECT name FROM emps INTERSECT ALL SELECT name FROM deps", "SELECT name FROM emps INTERSECT ALL SELECT name FROM depts"),
        # the same filter written on the other column
        (
            "SELECT name, deptno FROM emps WHERE name = 'Bill' EXCEPT ALL SELECT name, deptno FROM depts",
            "SELECT t.name, t.deptno FROM (SELECT name, deptno FROM emps EXCEPT ALL SELECT name, deptno FROM depts) AS t WHERE t.deptno = 10",
        ),
    ],
)
def test_set_operations_that_differ_are_not_proven(left, right):
    assert _congruent(left, right) is None


def test_mixed_branch_types_are_declined():
    types = {"emps": {"empid": "int", "deptno": "int", "name": "text"}, "deps": {"empid": "int", "name": "text"}}
    left = "SELECT empid FROM emps INTERSECT ALL SELECT name FROM deps"
    right = "SELECT empid FROM emps INTERSECT ALL SELECT name FROM deps"
    assert _congruent(left, right, types=types) is None


def test_congruence_ignores_queries_without_set_operations():
    assert _congruent("SELECT name FROM emps", "SELECT name FROM emps") is None


# ---- a model that is a set operation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "query, model",
    [
        ("SELECT name FROM emps WHERE name = 'Bill' EXCEPT ALL SELECT name FROM deps", "SELECT name FROM emps EXCEPT ALL SELECT name FROM deps"),
        ("SELECT name FROM emps WHERE name IS NULL EXCEPT ALL SELECT name FROM deps", "SELECT name FROM emps EXCEPT ALL SELECT name FROM deps"),
        ("SELECT name FROM emps WHERE name > 'B' INTERSECT ALL SELECT name FROM deps WHERE name > 'B'", "SELECT name FROM emps INTERSECT ALL SELECT name FROM deps"),
        ("SELECT name FROM emps INTERSECT ALL SELECT name FROM deps WHERE name = 'Bill'", "SELECT name FROM emps INTERSECT ALL SELECT name FROM deps"),
        ("SELECT name FROM deps INTERSECT ALL SELECT name FROM emps", "SELECT name FROM emps INTERSECT ALL SELECT name FROM deps"),
        ("SELECT empid FROM emps WHERE deptno = 10", "SELECT empid, deptno FROM emps WHERE deptno = 10 UNION ALL SELECT empid, deptno FROM emps WHERE deptno = 20"),
        ("SELECT DISTINCT deptno FROM emps UNION SELECT deptno FROM depts", "SELECT deptno FROM emps UNION ALL SELECT deptno FROM depts"),
        ("SELECT deptno FROM emps UNION SELECT deptno FROM depts", "SELECT deptno FROM emps UNION ALL SELECT deptno FROM depts"),
        (
            "SELECT name, deptno FROM depts INTERSECT ALL SELECT name, deptno FROM emps",
            "SELECT t.name, t.deptno FROM (SELECT deptno, name FROM emps INTERSECT ALL SELECT deptno, name FROM depts) AS t",
        ),
    ],
)
def test_set_operation_models_answer_the_query(query, model):
    reuse = _reuse(query, model)
    _agrees(query, model, reuse)
    assert "mv0" in reuse.sql


@pytest.mark.parametrize(
    "query, model, naive",
    [
        # the left operand of EXCEPT ALL loses the rows the right operand holds
        ("SELECT name FROM emps", "SELECT name FROM emps EXCEPT ALL SELECT name FROM deps", "SELECT name FROM mv0"),
        # the other order of the operands
        ("SELECT name FROM deps EXCEPT ALL SELECT name FROM emps", "SELECT name FROM emps EXCEPT ALL SELECT name FROM deps", "SELECT name FROM mv0"),
        # a filter on the subtracted side only
        ("SELECT name FROM emps EXCEPT ALL SELECT name FROM deps WHERE name = 'Bill'", "SELECT name FROM emps EXCEPT ALL SELECT name FROM deps", "SELECT name FROM mv0 WHERE name = 'Bill'"),
        # UNION ALL keeps the duplicates that UNION removed
        ("SELECT deptno FROM emps UNION ALL SELECT deptno FROM depts", "SELECT deptno FROM emps UNION SELECT deptno FROM depts", "SELECT deptno FROM mv0"),
        # INTERSECT ALL keeps copies that INTERSECT collapsed
        ("SELECT name FROM emps INTERSECT ALL SELECT name FROM deps", "SELECT name FROM emps INTERSECT SELECT name FROM deps", "SELECT name FROM mv0"),
        # the view holds only two of the ranges the query reads
        ("SELECT empid FROM emps", "SELECT empid, deptno FROM emps WHERE deptno = 10 UNION ALL SELECT empid, deptno FROM emps WHERE deptno = 20", "SELECT empid FROM mv0"),
        # without DISTINCT the union keeps repeated rows
        ("SELECT deptno FROM emps UNION SELECT deptno FROM depts", "SELECT deptno FROM emps UNION ALL SELECT deptno FROM depts", "SELECT deptno FROM mv0"),
    ],
)
def test_traps_stay_unrewritten(query, model, naive):
    # the naive replacement really differs on the sample database
    assert _rows(query) != _rows(_inline(naive, model))
    reuse = _reuse(query, model)
    if reuse.status == "rewritten":
        # a rewrite is allowed only when it is right (for example by reading the whole model again)
        assert _rows(query) == _rows(reuse.inlined_sql)
        assert reuse.sql != naive
    else:
        assert reuse.status in ("no_rewrite", "unsupported")


def test_expected_traps_have_no_rewrite_at_all():
    for query, model in [
        ("SELECT name FROM emps", "SELECT name FROM emps EXCEPT ALL SELECT name FROM deps"),
        ("SELECT name FROM deps EXCEPT ALL SELECT name FROM emps", "SELECT name FROM emps EXCEPT ALL SELECT name FROM deps"),
        ("SELECT deptno FROM emps UNION ALL SELECT deptno FROM depts", "SELECT deptno FROM emps UNION SELECT deptno FROM depts"),
    ]:
        assert _reuse(query, model).status != "rewritten"
