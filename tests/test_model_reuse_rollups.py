"""Model reuse for FLOOR(x TO unit) rollups, USING-join stars, identity casts, a global aggregate over a model grouped by
a filtered key, and a model HAVING that a stricter query HAVING implies (the Calcite materialized-view misses)."""

from decimal import Decimal

import pytest
from sqlglot import exp
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic  # noqa: E402
from kumosql.comparison_implication import comparison, implied_by_any, implies  # noqa: E402
from kumosql.floor_unit_rules import collapse_nested_floor, floor_from_finer, nests_into  # noqa: E402
from kumosql.model_reuse import rewrite_over_model  # noqa: E402
from kumosql.random_check import Column, Schema, Table, find_difference  # noqa: E402
from kumosql.using_star_order import using_star_order  # noqa: E402

EVENTS = Schema([Table("events", [Column("id", "int", True), Column("ts", "date")])])
HR = Schema(
    [
        Table("emps", [Column("empid", "int", True), Column("deptno", "int", True), Column("name", "text"), Column("salary", "float", True)]),
        Table("depts", [Column("deptno", "int", True), Column("name", "text")]),
    ]
)


def _reuse(query, model, schema, types=None):
    return rewrite_over_model(query, model, schema=schema.columns, types=types)


def _no_difference(schema, reuse):
    assert reuse.rewritten, reuse.reason
    assert find_difference(schema, reuse.query_sql, reuse.inlined_sql, mode="bag", trials=60) is None


# ---- nested FLOOR(x TO unit) -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "finer, coarser",
    [("second", "minute"), ("minute", "hour"), ("hour", "day"), ("day", "week"), ("day", "month"), ("month", "year"), ("second", "year"), ("month", "quarter"), ("day", "day")],
)
def test_units_that_nest(finer, coarser):
    assert nests_into(finer, coarser)


@pytest.mark.parametrize("finer, coarser", [("minute", "second"), ("year", "month"), ("week", "month"), ("week", "year"), ("month", "week"), ("century", "year"), ("day", "fortnight")])
def test_units_that_do_not_nest(finer, coarser):
    assert not nests_into(finer, coarser)


def test_nested_floor_collapses_only_when_the_units_nest():
    collapsed = collapse_nested_floor(sqlglot.parse_one("SELECT FLOOR(FLOOR(ts TO second) TO minute) FROM events"))
    assert collapsed.sql() == "SELECT FLOOR(ts TO minute) FROM events"
    kept = collapse_nested_floor(sqlglot.parse_one("SELECT FLOOR(FLOOR(ts TO week) TO month) FROM events"))
    assert kept.sql() == "SELECT FLOOR(FLOOR(ts TO week) TO month) FROM events"
    kept = collapse_nested_floor(sqlglot.parse_one("SELECT FLOOR(FLOOR(ts TO minute) TO second) FROM events"))
    assert kept.sql() == "SELECT FLOOR(FLOOR(ts TO minute) TO second) FROM events"


def test_floor_from_finer_reads_the_finer_column():
    node = sqlglot.parse_one("FLOOR(ts TO month)")
    held = {sqlglot.parse_one("FLOOR(ts TO day)").sql(): exp.column("f", table="mv0")}
    rolled = floor_from_finer(node, lambda e: held.get(e.sql()))
    assert rolled is not None and rolled.sql() == "FLOOR(mv0.f TO month)"
    assert floor_from_finer(sqlglot.parse_one("FLOOR(ts TO day)"), lambda e: held.get(e.sql())) is None  # the model's unit is coarser


def test_prover_does_not_equate_floors_that_do_not_nest():
    schema = {"events": ["id", "ts"]}
    nested = "SELECT FLOOR(FLOOR(ts TO week) TO month) FROM events"
    once = "SELECT FLOOR(ts TO month) FROM events"
    assert not prove_equivalent_algebraic(nested, once, schema=schema, dialect="postgres", compare_names=False).proven
    assert prove_equivalent_algebraic("SELECT FLOOR(FLOOR(ts TO day) TO month) FROM events", once, schema=schema, dialect="postgres", compare_names=False).proven


MODEL_SECOND = "SELECT id, FLOOR(CAST(ts AS TIMESTAMP) TO second) AS s, COUNT(*) AS c, SUM(id) AS total FROM events GROUP BY id, FLOOR(CAST(ts AS TIMESTAMP) TO second)"


@pytest.mark.parametrize("unit", ["minute", "hour", "day", "month", "year"])
def test_view_by_second_answers_a_coarser_floor_group(unit):
    query = f"SELECT FLOOR(CAST(ts AS TIMESTAMP) TO {unit}), SUM(id) AS total FROM events GROUP BY FLOOR(CAST(ts AS TIMESTAMP) TO {unit})"
    reuse = _reuse(query, MODEL_SECOND, EVENTS)
    _no_difference(EVENTS, reuse)
    assert f"FLOOR(mv0.s TO {unit})" in reuse.sql


def test_view_by_day_answers_month_and_year_counts():
    model = "SELECT FLOOR(CAST(ts AS TIMESTAMP) TO day) AS d, COUNT(*) AS c FROM events GROUP BY FLOOR(CAST(ts AS TIMESTAMP) TO day)"
    for unit in ("month", "year"):
        query = f"SELECT FLOOR(CAST(ts AS TIMESTAMP) TO {unit}) AS u, COUNT(*) AS n FROM events GROUP BY FLOOR(CAST(ts AS TIMESTAMP) TO {unit})"
        _no_difference(EVENTS, _reuse(query, model, EVENTS))


def test_view_by_coarse_unit_cannot_answer_a_finer_group():
    model = "SELECT FLOOR(CAST(ts AS TIMESTAMP) TO month) AS m, COUNT(*) AS c FROM events GROUP BY FLOOR(CAST(ts AS TIMESTAMP) TO month)"
    query = "SELECT FLOOR(CAST(ts AS TIMESTAMP) TO day), COUNT(*) FROM events GROUP BY FLOOR(CAST(ts AS TIMESTAMP) TO day)"
    assert not _reuse(query, model, EVENTS).rewritten


def test_week_view_cannot_answer_a_month_group():
    model = "SELECT FLOOR(CAST(ts AS TIMESTAMP) TO week) AS w, COUNT(*) AS c FROM events GROUP BY FLOOR(CAST(ts AS TIMESTAMP) TO week)"
    query = "SELECT FLOOR(CAST(ts AS TIMESTAMP) TO month), COUNT(*) FROM events GROUP BY FLOOR(CAST(ts AS TIMESTAMP) TO month)"
    assert not _reuse(query, model, EVENTS).rewritten


# ---- SELECT * over USING ----------------------------------------------------------------------------------------


def test_star_over_using_lists_the_merged_column_first():
    tree = using_star_order(sqlglot.parse_one("SELECT * FROM emps JOIN depts USING (deptno)"), HR.columns)
    assert [i.alias_or_name for i in tree.expressions] == ["deptno", "empid", "name", "salary", "name"]


def test_star_without_using_is_left_alone():
    sql = "SELECT * FROM emps JOIN depts ON emps.deptno = depts.deptno"
    assert using_star_order(sqlglot.parse_one(sql), HR.columns).sql() == sqlglot.parse_one(sql).sql()


def test_model_of_one_table_answers_a_using_star_join():
    model = "SELECT deptno, empid, name, salary FROM emps"
    reuse = _reuse("SELECT * FROM emps JOIN depts USING (deptno)", model, HR)
    _no_difference(HR, reuse)
    assert "depts" in reuse.sql


def test_the_random_check_reads_a_using_star_as_the_prover_does():
    star = "SELECT * FROM emps JOIN depts USING (deptno)"
    explicit = "SELECT emps.deptno, emps.empid, emps.name, emps.salary, depts.name FROM emps JOIN depts ON emps.deptno = depts.deptno"
    reordered = "SELECT emps.empid, emps.deptno, emps.name, emps.salary, depts.name FROM emps JOIN depts ON emps.deptno = depts.deptno"
    assert find_difference(HR, star, explicit, mode="bag", trials=40) is None
    assert find_difference(HR, star, reordered, mode="bag", trials=40) is not None


# ---- identity casts ----------------------------------------------------------------------------------------------


def test_cast_column_answers_a_filter_on_the_column_when_the_type_fits():
    model = "SELECT CAST(empid AS BIGINT) AS e, name FROM emps"
    query = "SELECT empid AS deptno FROM emps WHERE empid = 1"
    types = {"emps": {"empid": "int", "deptno": "int", "name": "text", "salary": "float"}, "depts": {"deptno": "int", "name": "text"}}
    reuse = _reuse(query, model, HR, types=types)
    _no_difference(HR, reuse)
    assert "WHERE" in reuse.sql.upper()


def test_cast_column_is_not_read_as_the_column_without_types():
    model = "SELECT CAST(empid AS BIGINT) AS e, name FROM emps"
    assert not _reuse("SELECT empid FROM emps WHERE empid = 1", model, HR).rewritten


def test_text_cast_to_a_number_is_not_the_column():
    model = "SELECT CAST(name AS BIGINT) AS e FROM emps"
    types = {"emps": {"empid": "int", "deptno": "int", "name": "text", "salary": "float"}, "depts": {"deptno": "int", "name": "text"}}
    assert not _reuse("SELECT name FROM emps WHERE name = 1", model, HR, types=types).rewritten


# ---- a global aggregate over a model grouped by a key the query fixes ----------------------------------------------


def test_constant_filter_in_a_global_aggregate_reads_one_group():
    model = "SELECT name, COUNT(DISTINCT deptno) AS cnt, SUM(salary) AS s, MAX(salary) AS m FROM emps GROUP BY name"
    for aggregate, column in (("COUNT(DISTINCT deptno)", "cnt"), ("SUM(salary)", "s"), ("MAX(salary)", "m")):
        reuse = _reuse(f"SELECT {aggregate} AS v FROM emps WHERE name = 'a'", model, HR)
        _no_difference(HR, reuse)
        assert f"mv0.{column}" in reuse.sql
    assert "COALESCE" in _reuse("SELECT COUNT(DISTINCT deptno) AS v FROM emps WHERE name = 'a'", model, HR).sql  # a count over no group is 0


def test_global_aggregate_is_not_read_from_a_model_with_a_free_key():
    model = "SELECT name, deptno, COUNT(DISTINCT salary) AS cnt FROM emps GROUP BY name, deptno"
    assert not _reuse("SELECT COUNT(DISTINCT salary) FROM emps WHERE name = 'a'", model, HR).rewritten


def test_global_aggregate_without_the_filter_is_not_read_from_one_group():
    model = "SELECT name, COUNT(DISTINCT deptno) AS cnt FROM emps GROUP BY name"
    assert not _reuse("SELECT COUNT(DISTINCT deptno) FROM emps", model, HR).rewritten


def test_prover_collapses_a_global_aggregate_over_a_fixed_key_group():
    schema = {"emps": ["empid", "deptno", "name", "salary"]}
    grouped = "SELECT COALESCE(SUM(c), 0) AS v FROM (SELECT name, COUNT(salary) AS c FROM emps WHERE name = 'a' GROUP BY name) AS d"
    assert prove_equivalent_algebraic(grouped, "SELECT COUNT(salary) AS v FROM emps WHERE name = 'a'", schema=schema, dialect="postgres", compare_names=False).proven
    # a bare SUM of a COUNT is NULL over no row where COUNT is 0
    bare = "SELECT SUM(c) AS v FROM (SELECT name, COUNT(salary) AS c FROM emps WHERE name = 'a' GROUP BY name) AS d"
    assert not prove_equivalent_algebraic(bare, "SELECT COUNT(salary) AS v FROM emps WHERE name = 'a'", schema=schema, dialect="postgres", compare_names=False).proven
    # the rule itself reads only a filter that fixes every key
    from kumosql.fixed_key_regroup import collapse_fixed_key_regroup

    def collapsed(sql):
        return collapse_fixed_key_regroup(sqlglot.parse_one(sql, read="postgres"))

    assert collapsed(grouped) is not None
    assert collapsed("SELECT SUM(c) AS v FROM (SELECT name, SUM(salary) AS c FROM emps WHERE salary > 1 GROUP BY name) AS d") is None
    assert collapsed("SELECT SUM(c) AS v FROM (SELECT name, deptno, SUM(salary) AS c FROM emps WHERE name = 'a' GROUP BY name, deptno) AS d") is None
    assert collapsed("SELECT SUM(c) AS v FROM (SELECT name, SUM(salary) AS c FROM emps WHERE name = NULL GROUP BY name) AS d") is None


def test_prover_regroups_by_an_expression_of_the_keys():
    schema = {"events": ["id", "ts"]}
    layered = "SELECT FLOOR(f TO minute) AS m, SUM(s) AS t FROM (SELECT id, FLOOR(ts TO second) AS f, SUM(id) AS s FROM events GROUP BY id, FLOOR(ts TO second)) AS d GROUP BY FLOOR(f TO minute)"
    flat = "SELECT FLOOR(ts TO minute) AS m, SUM(id) AS t FROM events GROUP BY FLOOR(ts TO minute)"
    assert prove_equivalent_algebraic(layered, flat, schema=schema, dialect="postgres", compare_names=False).proven
    # a different key expression is a different grouping
    other = "SELECT FLOOR(ts TO hour) AS m, SUM(id) AS t FROM events GROUP BY FLOOR(ts TO hour)"
    assert not prove_equivalent_algebraic(layered, other, schema=schema, dialect="postgres", compare_names=False).proven
    # AVG cannot be rolled up from partial averages
    avg = "SELECT FLOOR(f TO minute) AS m, AVG(s) AS t FROM (SELECT id, FLOOR(ts TO second) AS f, AVG(id) AS s FROM events GROUP BY id, FLOOR(ts TO second)) AS d GROUP BY FLOOR(f TO minute)"
    assert not prove_equivalent_algebraic(avg, "SELECT FLOOR(ts TO minute) AS m, AVG(id) AS t FROM events GROUP BY FLOOR(ts TO minute)", schema=schema, dialect="postgres", compare_names=False).proven


# ---- a model HAVING implied by the query's ------------------------------------------------------------------------


def _cmp(sql):
    return comparison(sqlglot.parse_one(sql), lambda node: node.sql())


@pytest.mark.parametrize(
    "strong, weak, expected",
    [
        ("x > 20", "x > 10", True),
        ("x > 10", "x > 10", True),
        ("x >= 10", "x > 10", False),
        ("x > 10", "x >= 10", True),
        ("x >= 11", "x > 10", True),
        ("x = 20", "x >= 20", True),
        ("x = 5", "x > 10", False),
        ("x < 5", "x < 10", True),
        ("x < 15", "x < 10", False),
        ("x > 20", "x < 30", False),
        ("x > 20", "y > 10", False),
        ("20 < x", "x > 10", True),
    ],
)
def test_comparison_implication(strong, weak, expected):
    assert implies(_cmp(strong), _cmp(weak)) is expected


def test_comparison_reads_numbers_only():
    assert _cmp("x > 'a'") is None
    assert _cmp("x > y") is None
    assert _cmp("x > 1.5")[2] == Decimal("1.5")


def test_implied_having_needs_every_model_conjunct_covered():
    key = lambda node: node.sql()  # noqa: E731
    model = [sqlglot.parse_one("SUM(s) > 10"), sqlglot.parse_one("COUNT(*) > 1")]
    assert not implied_by_any(model, [sqlglot.parse_one("SUM(s) > 20")], key)
    assert implied_by_any(model, [sqlglot.parse_one("SUM(s) > 20"), sqlglot.parse_one("COUNT(*) > 1")], key)


MODEL_HAVING = "SELECT deptno, SUM(salary) AS total FROM emps WHERE deptno >= 10 GROUP BY deptno HAVING SUM(salary) > 10"


def test_stricter_having_reads_a_model_having():
    reuse = _reuse("SELECT deptno, SUM(salary) AS total FROM emps WHERE deptno >= 20 GROUP BY deptno HAVING SUM(salary) > 20", MODEL_HAVING, HR)
    _no_difference(HR, reuse)
    assert "mv0.total > 20" in reuse.sql


def test_laxer_having_cannot_read_a_model_having():
    assert not _reuse("SELECT deptno, SUM(salary) AS total FROM emps WHERE deptno >= 20 GROUP BY deptno HAVING SUM(salary) > 5", MODEL_HAVING, HR).rewritten
    assert not _reuse("SELECT deptno, SUM(salary) AS total FROM emps WHERE deptno >= 20 GROUP BY deptno", MODEL_HAVING, HR).rewritten
