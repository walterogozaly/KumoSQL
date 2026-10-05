"""Bounded model finding with row multiplicities (``kumosql.refute_bounded``): a few distinct rows, each repeated.

Every refutation it returns must be confirmed by the replay judge (``kumosql.refutation_replay``), so each test
replays the database it gets with a fresh judge; equivalent pairs must give none; constructs it does not model
raise ``Unsupported`` or give no refutation, never a wrong one.
"""

import time
from collections import Counter

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

from kumosql import bounded_equivalence as be  # noqa: E402
from kumosql import refute_bounded as rb  # noqa: E402
from kumosql.refutation_replay import Judge, Verdict, replay_counterexample  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

TYPES = {"t": {"a": "INT64", "b": "INT64"}, "u": {"a": "INT64", "c": "INT64"}}
KEYED_T = {"t": TableConstraints(keys=(("a",),), not_null=frozenset({"a"}))}
KEYED_U = {"u": TableConstraints(keys=(("a",),), not_null=frozenset({"a"}))}


def _keys(constraints):
    return {t: [list(k) for k in c.keys] for t, c in constraints.items()}


def search(left, right, *, constraints=None, types=TYPES, dialect="bigquery", seconds=15):
    """The database ``find_counterexample`` returns for the pair, after a fresh judge confirms it (or ``None``)."""

    constraints = constraints or {}
    with Judge(left, right, types, dialect=dialect, keys=_keys(constraints)) as judge:
        data = rb.find_counterexample(
            left, right, types, constraints, dialect=dialect, judge=judge, deadline=time.monotonic() + seconds
        )
    if data is None:
        return None
    assert replay_counterexample(left, right, data, schema=types, dialect=dialect, keys=_keys(constraints)), "the judge does not confirm it"
    return data


def copies(data, table):
    return Counter(map(tuple, data[table]))


# -- refutations the bounded-rows search cannot reach or the multiplicities make easy ---------------------------


def test_having_count_over_1000_is_refuted_against_the_unconditional_variant():
    data = search("select a from t group by a having count(*) > 1000", "select a from t group by a")
    assert data is not None
    assert len(data["t"]) <= 1000  # any group of up to 1,000 rows separates them: the search finds a small one


def test_group_that_needs_more_than_1000_rows_is_found():
    # the two thresholds differ only on a group of exactly 1,001 rows
    data = search("select a from t group by a having count(*) > 1000", "select a from t group by a having count(*) > 1001")
    assert data is not None
    assert max(copies(data, "t").values()) == 1001


def test_same_threshold_written_two_ways_is_not_refuted():
    assert search("select a from t group by a having count(*) > 1000", "select a from t group by a having count(*) >= 1001") is None


def test_intersect_all_against_intersect_needs_a_repeated_row():
    data = search("select a from t intersect all select a from u", "select a from t intersect distinct select a from u")
    assert data is not None
    assert max(max(copies(data, "t").values(), default=0), max(copies(data, "u").values(), default=0)) >= 2


def test_except_all_against_except_needs_a_repeated_row():
    data = search("select a from t except all select a from u", "select a from t except distinct select a from u")
    assert data is not None
    assert max(copies(data, "t").values()) > copies(data, "u").get(max(copies(data, "t"), key=copies(data, "t").get), 0)


def test_sum_against_sum_distinct_needs_a_repeated_value():
    data = search("select sum(a) from t", "select sum(distinct a) from t")
    assert data is not None
    values = [row[0] for row in data["t"] if row[0] is not None]
    assert len(values) > len(set(values))


def test_count_star_against_count_distinct():
    assert search("select count(*) from t", "select count(distinct a) from t") is not None


def test_avg_against_avg_distinct():
    assert search("select avg(a) from t", "select avg(distinct a) from t") is not None


def test_order_by_limit_against_distinct_limit():
    # LIMIT 2 of the sorted bag takes two copies of the smallest value; DISTINCT keeps one of each
    data = search("select a from t order by a limit 2", "select distinct a from t order by a limit 2")
    assert data is not None
    assert max(copies(data, "t").values()) >= 2


def test_order_by_limit_one_against_distinct_limit_two():
    assert search("select a from t order by a limit 1", "select distinct a from t order by a limit 2") is not None


def test_join_multiplies_the_copies():
    # a fan-out join repeats the left row once per match; DISTINCT hides it
    data = search("select t.a from t join u on t.a = u.a", "select distinct t.a from t join u on t.a = u.a")
    assert data is not None


def test_union_all_against_union():
    assert search("select a from t union all select a from u", "select a from t union distinct select a from u") is not None


def test_mysql_dialect_is_judged_on_duckdb_too():
    types = {"t": {"a": "INTEGER", "b": "INTEGER"}}
    data = search("select sum(a) from t", "select sum(distinct a) from t", types=types, dialect="mysql")
    assert data is not None


# -- keyed tables hold one copy of a row ---------------------------------------------------------------------------


def test_pair_that_differs_only_through_duplicates_is_refuted_without_a_key():
    assert search("select count(*) from t", "select count(distinct a) from t") is not None


def test_pair_that_differs_only_through_duplicates_is_not_refuted_with_a_key():
    assert search("select count(*) from t", "select count(distinct a) from t", constraints=KEYED_T) is None
    assert search("select sum(a) from t", "select sum(distinct a) from t", constraints=KEYED_T) is None
    assert search("select a, b from t", "select distinct a, b from t", constraints=KEYED_T) is None


def test_every_table_keyed_leaves_nothing_to_repeat():
    both = {**KEYED_T, **KEYED_U}
    with Judge("select a from t", "select distinct a from t", TYPES, dialect="bigquery", keys=_keys(both)) as judge:
        assert rb.find_counterexample("select a from t", "select distinct a from t", TYPES, both, dialect="bigquery", judge=judge, deadline=time.monotonic() + 5) is None


def test_keyed_table_stays_one_copy_while_another_table_repeats():
    left, right = "select t.a from t join u on t.a = u.a", "select distinct t.a from t join u on t.a = u.a"
    data = search(left, right, constraints=KEYED_T)
    assert data is not None
    assert max(copies(data, "t").values(), default=1) == 1  # a key value never repeats
    assert max(copies(data, "u").values()) >= 2  # the duplicates are in the keyless table


def test_database_from_model_repeats_only_keyless_rows():
    schema = be.schema_from_prover({t: list(c) for t, c in TYPES.items()}, KEYED_T, TYPES)
    database = rb.CountingDatabase(schema, 2, repeat=frozenset({"t", "u"}), cap=7)
    names = {slot.copies.decl().name() for slots in database.tables.values() for slot in slots if not rb.z3.is_int_value(slot.copies)}
    assert names and all(name.startswith("u#") for name in names)  # t has a key: no copy variable


# -- equivalent pairs give no refutation ----------------------------------------------------------------------------


EQUIVALENT = [
    ("select a from t where a > 2", "select a from t where a >= 3"),
    ("select count(*) from t", "select count(1) from t"),
    ("select a, sum(b) from t group by a", "select a, sum(b) from t group by a having count(*) > 0"),
    ("select distinct a from t", "select a from t group by a"),
    ("select a from t union all select a from t where a > 0", "select a from t union all select a from t where a >= 1"),
    ("select a from t intersect all select a from t", "select a from t"),
    ("select t.a, u.c from t join u on t.a = u.a", "select t.a, u.c from u join t on t.a = u.a"),
    ("select a from t order by a limit 2", "select a from t order by a asc limit 2"),
    ("select a from t except all select a from t where a < 0", "select a from t where a is null or a >= 0"),
    ("select sum(a) from t", "select sum(a) + 0 from t"),
]


@pytest.mark.parametrize("left,right", EQUIVALENT)
def test_equivalent_pairs_are_not_refuted(left, right):
    assert search(left, right, seconds=8) is None


# -- what the encoding does not model --------------------------------------------------------------------------------


def _compile(sql):
    schema = be.schema_from_prover({t: list(c) for t, c in TYPES.items()}, {}, TYPES)
    return rb.CountingCompiler(rb.CountingDatabase(schema, 2), "bigquery").compile(sql)


UNSUPPORTED = [
    ("select a, row_number() over (order by b) from t", "window"),
    ("select a, sum(b) over (partition by a) from t", "window"),
    ("select a from t qualify row_number() over (order by b) = 1", "window"),
    ("select t.a from t, lateral (select u.c from u where u.a = t.a) x", "LATERAL"),
    ("select * from t join u using (a)", "USING"),
    ("select * from t natural join u", "NATURAL"),
    ("select mystery(a) from t", "mystery"),
    ("select a from t limit 1", "LIMIT without ORDER BY"),
    ("select distinct on (a) a from t order by a, b", "DISTINCT ON"),
]


@pytest.mark.parametrize("sql,reason", UNSUPPORTED)
def test_unsupported_constructs_raise(sql, reason):
    with pytest.raises(be.Unsupported, match=reason):
        _compile(sql)


@pytest.mark.parametrize("sql,reason", UNSUPPORTED)
def test_unsupported_constructs_are_not_refuted(sql, reason):
    # the other side clearly differs on the data, but a query the encoding cannot read gives no refutation
    assert search(sql, "select 12345 from t", seconds=5) is None
    assert search("select 12345 from t", sql, seconds=5) is None


def test_unsupported_query_stops_the_search_at_once():
    began = time.monotonic()
    assert search("select a, row_number() over (order by b) from t", "select a, 1 from t") is None
    assert time.monotonic() - began < 2


# -- the cap on copies ---------------------------------------------------------------------------------------------


def test_cap_is_twice_the_largest_integer_literal():
    assert rb._cap("select a from t group by a having count(*) > 1000", "select a from t", "bigquery") >= 2000
    assert rb._cap("select a from t group by a having count(*) > 1000", "select a from t", "bigquery") == 2 * 1000 + 2
    assert rb._cap("select a from t", "select a from t where a = -70", "bigquery") == 2 * 70 + 2


def test_cap_has_a_floor_and_a_ceiling():
    floor = rb._cap("select a from t", "select a from t where a > 1", "bigquery")
    assert floor >= 2 * 8  # no literal: still room to cross a small constant
    assert rb._cap("select a from t where a > 9999999999", "select 1", "bigquery") == rb.MAX_COPIES


def test_cap_ignores_string_literals_and_unparsable_sql():
    assert rb._cap("select a from t where c = '99999'", "select a from t", "bigquery") == rb._cap("select a from t", "select a from t", "bigquery")
    assert rb._cap("this is not sql ((", "select a from t where a > 100", "bigquery") == 202


def test_copies_never_exceed_the_cap():
    left, right = "select sum(a) from t where a < 40", "select sum(distinct a) from t where a < 40"
    cap = rb._cap(left, right, "bigquery")
    data = search(left, right)
    assert data is not None
    assert max(copies(data, "t").values()) <= cap


def test_copy_variables_are_bounded_between_one_and_cap():
    schema = be.schema_from_prover({t: list(c) for t, c in TYPES.items()}, {}, TYPES)
    database = rb.CountingDatabase(schema, 2, repeat=frozenset({"t"}), cap=5)
    solver = rb.z3.Solver()
    solver.add(*database.constraints)
    slot = database.tables["t"][0]
    for value, status in ((0, rb.z3.unsat), (1, rb.z3.sat), (5, rb.z3.sat), (6, rb.z3.unsat)):
        solver.push()
        solver.add(slot.copies == value)
        assert solver.check() == status
        solver.pop()


# -- the judge decides ----------------------------------------------------------------------------------------------


class StubJudge:
    def __init__(self, verdict):
        self.answer = verdict
        self.seen = 0

    def verdict(self, data):
        self.seen += 1
        return self.answer


@pytest.mark.parametrize("verdict", [Verdict.SAME, Verdict.ERROR, Verdict.UNSTABLE, Verdict.ILLEGAL])
def test_no_database_is_returned_unless_the_judge_says_differs(verdict):
    judge = StubJudge(verdict)
    found = rb.find_counterexample(
        "select sum(a) from t", "select sum(distinct a) from t", TYPES, {}, dialect="bigquery", judge=judge, deadline=time.monotonic() + 10
    )
    assert found is None
    assert judge.seen >= 1  # the encoding did find models: the judge refused them


def test_the_database_the_judge_confirms_is_returned():
    judge = StubJudge(Verdict.DIFFERS)
    found = rb.find_counterexample(
        "select sum(a) from t", "select sum(distinct a) from t", TYPES, {}, dialect="bigquery", judge=judge, deadline=time.monotonic() + 10
    )
    assert found is not None and judge.seen == 1
    assert set(found) == {"t", "u"}


def test_expired_deadline_searches_nothing():
    judge = StubJudge(Verdict.DIFFERS)
    assert rb.find_counterexample("select a from t", "select 1 from t", TYPES, {}, dialect="bigquery", judge=judge, deadline=time.monotonic() - 1) is None
    assert judge.seen == 0


def test_mismatched_column_counts_give_no_refutation():
    assert search("select a from t", "select a, b from t") is None
