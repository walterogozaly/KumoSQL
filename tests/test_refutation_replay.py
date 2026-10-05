"""The judge of synthesized refutations: when a database is a replayable counterexample, and when it is not."""

from decimal import Decimal

import pytest

pytest.importorskip("duckdb")

from kumosql import refutation_replay  # noqa: E402
from kumosql.refutation_replay import Judge, Verdict, bag, positional, replay_counterexample, witness_differs  # noqa: E402

T = {"t": {"a": "INT64", "b": "INT64"}}
TU = {"t": {"a": "INT64", "b": "INT64"}, "u": {"c": "INT64", "d": "INT64"}}


def verdict(left, right, data, schema=T, dialect="bigquery", **kwargs):
    with Judge(left, right, schema, dialect=dialect, **kwargs) as judge:
        assert judge.problem is None
        return judge.verdict(data)


# --- one test per verdict kind -----------------------------------------------------------------------


@pytest.mark.parametrize("dialect", ["bigquery", "duckdb"])
def test_differs_on_a_confirmed_difference(dialect):
    rows = {"t": [(1, 1), (2, 2)]}
    assert verdict("SELECT a FROM t", "SELECT a FROM t WHERE a > 1", rows, dialect=dialect) is Verdict.DIFFERS


def test_differs_for_mysql_sql():
    schema = {"t": {"a": "INTEGER", "b": "INTEGER"}}
    rows = {"t": [(1, 1), (2, 2)]}
    assert verdict("SELECT a FROM t", "SELECT a FROM t WHERE a > 1", rows, schema, "mysql") is Verdict.DIFFERS
    assert verdict("SELECT a FROM t", "SELECT a FROM t WHERE a > 0", rows, schema, "mysql") is Verdict.SAME


def test_same_when_the_bags_agree():
    rows = {"t": [(1, 1), (2, 2)]}
    assert verdict("SELECT a FROM t", "SELECT a FROM t WHERE a IS NOT NULL", rows) is Verdict.SAME


def test_same_counts_duplicates():
    rows = {"t": [(1, 1), (1, 2)]}
    assert verdict("SELECT a FROM t", "SELECT DISTINCT a FROM t", rows) is Verdict.DIFFERS
    assert verdict("SELECT a FROM t", "SELECT DISTINCT a FROM t", {"t": [(1, 1)]}) is Verdict.SAME


def test_error_when_a_query_fails():
    assert verdict("SELECT a FROM t", "SELECT nope FROM t", {"t": [(1, 1)]}) is Verdict.ERROR


def test_error_when_a_bigquery_guard_fires():
    # BigQuery fails on division by zero, so a difference found next to it is no evidence
    assert verdict("SELECT a / b FROM t", "SELECT a FROM t", {"t": [(1, 0)]}) is Verdict.ERROR


def test_error_when_sql_does_not_parse():
    with Judge("SELEC FROM", "SELECT a FROM t", T) as judge:
        assert judge.problem is not None and judge.problem.startswith("cannot translate")
        assert judge.verdict({"t": [(1, 1)]}) is Verdict.ERROR


def test_unstable_for_limit_without_order_by():
    rows = {"t": [(1, 1), (2, 2)]}
    assert verdict("SELECT a FROM t LIMIT 1", "SELECT a FROM t LIMIT 1 OFFSET 1", rows) is Verdict.UNSTABLE
    # with a total order the same pair is a stable difference
    assert verdict("SELECT a FROM t ORDER BY a LIMIT 1", "SELECT a FROM t ORDER BY a LIMIT 1 OFFSET 1", rows) is Verdict.DIFFERS


def test_unstable_when_an_arbitrary_pick_decides():
    # ANY_VALUE returns whichever row comes first, so storing the rows in another order changes it
    rows = {"t": [(1, 5), (1, 7), (1, 6)]}
    left, right = "SELECT a, ANY_VALUE(b) FROM t GROUP BY a", "SELECT a, MAX(b) FROM t GROUP BY a"
    assert verdict(left, right, rows, dialect="duckdb") is Verdict.UNSTABLE


def test_a_stable_difference_survives_any_storage_order():
    left, right = "SELECT a FROM t", "SELECT a FROM t WHERE a > 1"
    rows = [(1, 1), (2, 2), (3, 3), (4, 4)]
    with Judge(left, right, T) as judge:
        assert judge.verdict({"t": rows}) is Verdict.DIFFERS
        assert judge.verdict({"t": list(reversed(rows))}) is Verdict.DIFFERS
        assert judge.verdict({"t": rows[2:] + rows[:2]}) is Verdict.DIFFERS


def test_reorders_cover_reversed_rotated_and_shuffled_rows():
    import random

    data = {"t": [(1,), (2,), (3,), (4,)]}
    orders = [d["t"] for d in refutation_replay._reorders(data, random.Random(0), 3)]
    assert orders[0] == [(4,), (3,), (2,), (1,)]
    assert orders[1] == [(2,), (3,), (4,), (1,)]
    assert len(orders) == 5
    assert all(sorted(o) == data["t"] for o in orders)
    assert data == {"t": [(1,), (2,), (3,), (4,)]}  # the caller's rows are untouched


# --- illegal databases --------------------------------------------------------------------------------


def test_illegal_when_a_key_is_violated():
    left, right = "SELECT a FROM t", "SELECT a + 1 FROM t"
    keys = {"t": [["a"]]}
    assert verdict(left, right, {"t": [(1, 1), (1, 2)]}, keys=keys) is Verdict.ILLEGAL
    assert verdict(left, right, {"t": [(1, 1), (2, 2)]}, keys=keys) is Verdict.DIFFERS


def test_illegal_when_a_key_column_is_null():
    left, right = "SELECT a FROM t", "SELECT a + 1 FROM t"
    assert verdict(left, right, {"t": [(None, 1)]}, keys={"t": [["a"]]}) is Verdict.ILLEGAL


def test_illegal_when_a_not_null_column_is_null():
    left, right = "SELECT b FROM t", "SELECT b + 1 FROM t"
    assert verdict(left, right, {"t": [(1, None)]}, not_null={"t": ["b"]}) is Verdict.ILLEGAL
    assert verdict(left, right, {"t": [(None, 1)]}, not_null={"t": ["b"]}) is Verdict.DIFFERS


def test_illegal_when_a_foreign_key_has_no_parent():
    schema = {"p": {"id": "INT64"}, "c": {"pid": "INT64"}}
    fks = [("c", ["pid"], "p", ["id"])]
    left, right = "SELECT pid FROM c", "SELECT pid + 1 FROM c"
    assert verdict(left, right, {"p": [(1,)], "c": [(2,)]}, schema, foreign_keys=fks) is Verdict.ILLEGAL
    assert verdict(left, right, {"p": [(1,)], "c": [(1,)]}, schema, foreign_keys=fks) is Verdict.DIFFERS
    # a NULL child value needs no parent
    assert verdict(left, right, {"p": [], "c": [(None,)]}, schema, foreign_keys=fks) is Verdict.SAME


def test_names_compare_case_insensitively():
    keys = {"T": [["A"]]}
    assert verdict("SELECT a FROM t", "SELECT a + 1 FROM t", {"t": [(1, 1), (1, 2)]}, keys=keys) is Verdict.ILLEGAL


# --- NULL order: BigQuery sorts NULLs first ascending and last descending --------------------------------

NULLS = {"t": [(None, 1), (3, 2)]}


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        ("a ASC NULLS LAST", 3),
        ("a ASC NULLS FIRST", None),
        ("a", None),  # BigQuery's default ascending order puts NULL first
        ("a ASC", None),
        ("a DESC", 3),  # and descending puts it last
        ("a DESC NULLS LAST", 3),
        ("a DESC NULLS FIRST", None),
    ],
)
def test_bigquery_null_order_is_kept_when_translating(order, expected):
    sql = f"SELECT a FROM t ORDER BY {order} LIMIT 1"
    with Judge(sql, sql, T) as judge:
        left, _ = judge.outputs(NULLS)
    assert left == [(expected,)], judge.left


def test_asc_nulls_last_limit_is_the_minimum_that_is_not_null():
    # ASC NULLS LAST ... LIMIT 1 is the smallest value, not NULL: the same on either side is no difference
    nulls_last = "SELECT a FROM t ORDER BY a ASC NULLS LAST LIMIT 1"
    assert verdict(nulls_last, "SELECT MIN(a) FROM t", NULLS) is Verdict.SAME
    # while the default order takes the NULL row
    assert verdict("SELECT a FROM t ORDER BY a LIMIT 1", "SELECT MIN(a) FROM t", NULLS) is Verdict.DIFFERS
    # and ASC NULLS FIRST is not rewritten into the default of the session
    assert verdict("SELECT a FROM t ORDER BY a ASC NULLS FIRST LIMIT 1", nulls_last, NULLS) is Verdict.DIFFERS


def test_null_order_survives_a_renamed_table():
    schema = {"proj.ds.t": {"a": "INT64", "b": "INT64"}}
    nulls_last = "SELECT a FROM `proj.ds.t` ORDER BY a ASC NULLS LAST LIMIT 1"
    data = {"proj.ds.t": NULLS["t"]}
    assert verdict(nulls_last, "SELECT MIN(a) FROM `proj.ds.t`", data, schema) is Verdict.SAME
    assert verdict("SELECT a FROM `proj.ds.t` ORDER BY a LIMIT 1", "SELECT MIN(a) FROM `proj.ds.t`", data, schema) is Verdict.DIFFERS


def test_duckdb_sql_is_run_as_written():
    # DuckDB sorts NULLs last by default; DuckDB-dialect SQL keeps DuckDB's meaning
    left = "SELECT a FROM t ORDER BY a LIMIT 1"
    assert verdict(left, "SELECT MIN(a) FROM t", NULLS, dialect="duckdb") is Verdict.SAME


# --- floats -------------------------------------------------------------------------------------------


def test_bag_rounds_floats_to_six_significant_digits():
    assert bag([(0.1 + 0.2,)]) != bag([(0.3,)])
    assert bag([(0.1 + 0.2,)], 6) == bag([(0.3,)], 6)
    assert bag([(123456.7,)], 6) == bag([(123456.9,)], 6)  # 123457 either way
    assert bag([(123456.7,)], 6) != bag([(123466.7,)], 6)
    assert bag([(Decimal("1.0000001"),)], 6) == bag([(1.0,)], 6)
    assert bag([(float("inf"),)], 6) == bag([(float("inf"),)])  # non-finite floats are left alone
    assert bag([(0.0,), (None,), (True,), ("x",)], 6) == bag([(0.0,), (None,), (True,), ("x",)])


def test_bag_rounds_floats_inside_arrays_too():
    assert bag([([0.1 + 0.2, 1],)], 6) == bag([([0.3, 1],)], 6)


def test_a_difference_in_the_seventh_digit_is_no_difference():
    schema = {"t": {"x": "FLOAT64"}}
    rows = {"t": [(1.5,), (2.25,)]}
    assert verdict("SELECT x FROM t", "SELECT x * 1.0000000001 FROM t", rows, schema) is Verdict.SAME
    assert verdict("SELECT x FROM t", "SELECT x * 1.001 FROM t", rows, schema) is Verdict.DIFFERS


def test_float_sums_in_another_order_are_the_same_bag():
    schema = {"t": {"x": "FLOAT64"}}
    rows = {"t": [(0.1,), (0.2,), (0.3,)]}
    assert verdict("SELECT SUM(x) FROM t", "SELECT 0.3 + 0.2 + 0.1 FROM t LIMIT 1", rows, schema) is Verdict.SAME


# --- DuckDB's optimizer -------------------------------------------------------------------------------

# DuckDB 1.5 returns no row for the EXISTS query below on this database although (0, 1) qualifies: the
# unoptimized plan returns it, so the left side equals ``right`` once the bug is out of the way
OPTIMIZER_DATA = {"t": [(0, 1), (1, None), (0, 0)], "u": [(0, 1), (1, 2)]}
OPTIMIZER_LEFT = "SELECT a, b FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.d <> t.a AND t.b > u.c)"
OPTIMIZER_RIGHT = "SELECT a, b FROM t WHERE a = 0 AND b = 1"


@pytest.mark.parametrize("dialect", ["bigquery", "duckdb"])
def test_a_difference_only_the_optimizer_makes_is_not_differs(dialect):
    with Judge(OPTIMIZER_LEFT, OPTIMIZER_RIGHT, TU, dialect=dialect) as judge:
        optimized = judge.outputs(OPTIMIZER_DATA)
        reference = refutation_replay.run_unoptimized(judge.db, judge.left, judge.right)
        found = judge.verdict(OPTIMIZER_DATA)
    assert reference[0] == reference[1] == [(0, 1)]  # the plan without the optimizer is right
    if sorted(optimized[0]) != sorted(optimized[1]):
        assert found is Verdict.UNSTABLE  # this DuckDB has the bug: the optimized difference is not evidence
    else:
        assert found is Verdict.SAME  # a DuckDB that fixed it sees no difference at all


def test_unoptimized_disagreement_is_unstable(monkeypatch):
    # independent of the DuckDB version: the optimizer-off run returns other rows than the optimized one
    calls = []

    def fake(db, *queries):
        calls.append(queries)
        return [[(99,)] for _ in queries]

    monkeypatch.setattr(refutation_replay, "run_unoptimized", fake)
    rows = {"t": [(1, 1), (2, 2)]}
    assert verdict("SELECT a FROM t", "SELECT a FROM t WHERE a > 1", rows) is Verdict.UNSTABLE
    assert len(calls) == 1


def test_unoptimized_agreement_is_required_to_confirm(monkeypatch):
    seen = []
    real = refutation_replay.run_unoptimized

    def spy(db, *queries):
        seen.append(queries)
        return real(db, *queries)

    monkeypatch.setattr(refutation_replay, "run_unoptimized", spy)
    rows = {"t": [(1, 1), (2, 2)]}
    assert verdict("SELECT a FROM t", "SELECT a FROM t WHERE a > 1", rows) is Verdict.DIFFERS
    assert len(seen) == 1
    # equal bags never reach the optimizer-off run
    seen.clear()
    assert verdict("SELECT a FROM t", "SELECT a FROM t", rows) is Verdict.SAME
    assert seen == []


# --- helpers the evals replay with ----------------------------------------------------------------------


def test_positional_accepts_dict_rows_and_positional_rows():
    out = positional({"T": [{"B": 2, "a": 1}, (3, 4)], "other": [(9,)]}, T)
    assert out == {"t": [(1, 2), (3, 4)]}
    assert positional({"x.y.t": [{"a": 1}]}, T) == {"t": [(1, None)]}


def test_replay_counterexample_accepts_a_prover_counterexample():
    from kumosql.smt_equivalence import Counterexample

    found = Counterexample(tables={"t": [{"a": 1, "b": 1}, {"a": 2, "b": 2}]}, left_rows=[], right_rows=[])
    assert replay_counterexample("SELECT a FROM t", "SELECT a FROM t WHERE a > 1", found, schema=T, dialect="bigquery")
    assert not replay_counterexample("SELECT a FROM t", "SELECT a FROM t", found, schema=T, dialect="bigquery")
    assert not replay_counterexample("SELECT a FROM t", "SELECT a FROM t WHERE a > 1", "not a database", schema=T, dialect="bigquery")


def test_replay_counterexample_applies_the_declared_constraints():
    rows = {"t": [(1, 1), (1, 2)]}
    args = ("SELECT a FROM t", "SELECT a + 1 FROM t", rows)
    assert replay_counterexample(*args, schema=T, dialect="bigquery")
    assert not replay_counterexample(*args, schema=T, dialect="bigquery", keys={"t": [["a"]]})


def test_witness_differs_runs_the_cases_own_database():
    left, right = "SELECT a FROM t", "SELECT a FROM t WHERE a > 1"
    assert witness_differs(left, right, {"t": [[1, 1], [2, 2]]}, schema=T, dialect="bigquery")
    assert not witness_differs(left, right, {"t": [[2, 2]]}, schema=T, dialect="bigquery")
    assert not witness_differs(left, "SELECT nope FROM t", {"t": [[2, 2]]}, schema=T, dialect="bigquery")


def test_witness_differs_on_sqlite_for_pairs_duckdb_cannot_run():
    assert witness_differs("SELECT 1", "SELECT 2", {}, schema={}, dialect="sqlite", engine="sqlite")
    assert not witness_differs("SELECT 1", "SELECT 1.0", {}, schema={}, dialect="sqlite", engine="sqlite")


def test_judges_are_independent():
    # no state is shared between judges, so tests may run in parallel
    with Judge("SELECT a FROM t", "SELECT a + 1 FROM t", T) as one, Judge("SELECT a FROM t", "SELECT a FROM t", T) as two:
        rows = {"t": [(1, 1)]}
        assert one.verdict(rows) is Verdict.DIFFERS
        assert two.verdict(rows) is Verdict.SAME
        assert one.verdict(rows) is Verdict.DIFFERS
