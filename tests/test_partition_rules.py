"""TLP-style partition recombination (src/kumosql/partition_rules.py)."""

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.partition_rules import recombine_partitions  # noqa: E402
from kumosql.smt_equivalence import SmtStatus  # noqa: E402

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"]}


def _prove(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False).status


def _rule(sql):
    return recombine_partitions(sqlglot.parse_one(sql, read="bigquery")).sql(dialect="bigquery")


def _tlp(query, p, third="({p}) IS NULL"):
    return " UNION ALL ".join(f"{query} WHERE {q}" for q in (f"({p})", f"NOT ({p})", third.format(p=p)))


def test_three_partitions_of_a_filter_are_the_unfiltered_query():
    q = "SELECT x.a AS k0, y.b AS k1 FROM t AS x LEFT JOIN u AS y ON x.a = y.a"
    p = "(x.b > 0 AND y.a <> 0) OR x.c IN (1, 2)"
    assert _rule(_tlp(q, p)) == q
    assert _prove(q, _tlp(q, p)) is SmtStatus.PROVEN_EQUIVALENT
    assert _prove(q, _tlp(q, p, "({p}) IS UNKNOWN")) is SmtStatus.PROVEN_EQUIVALENT
    assert _prove(q, _tlp(q, p, "NOT (({p}) IS NOT NULL)")) is SmtStatus.PROVEN_EQUIVALENT


def test_true_and_false_partitions_miss_the_null_rows():
    q = "SELECT a AS k FROM t"
    two = f"{q} WHERE b > 0 UNION ALL {q} WHERE NOT (b > 0)"
    assert _rule(two) == f"{q} WHERE (b > 0) OR (NOT (b > 0))"
    assert _prove(q, two) is SmtStatus.NOT_EQUIVALENT
    # IS TRUE / IS NOT TRUE also partition every row.
    assert _prove(q, f"{q} WHERE (b > 0) IS TRUE UNION ALL {q} WHERE (b > 0) IS NOT TRUE") is SmtStatus.PROVEN_EQUIVALENT


def test_overlapping_filters_stay_separate_branches():
    q = "SELECT a AS k FROM t"
    overlapping = f"{q} WHERE b > 0 UNION ALL {q} WHERE b > 0 OR c = 1"
    assert _rule(overlapping).count("UNION ALL") == 1
    assert _prove(f"{q} WHERE b > 0 OR c = 1", overlapping) is not SmtStatus.PROVEN_EQUIVALENT
    # A duplicated partition keeps its extra copy.
    dup = _tlp(q, "b > 0") + f" UNION ALL {q} WHERE (b > 0)"
    assert _rule(dup) == f"{q} UNION ALL {q} WHERE (b > 0)"
    assert _prove(q, dup) is not SmtStatus.PROVEN_EQUIVALENT


def test_a_null_guard_simplified_away_in_one_branch_still_partitions():
    # The normalizer drops `NOT a IS NULL` from the WHERE of the first branch (b < a already implies it);
    # the merge must know that b < a is NULL exactly when a or b is.
    q = "SELECT a AS k0, c AS k1 FROM t"
    p = "((b < a) AND (NOT a IS NULL)) AND ((a >= 3) OR (a = 3))"
    assert _prove(q, _tlp(q, p)) is SmtStatus.PROVEN_EQUIVALENT


def test_branches_that_differ_outside_the_filter_or_cannot_move_it_are_left_alone():
    for sql in (
        "SELECT a AS k FROM t WHERE b > 0 UNION ALL SELECT a AS k FROM u WHERE NOT (b > 0)",
        "SELECT a AS k FROM t WHERE b > 0 UNION ALL SELECT b AS k FROM t WHERE NOT (b > 0)",
        "SELECT a AS k FROM t WHERE RAND() < 0.5 UNION ALL SELECT a AS k FROM t WHERE NOT (RAND() < 0.5)",
        "SELECT COUNT(*) AS k FROM t WHERE b > 0 UNION ALL SELECT COUNT(*) AS k FROM t WHERE NOT (b > 0)",
        "SELECT DISTINCT a AS k FROM t WHERE b > 0 UNION ALL SELECT DISTINCT a AS k FROM t WHERE NOT (b > 0)",
        "(SELECT a AS k FROM t WHERE b > 0 LIMIT 1) UNION ALL (SELECT a AS k FROM t WHERE NOT (b > 0) LIMIT 1)",
        "SELECT a AS k FROM t WHERE b IN (SELECT a FROM u) UNION ALL SELECT a AS k FROM t WHERE NOT (b IN (SELECT a FROM u))",
        "SELECT a AS k FROM t WHERE b > 0 UNION DISTINCT SELECT a AS k FROM t WHERE NOT (b > 0)",
    ):
        assert _rule(sql) == sqlglot.parse_one(sql, read="bigquery").sql(dialect="bigquery"), sql


def test_partitions_inside_a_larger_union_merge_and_keep_the_rest():
    q = "SELECT a AS k FROM t"
    sql = f"SELECT b AS k FROM u UNION ALL {_tlp(q, 'c = 1')} UNION ALL SELECT c AS k FROM u"
    assert _rule(sql) == f"SELECT b AS k FROM u UNION ALL {q} UNION ALL SELECT c AS k FROM u"


@pytest.mark.parametrize(
    ("inner", "outer"),
    [("COUNT(*)", "SUM"), ("COUNT(c)", "SUM"), ("SUM(c)", "SUM"), ("MIN(c)", "MIN"), ("MAX(c)", "MAX")],
)
def test_reaggregating_partition_aggregates_is_the_aggregate(inner, outer):
    source = "t AS x JOIN u AS y ON x.a = y.a"
    parts = " UNION ALL ".join(f"SELECT {inner.replace('c)', 'x.c)')} AS v FROM {source} WHERE {q}" for q in ("(y.b < 2)", "NOT (y.b < 2)", "(y.b < 2) IS NULL"))
    left = f"SELECT {inner.replace('c)', 'x.c)')} AS v FROM {source}"
    assert _prove(left, f"SELECT {outer}(v) AS v FROM ({parts})") is SmtStatus.PROVEN_EQUIVALENT


def test_reaggregation_of_partitions_reads_the_unsplit_query():
    sql = (
        "SELECT SUM(v) AS n, MAX(m) AS hi FROM (SELECT COUNT(*) AS v, MAX(a) AS m FROM t WHERE b = 1 "
        "UNION ALL SELECT COUNT(*) AS v, MAX(a) AS m FROM t WHERE NOT (b = 1) UNION ALL SELECT COUNT(*) AS v, MAX(a) AS m FROM t WHERE (b = 1) IS NULL) AS d"
    )
    assert _rule(sql) == "SELECT COUNT(d._p0) AS n, MAX(d._p1) AS hi FROM (SELECT 1 AS _p0, a AS _p1 FROM t) AS d"


def test_reaggregations_that_do_not_recombine_are_left_alone():
    for sql in (
        # Partials of different queries stay split: that is the form the aggregate rules normalize to.
        "SELECT SUM(v) AS n, MAX(m) AS hi FROM (SELECT COUNT(*) AS v, MAX(a) AS m FROM t WHERE b = 1 UNION ALL SELECT COUNT(*) AS v, MAX(c) AS m FROM u) AS d",
        "SELECT SUM(v) AS v FROM (SELECT COUNT(*) AS v FROM t WHERE b = 1 UNION ALL SELECT COUNT(*) AS v FROM t WHERE b = 2)",
        "SELECT AVG(v) AS v FROM (SELECT AVG(a) AS v FROM t UNION ALL SELECT AVG(a) AS v FROM u)",
        "SELECT MAX(v) AS v FROM (SELECT COUNT(*) AS v FROM t UNION ALL SELECT COUNT(*) AS v FROM u)",
        "SELECT SUM(v) AS v FROM (SELECT MAX(a) AS v FROM t UNION ALL SELECT MAX(a) AS v FROM u)",
        "SELECT SUM(DISTINCT v) AS v FROM (SELECT COUNT(*) AS v FROM t UNION ALL SELECT COUNT(*) AS v FROM u)",
        "SELECT SUM(v) AS v FROM (SELECT COUNT(DISTINCT a) AS v FROM t UNION ALL SELECT COUNT(DISTINCT a) AS v FROM u)",
        "SELECT SUM(v) AS v FROM (SELECT COUNT(*) AS v FROM t GROUP BY a UNION ALL SELECT COUNT(*) AS v FROM u)",
        "SELECT SUM(v) AS v FROM (SELECT COUNT(*) AS v FROM t UNION DISTINCT SELECT COUNT(*) AS v FROM u)",
        "SELECT SUM(v) AS v FROM (SELECT COUNT(*) AS v FROM t UNION ALL SELECT COUNT(*) AS v FROM u) WHERE v > 1",
        "SELECT SUM(v) + 1 AS v FROM (SELECT COUNT(*) AS v FROM t UNION ALL SELECT COUNT(*) AS v FROM u)",
    ):
        assert _rule(sql) == sqlglot.parse_one(sql, read="bigquery").sql(dialect="bigquery"), sql


def test_a_lost_partition_is_never_proved():
    q = "SELECT a AS k FROM t"
    left = "SELECT COUNT(*) AS v FROM t"
    parts = " UNION ALL ".join(f"SELECT COUNT(*) AS v FROM t WHERE {w}" for w in ("(b = 1)", "NOT (b = 1)"))
    assert _prove(left, f"SELECT SUM(v) AS v FROM ({parts})") is not SmtStatus.PROVEN_EQUIVALENT
    assert _prove(q, f"{q} WHERE (b = 1) UNION ALL {q} WHERE (b = 1) IS NULL") is not SmtStatus.PROVEN_EQUIVALENT


PRESERVED = [
    _tlp("SELECT x.a AS k0, y.b AS k1 FROM t AS x LEFT JOIN u AS y ON x.a = y.a", "x.b > y.c OR y.a IS NULL"),
    "SELECT a AS k FROM t WHERE b = 1 UNION ALL SELECT a AS k FROM t WHERE c > 0 UNION ALL SELECT a AS k FROM t WHERE NOT (b = 1)",
    "SELECT a AS k FROM t WHERE b > 0 UNION ALL SELECT a AS k FROM t WHERE NOT (b > 0) UNION ALL SELECT a AS k FROM t WHERE NOT ((b > 0) IS TRUE)",
    "SELECT a AS k FROM t WHERE b IS NULL UNION ALL SELECT a AS k FROM t WHERE b < c UNION ALL SELECT a AS k FROM t WHERE b >= c",
    "SELECT SUM(v) AS n, MIN(m) AS lo FROM (SELECT COUNT(c) AS v, MIN(a) AS m FROM t WHERE b = 1 UNION ALL SELECT COUNT(c) AS v, MIN(a) AS m FROM t WHERE b <> 1 OR b IS NULL) AS d",
    "SELECT SUM(v) AS v FROM (SELECT SUM(x.c) AS v FROM t AS x JOIN u AS y ON x.a = y.b WHERE x.b < 2 UNION ALL SELECT SUM(x.c) AS v FROM t AS x JOIN u AS y ON x.a = y.b WHERE NOT (x.b < 2) OR x.b IS NULL)",
]


@pytest.mark.parametrize("sql", PRESERVED)
def test_normalization_preserves_results_on_random_databases(sql):
    import random
    from collections import Counter

    duckdb = pytest.importorskip("duckdb")
    from kumosql.algebraic_equivalence import normalize

    rewritten = normalize(sql, schema=SCHEMA)
    assert rewritten != sqlglot.parse_one(sql, read="bigquery").sql(dialect="bigquery")
    from kumosql.duckdb_load import insert_rows

    rng = random.Random(5)
    db = duckdb.connect()  # one connection, new tables each trial
    left, right = (sqlglot.transpile(q, read="bigquery", write="duckdb")[0] for q in (sql, rewritten))
    for _ in range(60):
        for table in ("t", "u"):
            db.execute(f"CREATE OR REPLACE TABLE {table} (a INT, b INT, c INT)")
            insert_rows(db, table, [[rng.choice([None, 0, 1, 2, 3]) for _ in range(3)] for _ in range(rng.choice([0, 1, 3, 6]))])
        run = lambda q: Counter(db.execute(q).fetchall())  # noqa: E731
        assert run(left) == run(right), rewritten
