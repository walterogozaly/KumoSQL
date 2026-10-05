"""Shapes the model-reuse proposer and the containment checker read since the failing-tests triage
(cluster 15): literal IN lists, set operations, grouped derived tables, models the proposer does not
read, distinct aggregates and constant keys at the model's grain, and empty grouping sets."""

import duckdb
import pytest

pytest.importorskip("z3")

from kumosql.containment import check_containment  # noqa: E402
from kumosql.model_reuse import rewrite_over_model  # noqa: E402

ORDERS = {"orders": ["id", "amount", "region"]}
EMPS = {"emps": ["empid", "deptno", "name", "salary", "commission"], "depts": ["deptno", "name"]}


@pytest.mark.parametrize("semantics", ["set", "bag"])
@pytest.mark.parametrize(
    "q1, q2",
    [
        ("SELECT id FROM orders WHERE region IN (1, 2) AND amount > 5", "SELECT id FROM orders WHERE region IN (1, 2, 3)"),
        ("SELECT id FROM orders WHERE amount > 10", "SELECT id FROM orders WHERE amount > 10 UNION ALL SELECT id FROM orders WHERE region = 1"),
        (
            "SELECT id FROM orders WHERE amount > 10 AND region = 1 UNION ALL SELECT id FROM orders WHERE region = 2",
            "SELECT id FROM orders WHERE amount > 5 UNION ALL SELECT id FROM orders WHERE region IN (1, 2)",
        ),
        ("SELECT id FROM orders UNION SELECT id FROM orders", "SELECT id FROM orders UNION ALL SELECT id FROM orders"),
        ("SELECT id FROM orders WHERE amount > 10 EXCEPT SELECT id FROM orders WHERE region = 1", "SELECT id FROM orders WHERE amount > 10"),
        ("SELECT id FROM orders WHERE amount > 10 INTERSECT SELECT id FROM orders WHERE region = 1", "SELECT id FROM orders WHERE region = 1"),
    ],
)
def test_contained_through_in_lists_and_set_operations(q1, q2, semantics):
    result = check_containment(q1, q2, schema=ORDERS, semantics=semantics)
    assert result.contained, result.reason


@pytest.mark.parametrize(
    "q1, q2",
    [
        # each branch fits the single right branch, but together they can exceed it
        ("SELECT id FROM orders UNION ALL SELECT id FROM orders", "SELECT id FROM orders"),
        # two left branches cannot share one right branch
        ("SELECT id FROM orders WHERE region = 1 UNION ALL SELECT id FROM orders WHERE region = 1", "SELECT id FROM orders WHERE region = 1 UNION ALL SELECT id FROM orders WHERE region = 2"),
        ("SELECT id FROM orders WHERE region NOT IN (1, 2)", "SELECT id FROM orders WHERE region IN (3, 4)"),
    ],
)
def test_not_proven_bag_contained_when_it_is_not(q1, q2):
    assert not check_containment(q1, q2, schema=ORDERS, semantics="bag").contained


def _same_rows(query, reuse, setup):
    con = duckdb.connect()
    for statement in setup:
        con.execute(statement)
    return sorted(map(repr, con.execute(query).fetchall())) == sorted(map(repr, con.execute(reuse.inlined_sql).fetchall()))


def test_filter_over_grouped_derived_table_reads_the_model():
    model = 'SELECT * FROM (SELECT deptno, SUM(salary) AS sum_salary, SUM(commission) FROM emps GROUP BY deptno) WHERE sum_salary > 10'
    query = 'SELECT * FROM (SELECT deptno, SUM(salary) AS sum_salary FROM emps WHERE deptno >= 20 GROUP BY deptno) WHERE sum_salary > 10'
    reuse = rewrite_over_model(query, model, schema=EMPS)
    assert reuse.rewritten, reuse.reason
    assert "WHERE" in reuse.sql.upper() and "SUM(" not in reuse.sql.upper()


def test_projection_of_a_model_the_proposer_does_not_read():
    model = """SELECT * FROM (SELECT deptno, SUM(salary), SUM(commission) FROM emps GROUP BY deptno) a
               JOIN (SELECT deptno, COUNT(name) FROM depts GROUP BY deptno) b ON a.deptno = b.deptno"""
    query = """SELECT * FROM (SELECT deptno, SUM(salary) FROM emps GROUP BY deptno) a
               JOIN (SELECT deptno FROM depts GROUP BY deptno) b ON a.deptno = b.deptno"""
    reuse = rewrite_over_model(query, model, schema=EMPS)
    assert reuse.rewritten and reuse.strategy == "projection", reuse.reason


def test_projection_does_not_mix_up_derived_tables():
    """Both derived tables have a column deptno; the query's b.deptno must not read a.deptno."""

    model = """SELECT * FROM (SELECT deptno, SUM(salary) FROM emps GROUP BY deptno) a
               JOIN (SELECT deptno, COUNT(name) FROM depts GROUP BY deptno) b ON a.deptno <= b.deptno"""
    query = """SELECT b.deptno FROM (SELECT deptno, SUM(salary) FROM emps GROUP BY deptno) a
               JOIN (SELECT deptno, COUNT(name) FROM depts GROUP BY deptno) b ON a.deptno <= b.deptno"""
    reuse = rewrite_over_model(query, model, schema=EMPS)
    assert reuse.rewritten, reuse.reason
    setup = [
        "CREATE TABLE emps (empid INT, deptno INT, name TEXT, salary INT, commission INT)",
        "CREATE TABLE depts (deptno INT, name TEXT)",
        "INSERT INTO emps VALUES (1, 1, 'a', 5, 1)",
        "INSERT INTO depts VALUES (1, 'x'), (2, 'y')",
    ]
    assert _same_rows(query, reuse, setup)


@pytest.mark.parametrize(
    "model, query",
    [
        ("SELECT * FROM emps WHERE empid < 300 UNION SELECT * FROM emps WHERE empid > 200", "SELECT * FROM emps WHERE empid > 200 UNION SELECT * FROM emps WHERE empid < 300"),
        ("SELECT deptno FROM emps INTERSECT SELECT deptno FROM depts", "SELECT deptno FROM depts INTERSECT SELECT deptno FROM emps"),
    ],
)
def test_set_operation_model_answers_the_same_query(model, query):
    reuse = rewrite_over_model(query, model, schema=EMPS)
    assert reuse.rewritten and reuse.strategy == "same-as-model", reuse.reason


def test_set_operation_model_reports_unsupported_when_not_proven():
    model = "SELECT deptno FROM emps UNION SELECT deptno FROM depts"
    reuse = rewrite_over_model("SELECT deptno FROM emps", model, schema=EMPS)
    assert reuse.status == "unsupported"


def test_distinct_aggregate_at_the_model_grain():
    looked_up = rewrite_over_model(
        "SELECT deptno, COUNT(DISTINCT salary) AS s FROM emps GROUP BY name, deptno",
        "SELECT name, deptno, COUNT(DISTINCT salary) AS s FROM emps GROUP BY name, deptno",
        schema=EMPS,
    )
    assert looked_up.rewritten and "DISTINCT" not in looked_up.sql.upper(), looked_up.reason
    regrouped = rewrite_over_model("SELECT deptno, name, COUNT(DISTINCT name) FROM emps GROUP BY deptno, name", "SELECT deptno, name FROM emps GROUP BY deptno, name", schema=EMPS)
    assert regrouped.rewritten and "GROUP BY" in regrouped.sql.upper(), regrouped.reason


def test_constant_key_makes_the_grains_equal():
    model = "SELECT name, deptno, COUNT(DISTINCT commission) AS cnt FROM emps GROUP BY name, deptno"
    reuse = rewrite_over_model("SELECT deptno, COUNT(DISTINCT commission) AS cnt FROM emps WHERE name = 'hello' GROUP BY deptno", model, schema=EMPS)
    assert reuse.rewritten and reuse.strategy == "aggregate-same-grain", reuse.reason


def test_global_distinct_count_is_not_read_from_a_single_group():
    """On no rows the query returns one row (0) and the model none, so this stays unrewritten."""

    model = "SELECT name, COUNT(DISTINCT deptno) AS cnt FROM emps GROUP BY name"
    assert not rewrite_over_model("SELECT COUNT(DISTINCT deptno) FROM emps WHERE name = 'hello'", model, schema=EMPS).rewritten


@pytest.mark.parametrize("grouping", ["CUBE (empid, deptno)", "ROLLUP (deptno, empid)", "GROUPING SETS ((deptno), ())"])
def test_count_rollup_over_an_empty_grouping_set_keeps_zero(grouping):
    model = "SELECT empid, deptno, COUNT(*) AS c FROM emps GROUP BY empid, deptno"
    query = f"SELECT COUNT(*) + 1 AS c, deptno FROM emps GROUP BY {grouping}"
    reuse = rewrite_over_model(query, model, schema=EMPS)
    assert reuse.rewritten, reuse.reason
    assert "COALESCE" in reuse.sql.upper()
    setup = ["CREATE TABLE emps (empid INT, deptno INT, name TEXT, salary INT, commission INT)"]
    assert _same_rows(query, reuse, setup)  # empty table: the grand total is 1, not NULL


def test_whole_view_with_wrong_output_width_does_not_reach_prover(monkeypatch):
    import kumosql.algebraic_equivalence as ae
    import kumosql.model_reuse as mr
    original = ae.prove_equivalent_algebraic
    pairs = []
    def recording(left, right, **kwargs):
        pairs.append((left, right))
        return original(left, right, **kwargs)
    monkeypatch.setattr(ae, "prove_equivalent_algebraic", recording)
    result = mr.rewrite_over_model("SELECT x FROM t", "SELECT x, y FROM t",
                                   schema={"t": ["x", "y"]})
    assert result.rewritten
    assert len(pairs) == 1
    assert result.candidates_tried == 1


def test_whole_view_star_keeps_equivalent_candidate():
    from kumosql.model_reuse import rewrite_over_model
    result = rewrite_over_model("SELECT t.* FROM t", "SELECT x, y FROM t",
                                schema={"t": ["x", "y"]})
    assert result.rewritten
    assert result.strategy == "same-as-model"
