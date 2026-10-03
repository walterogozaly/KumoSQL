"""EXISTS, IN, NOT IN and outer joins: proofs must agree with execution.

Every pair is run on random DuckDB databases (NULLs, duplicates, empty tables).
A proved pair must never differ; the expectation column pins what is provable.
"""

import random
from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.smt_equivalence import SmtStatus, TableConstraints, prove_equivalent_smt
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import insert_rows

SCHEMA = {"t": ["id", "a"], "u": ["id", "b"]}
STRICT = {
    "t": TableConstraints(not_null=frozenset({"id", "a"}), keys=(("id",),)),
    "u": TableConstraints(not_null=frozenset({"id", "b"}), keys=(("id",),)),
}

# (left, right, proved without constraints, proved with NOT NULL + keys)
CASES = [
    pytest.param(
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.id = t.id)",
        "SELECT a FROM t WHERE EXISTS (SELECT 2 FROM u AS v WHERE t.id = v.id)",
        True, True, id="exists-alias-and-select-list",
    ),
    pytest.param(
        "SELECT a FROM t WHERE t.id IN (SELECT id FROM u)",
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.id = t.id)",
        True, True, id="in-is-exists",
    ),
    pytest.param(
        "SELECT a FROM t WHERE t.id NOT IN (SELECT id FROM u)",
        "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.id = t.id)",
        False, True, id="not-in-needs-not-null",
    ),
    pytest.param(
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.id = t.id)",
        "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.id = t.id AND u.b > 1)",
        False, False, id="extra-filter-in-subquery",
    ),
    pytest.param(
        "SELECT t.a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.id = t.id)",
        "SELECT t.a FROM t JOIN u ON u.id = t.id",
        False, True, id="semijoin-equals-join-only-with-a-key",
    ),
    pytest.param(
        "SELECT t.a FROM t WHERE t.id NOT IN (SELECT id FROM u)",
        "SELECT t.a FROM t",
        False, False, id="not-in-filters",
    ),
    pytest.param(
        "SELECT t.a, u.b FROM t LEFT JOIN u ON t.id = u.id",
        "SELECT t.a, u.b FROM u RIGHT JOIN t ON t.id = u.id",
        True, True, id="left-is-flipped-right",
    ),
    pytest.param(
        "SELECT t.a, u.b FROM t FULL JOIN u ON t.id = u.id",
        "SELECT t.a, u.b FROM u FULL JOIN t ON u.id = t.id",
        True, True, id="full-join-commutes",
    ),
    pytest.param(
        "SELECT t.a FROM t LEFT JOIN u ON t.id = u.id WHERE u.id IS NULL",
        "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.id = t.id)",
        True, True, id="left-join-is-null-is-anti-join",
    ),
    pytest.param(
        "SELECT t.a, u.b FROM t LEFT JOIN u ON t.id = u.id",
        "SELECT t.a, u.b FROM t JOIN u ON t.id = u.id",
        False, False, id="left-join-is-not-inner-join",
    ),
    pytest.param(
        "SELECT t.a, u.b FROM t LEFT JOIN u ON t.id = u.id WHERE u.b > 0",
        "SELECT t.a, u.b FROM t JOIN u ON t.id = u.id WHERE u.b > 0",
        True, True, id="filter-on-right-side-makes-left-join-inner",
    ),
    pytest.param(
        "SELECT t.a, u.b FROM t LEFT JOIN u ON t.id = u.id AND u.b > 0",
        "SELECT t.a, u.b FROM t LEFT JOIN u ON t.id = u.id WHERE u.b > 0 OR u.id IS NULL",
        False, False, id="on-versus-where-filter",
    ),
    pytest.param(
        "SELECT id FROM t INTERSECT DISTINCT SELECT id FROM u",
        "SELECT DISTINCT t.id FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.id = t.id) OR (t.id IS NULL AND EXISTS (SELECT 1 FROM u WHERE u.id IS NULL))",
        False, True, id="intersect-treats-nulls-as-equal",
    ),
    pytest.param(
        "SELECT id FROM t INTERSECT DISTINCT SELECT id FROM u",
        "SELECT id FROM u INTERSECT DISTINCT SELECT id FROM t",
        True, True, id="intersect-commutes",
    ),
    pytest.param(
        "SELECT id FROM t EXCEPT DISTINCT SELECT id FROM u",
        "SELECT id FROM u EXCEPT DISTINCT SELECT id FROM t",
        False, False, id="except-does-not-commute",
    ),
    pytest.param(
        "SELECT id FROM t EXCEPT DISTINCT SELECT id FROM u",
        "SELECT DISTINCT id FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.id = t.id)",
        False, True, id="except-is-anti-join-on-non-null-keys",
    ),
    pytest.param(
        "SELECT id FROM t INTERSECT DISTINCT SELECT id FROM u",
        "SELECT DISTINCT id FROM t WHERE id IN (SELECT id FROM u)",
        False, True, id="intersect-is-semi-join-on-non-null-keys",
    ),
    pytest.param(
        "SELECT id FROM t INTERSECT DISTINCT SELECT id FROM t",
        "SELECT DISTINCT id FROM t",
        True, True, id="intersect-with-itself",
    ),
    pytest.param(
        "SELECT t.a FROM t WHERE t.id IN (SELECT id FROM u) OR t.a = 1",
        "SELECT a FROM t WHERE t.a = 1 OR EXISTS (SELECT 1 FROM u WHERE u.id = t.id)",
        True, True, id="exists-inside-or",
    ),
]


_DB = None


def _database(rng, constrained):
    global _DB
    if _DB is None:
        _DB = duckdb.connect(":memory:")
        _DB.execute("CREATE TABLE t (id BIGINT, a BIGINT)")
        _DB.execute("CREATE TABLE u (id BIGINT, b BIGINT)")
    for table in ("t", "u"):
        _DB.execute(f"DELETE FROM {table}")
        used, rows = set(), []
        for _ in range(rng.choice([0, 1, 2, 3, 4])):
            row = [rng.choice([0, 1, 2, 3]), rng.choice([0, 1, 2, 3])]
            if constrained:
                if row[0] in used:
                    continue
                used.add(row[0])
            else:
                row = [None if rng.random() < 0.25 else v for v in row]
            rows.append(row)
        insert_rows(_DB, table, rows)
    return _DB


def _differs(left, right, constrained):
    left, right = (sqlglot.transpile(q, read="bigquery", write="duckdb")[0] for q in (left, right))
    rng = random.Random(5)
    for _ in range(120):
        db = _database(rng, constrained)
        if Counter(db.execute(left).fetchall()) != Counter(db.execute(right).fetchall()):
            return True
    return False


@pytest.mark.parametrize("left,right,loose,strict", CASES)
def test_proofs_match_expectations_and_execution(left, right, loose, strict):
    for constraints, expected in ((None, loose), (STRICT, strict)):
        result = prove_equivalent_smt(left, right, schema=SCHEMA, constraints=constraints, compare_names=False)
        assert result.proven == expected, (constraints is not None, result.reason)
        if result.proven:
            assert not _differs(left, right, constraints is not None), "proved but results differ"
        if result.status is SmtStatus.NOT_EQUIVALENT:
            assert _differs(left, right, constraints is not None)


def test_having_on_group_keys_equals_where():
    from kumosql.smt_equivalence import prove_equivalent_smt

    schema = {"dept": ["deptno", "name"]}
    having = "SELECT name, COUNT(*) FROM dept GROUP BY name HAVING name = 'a'"
    where = "SELECT name, COUNT(*) FROM dept WHERE name = 'a' GROUP BY name"
    assert prove_equivalent_smt(having, where, schema=schema).proven
    # A filter on an aggregate cannot move below the grouping.
    count = "SELECT name, COUNT(*) FROM dept GROUP BY name HAVING COUNT(*) > 1"
    assert not prove_equivalent_smt(count, where, schema=schema).proven


SEMI_SCHEMA = {"dept": ["deptno", "name"], "emp": ["empno", "deptno", "sal"]}


def _semi(left, right):
    from kumosql.smt_equivalence import prove_equivalent_smt

    return prove_equivalent_smt(left, right, schema=SEMI_SCHEMA, compare_names=False).proven


def test_join_to_distinct_source_is_an_existence_test():
    joined = "SELECT d.name FROM dept d JOIN (SELECT deptno FROM emp WHERE sal > 1 GROUP BY deptno) t ON d.deptno = t.deptno"
    exists = "SELECT d.name FROM dept d WHERE EXISTS (SELECT 1 FROM emp e WHERE e.sal > 1 AND e.deptno = d.deptno)"
    distinct = "SELECT d.name FROM dept d JOIN (SELECT DISTINCT deptno FROM emp WHERE sal > 1) t ON d.deptno = t.deptno"
    assert _semi(joined, exists)
    assert _semi(distinct, exists)


def test_join_to_non_distinct_or_partly_joined_source_is_not_an_existence_test():
    exists = "SELECT d.name FROM dept d WHERE EXISTS (SELECT 1 FROM emp e WHERE e.deptno = d.deptno)"
    plain = "SELECT d.name FROM dept d JOIN (SELECT deptno FROM emp) t ON d.deptno = t.deptno"
    two_columns = (
        "SELECT d.name FROM dept d JOIN (SELECT DISTINCT deptno, sal FROM emp) t ON d.deptno = t.deptno"
    )
    assert not _semi(plain, exists)
    assert not _semi(two_columns, exists)


def test_unread_derived_columns_do_not_matter():
    schema = {"emp": ["empno", "deptno", "sal"]}
    with_count = (
        "SELECT 1 FROM (SELECT deptno, COUNT(*) AS c FROM emp WHERE deptno > 7 GROUP BY deptno) t "
        "JOIN emp e ON t.deptno = e.deptno"
    )
    without = (
        "SELECT 1 FROM (SELECT deptno FROM emp WHERE deptno > 7 GROUP BY deptno) t JOIN "
        "(SELECT * FROM emp WHERE deptno > 7) e ON t.deptno = e.deptno"
    )
    assert prove_equivalent_algebraic(with_count, without, schema=schema).proven
    # A column that is read cannot be dropped.
    used = with_count.replace("SELECT 1 FROM", "SELECT t.c FROM")
    assert not prove_equivalent_algebraic(used, without.replace("SELECT 1 FROM", "SELECT 1 FROM"), schema=schema).proven
