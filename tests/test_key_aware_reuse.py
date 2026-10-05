"""Key-aware model reuse: joins that declared keys make lossless are left out of a match, a
LEFT JOIN onto a unique key nothing reads is ignored, and aggregate models joined to extra tables are
rolled up (``kumosql.key_aware``). Every rewrite is also run on a small database that respects the keys."""

import duckdb
import pytest

pytest.importorskip("z3")

from kumosql.key_aware import drop_unread_left_joins  # noqa: E402
from kumosql.model_reuse import rewrite_over_model  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

SCHEMA = {
    "emps": ["empid", "deptno", "name", "salary", "boss"],
    "depts": ["deptno", "name", "budget"],
    "dependents": ["empid", "name"],
}
# emps.deptno references depts(deptno); emps.boss is a nullable reference to the same key
KEYED = {
    "emps": TableConstraints(
        not_null=frozenset({"empid", "deptno"}),
        keys=(("empid",),),
        foreign_keys=((("deptno",), "depts", ("deptno",)), (("boss",), "depts", ("deptno",))),
    ),
    "depts": TableConstraints(not_null=frozenset({"deptno"}), keys=(("deptno",),)),
}
PLAIN = {"emps": TableConstraints(not_null=frozenset({"empid", "deptno"})), "depts": TableConstraints(not_null=frozenset({"deptno"}))}
DATA = [
    "CREATE TABLE depts AS SELECT * FROM (VALUES (1, 'a', 10), (2, 'b', 20), (3, 'c', NULL)) t(deptno, name, budget)",
    "CREATE TABLE emps AS SELECT * FROM (VALUES (1, 1, 'x', 5.0, 2), (2, 1, 'y', 6.0, NULL), (3, 2, 'z', 7.0, 3), (4, 3, 'w', NULL, 1)) t(empid, deptno, name, salary, boss)",
    "CREATE TABLE dependents AS SELECT * FROM (VALUES (1, 'p'), (1, 'q'), (3, 'r')) t(empid, name)",
]


def _rows(sql):
    con = duckdb.connect()
    for statement in DATA:
        con.execute(statement)
    return sorted(map(repr, con.execute(sql).fetchall()))


def _reuse(query, model, constraints=KEYED):
    return rewrite_over_model(query, model, schema=SCHEMA, constraints=constraints)


def _assert_rewritten(reuse, query):
    assert reuse.rewritten, reuse.reason
    assert _rows(reuse.inlined_sql) == _rows(query)


def test_parent_join_of_the_model_is_left_out():
    model = "SELECT e.empid, e.name, d.name AS dname FROM emps e JOIN depts d ON e.deptno = d.deptno"
    query = "SELECT empid, name FROM emps WHERE salary > 5"
    reuse = _reuse(query, model)
    # salary is not a model column, so this needs a model that exposes it
    assert not reuse.rewritten
    model = "SELECT e.empid, e.name, e.salary, d.name AS dname FROM emps e JOIN depts d ON e.deptno = d.deptno"
    reuse = _reuse(query, model)
    _assert_rewritten(reuse, query)
    assert "depts" not in reuse.sql and "JOIN" not in reuse.sql


def test_parent_join_of_the_query_is_left_out_of_an_aggregate_rollup():
    model = "SELECT dp.empid, e.deptno, SUM(e.salary) AS s FROM emps e JOIN dependents dp ON e.empid = dp.empid GROUP BY dp.empid, e.deptno"
    query = (
        "SELECT dp.empid, SUM(e.salary) AS s FROM emps e JOIN depts d ON e.deptno = d.deptno "
        "JOIN dependents dp ON e.empid = dp.empid GROUP BY dp.empid"
    )
    reuse = _reuse(query, model)
    _assert_rewritten(reuse, query)
    assert "depts" not in reuse.sql


def test_nullable_foreign_key_costs_an_is_not_null_filter():
    model = "SELECT e.empid, e.name, d.name AS dname FROM emps e JOIN depts d ON e.boss = d.deptno"
    # employees without a boss are missing from the model
    assert not _reuse("SELECT empid, name FROM emps", model).rewritten
    query = "SELECT empid, name FROM emps WHERE boss IS NOT NULL"
    _assert_rewritten(_reuse(query, model), query)


@pytest.mark.parametrize(
    "model, query",
    [
        # the parent is filtered: employees of other departments are missing
        ("SELECT e.empid, e.name FROM emps e JOIN depts d ON e.deptno = d.deptno WHERE d.name = 'a'", "SELECT empid, name FROM emps"),
        # the joined column is not a key of the parent
        ("SELECT e.empid, e.name FROM emps e JOIN depts d ON e.name = d.name", "SELECT empid, name FROM emps"),
        # a join onto a table the foreign key does not cover repeats and drops rows
        ("SELECT e.empid, e.name FROM emps e JOIN dependents d ON e.empid = d.empid", "SELECT empid, name FROM emps"),
    ],
)
def test_joins_that_are_not_lossless_are_kept(model, query):
    assert not _reuse(query, model).rewritten


def test_no_declared_foreign_key_means_no_lossless_join():
    model = "SELECT e.empid, e.name, d.name AS dname FROM emps e JOIN depts d ON e.deptno = d.deptno"
    assert not _reuse("SELECT empid, name FROM emps", model, constraints=PLAIN).rewritten


def test_left_join_onto_a_unique_key_is_ignored():
    model = "SELECT e.empid, e.name AS n FROM emps e LEFT JOIN depts d ON e.deptno = d.deptno"
    query = "SELECT empid FROM emps"
    _assert_rewritten(_reuse(query, model), query)
    query = "SELECT e.empid FROM emps e LEFT JOIN depts d ON e.deptno = d.deptno WHERE e.salary > 5"
    _assert_rewritten(_reuse(query, "SELECT empid, salary FROM emps"), query)


def test_left_join_that_can_repeat_or_is_read_is_kept():
    # dependents(empid) is not a key: an employee with two dependents appears twice
    model = "SELECT e.empid FROM emps e LEFT JOIN dependents d ON e.empid = d.empid"
    assert not _reuse("SELECT empid FROM emps", model).rewritten
    # the null-supplying side is read by the select
    tree = _tree("SELECT e.empid, d.name FROM emps e LEFT JOIN depts d ON e.deptno = d.deptno")
    assert drop_unread_left_joins(tree, KEYED) is tree


def _tree(sql):
    from kumosql.model_reuse import _prepare

    return _prepare(sql, SCHEMA, "postgres")


def test_unread_left_joins_are_dropped_in_a_chain_and_only_when_unread():
    tree = _tree("SELECT e.empid FROM emps e LEFT JOIN depts d ON e.deptno = d.deptno AND d.name = 'a'")
    assert not drop_unread_left_joins(tree, KEYED).args.get("joins")
    # a condition on the parent alone does not fix its key
    tree = _tree("SELECT e.empid FROM emps e LEFT JOIN depts d ON d.name = e.name")
    assert drop_unread_left_joins(tree, KEYED) is tree
    # no declared key, nothing to rely on
    tree = _tree("SELECT e.empid FROM emps e LEFT JOIN depts d ON e.deptno = d.deptno")
    assert drop_unread_left_joins(tree, PLAIN) is tree
    # the join is read by a later condition
    tree = _tree("SELECT e.empid FROM emps e LEFT JOIN depts d ON e.deptno = d.deptno JOIN dependents p ON p.name = d.name")
    assert drop_unread_left_joins(tree, KEYED) is tree


def test_aggregate_model_joined_to_an_extra_table_is_rolled_up():
    # no keys declared: the extra table repeats and is read, so it stays in the replacement
    model = "SELECT deptno, SUM(salary) AS s, COUNT(*) AS n FROM emps GROUP BY deptno"
    query = "SELECT d.name, SUM(e.salary) AS s FROM emps e JOIN depts d ON e.deptno = d.deptno GROUP BY d.name"
    reuse = _reuse(query, model, constraints=PLAIN)
    _assert_rewritten(reuse, query)
    assert "depts" in reuse.sql


def test_sum_over_a_column_of_the_extra_table_is_weighted_by_the_model_count():
    model = "SELECT deptno, COUNT(*) AS n FROM emps GROUP BY deptno"
    query = "SELECT e.deptno, SUM(d.budget) AS b FROM emps e JOIN depts d ON e.deptno = d.deptno GROUP BY e.deptno"
    reuse = _reuse(query, model, constraints=PLAIN)
    _assert_rewritten(reuse, query)
    assert "budget * mv0.n" in reuse.sql


def test_sum_over_the_extra_table_without_a_count_is_not_rewritten():
    model = "SELECT deptno, SUM(salary) AS s FROM emps GROUP BY deptno"
    query = "SELECT e.deptno, SUM(d.budget) AS b FROM emps e JOIN depts d ON e.deptno = d.deptno GROUP BY e.deptno"
    assert not _reuse(query, model, constraints=PLAIN).rewritten


def test_extra_table_joined_on_a_column_the_model_grouped_away_is_not_rewritten():
    model = "SELECT deptno, SUM(salary) AS s, COUNT(*) AS n FROM emps GROUP BY deptno"
    query = "SELECT d.name, SUM(e.salary) AS s FROM emps e JOIN depts d ON e.name = d.name GROUP BY d.name"
    assert not _reuse(query, model, constraints=PLAIN).rewritten


def _proven(a, b):
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    result = prove_equivalent_algebraic(a, b, schema=SCHEMA, constraints=KEYED, compare_names=False, dialect="postgres")
    return result.proven


def test_prover_reads_a_nullable_foreign_key_join_as_an_is_not_null_filter():
    join = "SELECT e.empid FROM emps e JOIN depts d ON e.boss = d.deptno"
    assert _proven(join, "SELECT empid FROM emps WHERE boss IS NOT NULL")
    assert not _proven(join, "SELECT empid FROM emps")
    # a parent that is read keeps the join
    assert not _proven("SELECT e.empid, d.name FROM emps e JOIN depts d ON e.boss = d.deptno", "SELECT empid, name FROM emps WHERE boss IS NOT NULL")
