"""Bounded equivalence (z3, at most N rows per table): verdicts, evidence level, and the encoding against DuckDB."""

import random

import pytest
import sqlglot

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

from kumosql.ast_utils import spell_for_duckdb  # noqa: E402
from kumosql import bounded_equivalence as be  # noqa: E402
from kumosql.bounded_equivalence import BColumn, BoundedSchema, BoundedStatus, BTable, check_bounded  # noqa: E402


def schema(**overrides):
    t = BTable("t", [BColumn("a", "INT64"), BColumn("b", "INT64")])
    u = BTable("u", [BColumn("a", "INT64", True), BColumn("c", "STRING")])
    for table in (t, u):
        for column in table.columns:
            column.not_null = overrides.get(f"{table.name}.{column.name}", column.not_null)
    e = BTable("e", [BColumn("id", "INT64"), BColumn("d", "DATE")])
    return BoundedSchema({"t": t, "u": u, "e": e})


def check(left, right, rows=3, **kwargs):
    return check_bounded(left, right, schema(), rows=rows, dialect="mysql", **kwargs)


def test_equal_queries_are_bounded_equivalent_not_proven():
    result = check("select a from t where a > 1", "select a from t where a >= 2")
    assert result.status is BoundedStatus.BOUNDED_EQUIVALENT
    assert result.label == "bounded, 3 rows"
    assert result.bound == 3
    assert any("bounded" in a for a in result.assumptions)


def test_counterexample_is_replayed_and_smallest():
    result = check("select a from t where a > 1", "select a from t where a >= 1")
    assert result.status is BoundedStatus.DIFFERENT
    assert result.bound == 1
    (row,) = result.counterexample["t"]
    assert row[0] == 1


def test_null_handling_is_part_of_the_search():
    # NOT IN differs from an anti-join as soon as the subquery can hold a NULL
    result = check("select a from t where a not in (select a from t)", "select a from t where false")
    assert result.status is BoundedStatus.BOUNDED_EQUIVALENT
    result = check(
        "select a from t where b not in (select a from t)",
        "select a from t where not exists (select 1 from t x where x.a = t.b)",
    )
    assert result.status is BoundedStatus.DIFFERENT
    assert any(None in row for rows in result.counterexample.values() for row in rows)


def test_bound_matters_a_difference_that_needs_more_rows():
    # three distinct values exist only with three rows
    left = "select 1 from t having count(distinct a) < 3"
    right = "select 1 from t having count(distinct a) < 4"
    assert check(left, right, rows=2).bounded_equivalent
    result = check(left, right, rows=3)
    assert result.status is BoundedStatus.DIFFERENT and result.bound == 3


def test_not_null_and_key_constraints_are_respected():
    keyed = BTable("t", [BColumn("a", "INT64", True), BColumn("b", "INT64")], keys=[("a",)])
    s = BoundedSchema({"t": keyed})
    same = "select a, count(*) from t group by a"
    one = "select a, 1 from t"
    assert check_bounded(same, one, s, rows=3, dialect="mysql").bounded_equivalent
    assert check(same, one).status is BoundedStatus.DIFFERENT  # without the key they differ


def test_foreign_key_gives_every_child_a_parent():
    parent = BTable("p", [BColumn("id", "INT64", True)], keys=[("id",)])
    child = BTable("c", [BColumn("pid", "INT64", True)], foreign_keys=[(("pid",), "p", ("id",))])
    s = BoundedSchema({"p": parent, "c": child})
    result = check_bounded("select pid from c", "select c.pid from c join p on p.id = c.pid", s, rows=3, dialect="mysql")
    assert result.bounded_equivalent


def test_unsupported_sql_is_unknown_never_a_verdict():
    result = check("select a from t", "select upper(c) from u")
    assert result.status is BoundedStatus.UNKNOWN
    assert "unsupported" in result.reason or "differ" in result.reason


def test_a_tie_does_not_produce_a_counterexample():
    # ORDER BY b LIMIT 1 is undefined on ties: the bound covers only databases without ties
    result = check("select a from t order by b limit 1", "select a from t order by b, a limit 1")
    assert result.bounded_equivalent


def test_limit_and_order_are_modeled():
    result = check("select a from t order by a limit 1", "select min(a) from t")
    assert result.status is BoundedStatus.DIFFERENT  # no row vs a NULL row on an empty table
    assert check("select a from t order by a desc limit 1", "select max(a) from t where a is not null having count(*) > 0").status is not None


def test_replay_decides_a_model_the_encoding_cannot_confirm():
    class Never:
        def differ(self, data):
            return False

    result = check("select a from t", "select b from t", replay=Never())
    assert result.status is BoundedStatus.UNKNOWN
    assert "did not confirm" in result.reason


# --- the encoding against DuckDB ---------------------------------------------------------------

QUERIES = [
    "select a, b from t where a > 1 or b is null",
    "select a + b, a * 2, b - a from t",
    "select a / b from t",
    "select case when a > 1 then 'x' when a is null then 'n' else 'y' end from t",
    "select coalesce(a, b, 0), nullif(a, b) from t",
    "select a, count(*), sum(b), min(b), max(b), avg(b), count(distinct b) from t group by a",
    "select count(*), sum(a), min(a), max(a), count(b) from t",
    "select a from t group by a having count(*) > 1",
    "select distinct a, b from t",
    "select t.a, u.c from t join u on t.a = u.a",
    "select t.a, u.c from t left join u on t.a = u.a",
    "select t.a, u.c from t right join u on t.a = u.a",
    "select t.a, u.c from t full join u on t.a = u.a",
    "select * from t join u using (a)",
    "select * from t natural join u",
    "select a from t where a in (select a from u)",
    "select a from t where b not in (select a from u)",
    "select a from t where exists (select 1 from u where u.a = t.a)",
    "select a, (select count(*) from u where u.a = t.a) from t",
    "select a from t union select a from u",
    "select a from t union all select a from u",
    "select a from t intersect select a from u",
    "select a from t except select a from u",
    "select a from t where b between 1 and 3",
    "select a from t where a in (1, 2, null)",
    "select a from t where not (a = 1)",
    "select a, b from t order by a desc, b limit 2 offset 1",
    "select x.a, count(*) from (select a, b from t where b > 0) x group by x.a",
    "with w as (select a, b from t where a > 0) select w.a, count(w.b) from w group by w.a",
    "select c, length(c) from u where c like 'a%'",
    "select round(a / 3, 1), abs(b - a) from t",
    "select id, year(d), month(d), day(d), quarter(d) from e",
    "select id, extract(year from d), d + interval 3 day, datediff(d, '2020-02-27') from e",
    "select id from e where d between '2020-02-27' and '2020-03-02'",
    "select t.a, x.m from t left join lateral (select max(u.a) as m from u where u.a = t.a) as x on true",
    "select t.a, x.c from t cross join lateral (select u.c from u where u.a = t.a) as x",
    "select count(distinct a, b) from t",
    "select substring(c, 2, 2), substring(c, 2) from u",
    "select a from t left semi join u on t.a = u.a",
    "select a from t left anti join u on t.a = u.a",
    "select a from t where a = any (select a from u)",
    "select a from t where a > all (select a from u)",
    "select a, row_number() over (partition by b order by a), rank() over (order by b), sum(a) over (partition by b) from t",
]


def concrete(rng):
    return {
        "t": [(rng.choice([None, 1, 2, 3, 4]), rng.choice([None, 0, 1, 3])) for _ in range(rng.randint(0, 4))],
        "u": [(rng.choice([1, 2, 3, 5]), rng.choice([None, "a", "ab", "b"])) for _ in range(rng.randint(0, 3))],
        "e": [(rng.randint(1, 3), rng.choice([None, "2020-02-28", "2020-02-29", "2020-03-01", "2019-12-31", "2021-01-01", "2024-02-29"])) for _ in range(rng.randint(0, 3))],
    }


def duck_rows(db, data, sql):
    for name in ("t", "u", "e"):
        db.execute(f"delete from {name}")
        for row in data[name]:
            db.execute(f"insert into {name} values (?, ?)", row)
    return db.execute(sql).fetchall()


def norm(rows):
    def cell(value):
        if hasattr(value, "hour") and hasattr(value, "date") and not (value.hour or value.minute or value.second):
            return value.date()  # DuckDB gives a TIMESTAMP for DATE + INTERVAL
        if hasattr(value, "is_finite") or isinstance(value, float):
            return round(float(value), 6)
        return value

    return sorted((tuple(cell(v) for v in row) for row in rows), key=repr)


@pytest.mark.parametrize("sql", QUERIES)
def test_encoding_matches_duckdb(sql):
    s = schema()
    db = duckdb.connect()
    db.execute("create table t(a bigint, b bigint)")
    db.execute("create table u(a bigint not null, c varchar)")
    db.execute("create table e(id bigint, d date)")
    rng = random.Random(abs(hash(sql)) % 1000 or 1)
    compared = 0
    for _ in range(14):
        data = concrete(rng)
        try:
            mine = be.evaluate(sql, s, data, "mysql")
        except be.Unsupported as error:
            if "side condition" in str(error) or "ungrouped" in str(error):  # an answer the engines leave arbitrary
                continue
            raise
        theirs = duck_rows(db, data, spell_for_duckdb(sqlglot.parse_one(sql, read="mysql")).sql(dialect="duckdb"))
        assert norm(mine) == norm(theirs), (sql, data)
        compared += 1
    assert compared >= 3


def test_an_ungrouped_column_is_an_arbitrary_pick_not_the_first_row():
    # b is not determined by a: the engines may return any b of the group, so the queries are not equivalent
    result = check("select a, b from t group by a", "select a, min(b) from t group by a")
    assert not result.bounded_equivalent or result.bound < 3  # at most the bound below the first difference
    # with a as a key, b is determined and the pick cannot matter
    keyed = BTable("t", [BColumn("a", "INT64", True), BColumn("b", "INT64")], keys=[("a",)])
    s = BoundedSchema({"t": keyed})
    assert check_bounded("select a, b from t group by a", "select a, min(b) from t group by a", s, rows=3, dialect="mysql").bounded_equivalent


def test_row_number_ties_do_not_hide_a_difference():
    # found by the LeetCode cross-check: with every tie excluded, two NULL emails (a tie) were never tried
    result = check(
        "select a from t group by a having count(a) > 1",
        "select distinct a from (select a, row_number() over (partition by a order by a) as n from t) x where n > 1",
    )
    assert result.status is BoundedStatus.DIFFERENT


def test_sqlite_offers_counterexamples_but_no_equivalence_claim():
    s = BoundedSchema({"t": BTable("t", [BColumn("a", "INT64", True), BColumn("b", "INT64")])})
    same = check_bounded("select a from t where a > 1", "select a from t where a >= 2", s, rows=2, dialect="sqlite", replay=be.SQLiteReplay(s, "select a from t where a > 1", "select a from t where a >= 2"))
    assert same.status is BoundedStatus.UNKNOWN and not same.bounded_equivalent
    left, right = "select a from t where a > 1", "select a from t where a > 2"
    result = check_bounded(left, right, s, rows=2, dialect="sqlite", replay=be.SQLiteReplay(s, left, right))
    assert result.status is BoundedStatus.DIFFERENT
