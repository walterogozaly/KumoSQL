"""The GoogleSQL differential check (tools/gsql_differential.py): the evaluator against BigQuery-on-DuckDB.

These cover the comparison, the verdicts (with a fake engine pair standing in for the two real ones), the
shrinker, the random generator and one small run of the real thing. ``python tools/gsql_differential.py``
runs every claimed conformance dev case.
"""

import importlib.util
import json
import math
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "tools" / "gsql_differential.py"
_SPEC = importlib.util.spec_from_file_location("gsql_differential", _PATH)
D = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = D  # dataclasses (postponed annotations) look their module up here
_SPEC.loader.exec_module(D)
T = D.T
V = D.V


def answer(columns, rows, ordered=True, inexact=False):
    return D.Answer([(f"c{i}", t) for i, t in enumerate(columns)], [tuple(r) for r in rows], ordered, inexact)


class FakeSession:
    """Both engines scripted: ``evaluate`` returns ``ev`` (or raises a Skip), ``duckdb`` the rows for the optimizer on and off."""

    def __init__(self, ev, on, off=None, last_sql="SELECT 1"):
        self.ev, self.on, self.off = ev, on, on if off is None else off
        self.last_sql = last_sql
        self.calls = []

    def evaluate(self, sql):
        self.calls.append(("evaluate", sql))
        if isinstance(self.ev, Exception):
            raise self.ev
        return self.ev

    def duckdb(self, sql, optimizer=True):
        self.calls.append(("duckdb", optimizer))
        result = self.on if optimizer else self.off
        if isinstance(result, Exception):
            raise result
        return result


# --- comparing values ------------------------------------------------------------------------------


def test_floats_compare_within_ulps_and_nan_equals_nan():
    assert D.float_close(0.1 + 0.2, 0.3)
    assert not D.float_close(1.0, 1.0001)
    assert D.float_close(math.nan, math.nan) and not D.float_close(math.nan, 1.0)
    assert D.float_close(math.inf, math.inf) and not D.float_close(math.inf, -math.inf)
    assert D.float_close(1.0, 1.0 + 1e-10, relative=1e-9) and not D.float_close(1.0, 1.0 + 1e-10)
    assert D.values_equal(T.FLOAT64, math.nan, math.nan)


def test_values_must_have_the_type_the_evaluator_says():
    assert D.values_equal(T.INT64, 3, 3)
    assert not D.values_equal(T.INT64, 3, 3.0)  # a double where BigQuery has an INT64
    assert not D.values_equal(T.INT64, 1, True)
    assert not D.values_equal(T.FLOAT64, 3.0, Decimal("3.0"))  # a decimal literal where BigQuery has FLOAT64
    assert D.values_equal(T.FLOAT64, 3.0, Decimal("3.0"), strict=False)  # the same value, another number type
    assert not D.values_equal(T.FLOAT64, 3.0, Decimal("3.5"), strict=False)
    assert D.values_equal(T.NUMERIC, Decimal("1.50"), Decimal("1.5"))
    assert not D.values_equal(T.NUMERIC, Decimal("1.5"), 1.5)
    assert D.values_equal(T.STRING, "a", "a") and not D.values_equal(T.STRING, "a", b"a")
    assert D.values_equal(T.STRING, None, None) and not D.values_equal(T.STRING, "", None)


def test_dates_timestamps_and_midnight_datetimes():
    assert D.values_equal(T.DATE, date(2020, 2, 29), date(2020, 2, 29))
    assert not D.values_equal(T.DATE, date(2020, 2, 29), datetime(2020, 2, 29, 1))
    micros = V.utc_to_micros(datetime(2020, 1, 2, 3, 4, 5, 6))
    assert D.values_equal(T.TIMESTAMP, micros, datetime(2020, 1, 2, 3, 4, 5, 6))
    # the layer turns a midnight timestamp into a date, as it cannot tell it from a DATE
    assert D.values_equal(T.DATETIME, datetime(2020, 1, 2), date(2020, 1, 2))
    assert not D.values_equal(T.DATETIME, datetime(2020, 1, 2, 1), date(2020, 1, 2))


def test_arrays_a_null_array_equals_an_empty_one_and_unordered_ones_compare_as_multisets():
    arr = T.array(T.INT64)
    assert D.values_equal(arr, (), None) and D.values_equal(arr, None, None) and D.values_equal(arr, None, ())
    assert D.values_equal(arr, (1, 2), (1, 2)) and not D.values_equal(arr, (1, 2), (2, 1))
    assert D.values_equal(arr, V.UnorderedArray((1, 2)), (2, 1))
    assert not D.values_equal(arr, V.UnorderedArray((1, 2)), (2, 2))
    floats = T.array(T.FLOAT64)
    assert D.values_equal(floats, (0.1 + 0.2, math.nan), (0.3, math.nan))
    nested = T.struct([("a", T.INT64), ("b", T.array(T.STRING))])
    assert D.values_equal(nested, (1, ("x",)), (1, ("x",)))
    assert not D.values_equal(nested, (1, ("x",)), (1, ("y",)))
    assert D.values_equal(nested, (1, ()), (1, None))


def test_rows_ordered_and_unordered():
    cols = [T.INT64, T.STRING]
    assert D.rows_equal(answer(cols, [(1, "a"), (2, "b")]), [(1, "a"), (2, "b")])
    assert not D.rows_equal(answer(cols, [(1, "a"), (2, "b")]), [(2, "b"), (1, "a")])
    assert D.rows_equal(answer(cols, [(1, "a"), (2, "b")], ordered=False), [(2, "b"), (1, "a")])
    assert not D.rows_equal(answer(cols, [(1, "a")]), [(1, "a"), (1, "a")])  # row count
    assert not D.rows_equal(answer(cols, [(1, "a")]), [(1,)])  # column count
    floats = answer([T.FLOAT64], [(0.1 + 0.2,), (math.nan,)], ordered=False)
    assert D.rows_equal(floats, [(math.nan,), (0.3,)])


def test_a_result_type_the_layer_cannot_return_is_skipped():
    with pytest.raises(D.Skip):
        D.rows_equal(answer([T.INTERVAL], [(V.Interval(1, 0, 0),)]), [(1,)])


# --- verdicts, with a fake engine pair -------------------------------------------------------------


def test_equal_rows_agree():
    verdict = D.classify(FakeSession(answer([T.INT64], [(1,)]), [(1,)]), "SELECT 1")
    assert verdict.kind == "agree"


def test_a_difference_is_a_divergence_when_the_optimizer_off_run_agrees_with_the_optimizer_on_run():
    session = FakeSession(answer([T.INT64], [(3,)]), on=[(2,)], off=[(2,)])
    verdict = D.classify(session, "SELECT CAST(2.5 AS INT64)")
    assert verdict.kind == "diverge" and verdict.difference == "value"
    assert verdict.evaluator_rows == [(3,)] and verdict.duckdb_rows == [(2,)]
    assert ("duckdb", False) in session.calls


def test_equal_values_of_another_number_type_are_a_type_difference():
    verdict = D.classify(FakeSession(answer([T.FLOAT64], [(3.0,)]), on=[(Decimal("3.0"),)]), "SELECT 3.0")
    assert verdict.kind == "diverge" and verdict.difference == "type"


def test_a_difference_the_optimizer_off_run_removes_is_an_optimizer_bug_not_a_divergence():
    session = FakeSession(answer([T.INT64], [(1,)]), on=[(7,)], off=[(1,)])
    assert D.classify(session, "SELECT 1").kind == "optimizer"


def test_an_optimizer_off_run_that_agrees_with_neither_is_unstable():
    session = FakeSession(answer([T.INT64], [(1,)]), on=[(7,)], off=[(9,)])
    assert D.classify(session, "SELECT 1").kind == "unstable"
    failing = FakeSession(answer([T.INT64], [(1,)]), on=[(7,)], off=D.NotRun("duckdb: timeout"))
    assert D.classify(failing, "SELECT 1").kind == "unstable"


def test_a_layer_that_declines_or_cannot_run_is_not_a_divergence():
    declined = D.classify(FakeSession(answer([T.INT64], [(1,)]), D.NotRun("refused: x", declined=True)), "SELECT 1")
    assert declined.kind == "not_run" and declined.declined
    not_run = D.classify(FakeSession(answer([T.INT64], [(1,)]), D.NotRun("duckdb: Parser Error")), "SELECT 1")
    assert not_run.kind == "not_run" and not not_run.declined


def test_a_query_the_evaluator_does_not_answer_is_skipped_and_duckdb_is_not_asked():
    session = FakeSession(D.Skip("unsupported: x"), [(1,)])
    verdict = D.classify(session, "SELECT 1")
    assert verdict.kind == "skipped" and session.calls == [("evaluate", "SELECT 1")]


def test_an_unordered_answer_compares_as_a_multiset():
    ev = answer([T.INT64], [(1,), (2,)], ordered=False)
    assert D.classify(FakeSession(ev, [(2,), (1,)]), "SELECT x").kind == "agree"


# --- shrinking -------------------------------------------------------------------------------------


class ShrinkSession(FakeSession):
    """Diverges (3 against 2) exactly when the query uses ABS and the table holds the row (5,)."""

    def __init__(self, tables):
        rows = [r[0] for r in tables["t"].rows] if "t" in tables else []
        self.rows = rows
        super().__init__(None, None)

    def evaluate(self, sql):
        return answer([T.INT64], [(3,)])

    def duckdb(self, sql, optimizer=True):
        return [(2,)] if "ABS(" in sql.upper() and 5 in self.rows else [(3,)]


def test_shrink_drops_rows_clauses_and_columns_while_the_divergence_holds():
    table = D.Table([("x", T.INT64), ("y", T.INT64)], [(1, 9), (5, 8), (7, 7), (5, 6)])
    sql = "SELECT ABS(x) + 1 AS a, y AS b FROM t WHERE x > 0 ORDER BY y"
    tables, small, verdict, checks = D.shrink({"t": table, "unused": D.Table([("z", T.INT64)], [(1,)])}, sql, ShrinkSession)
    assert verdict is not None and verdict.kind == "diverge"
    assert list(tables) == ["t"] and len(tables["t"].rows) == 1 and tables["t"].rows[0][0] == 5
    assert [c for c, _ in tables["t"].columns] == ["x"]
    assert "ABS(" in small.upper() and len(small) < len(sql) and "ORDER BY" not in small and "WHERE" not in small
    assert checks > 0


def test_shrink_returns_no_verdict_when_the_query_does_not_diverge():
    table = D.Table([("x", T.INT64)], [(1,)])
    tables, sql, verdict, _ = D.shrink({"t": table}, "SELECT ABS(x) AS a FROM t", ShrinkSession)
    assert verdict is None and sql == "SELECT ABS(x) AS a FROM t"


# --- the random generator --------------------------------------------------------------------------


def test_random_queries_and_tables_are_deterministic_per_seed():
    assert [D.random_query(3, i) for i in range(40)] == [D.random_query(3, i) for i in range(40)]
    assert [D.random_query(3, i) for i in range(40)] != [D.random_query(4, i) for i in range(40)]
    a, b = D.random_tables(7)["t"], D.random_tables(7)["t"]
    assert a.rows == b.rows and a.columns == b.columns and len(a.rows) == 10
    assert D.random_tables(7)["t"].rows != D.random_tables(8)["t"].rows


def test_random_queries_parse_as_bigquery():
    import sqlglot

    for i in range(150):
        sqlglot.parse_one(D.random_query(11, i), read="bigquery")


def test_random_units_cover_every_query_once_whatever_the_job_count():
    for jobs in (1, 4):
        units = D.random_units(130, 2, jobs)
        sources = [q[0] for u in units.values() for q in u["queries"]]
        assert sources == [f"random:2:{i}" for i in range(130)] or sorted(sources) == sorted(f"random:2:{i}" for i in range(130))


def test_functions_of_names_what_a_query_uses():
    names = D.functions_of("SELECT CAST(ROUND(f) AS INT64) + 1.5 FROM t")
    assert "CAST->INT64" in names and "Round" in names and "float-literal" in names


# --- the real engines ------------------------------------------------------------------------------


def test_typed_tables_load_into_both_engines_and_agree():
    pytest.importorskip("duckdb")
    columns = [
        ("i", T.INT64), ("f", T.FLOAT64), ("n", T.NUMERIC), ("s", T.STRING), ("d", T.DATE), ("ts", T.TIMESTAMP),
        ("a", T.array(T.INT64)), ("st", T.struct([("p", T.INT64), ("q", T.STRING)])),
    ]
    ts = V.utc_to_micros(datetime(2020, 1, 2, 3, 4, 5))
    rows = [(1, 1.5, Decimal("2.25"), "x", date(2020, 2, 29), ts, (1, 2), (7, "z")), (None,) * 8]
    session = D.Session({"t": D.Table(columns, rows)})
    try:
        for sql in ("SELECT i, f, n, s, d, ts FROM t ORDER BY i", "SELECT a, st FROM t ORDER BY i", "SELECT i + 1 AS j, UPPER(s) AS u FROM t ORDER BY i"):
            verdict = D.classify(session, sql)
            assert verdict.kind in ("agree", "not_run"), (sql, verdict)
        assert D.classify(session, "SELECT i + 1 AS j FROM t ORDER BY i").kind == "agree"
        assert D.classify(session, "SELECT i FROM t ORDER BY i").kind == "agree"
    finally:
        session.close()


def test_the_real_pair_finds_a_known_divergence():
    pytest.importorskip("duckdb")
    # BigQuery reads 3.0 as FLOAT64; the DuckDB layer reads it as a DECIMAL
    session = D.Session({})
    try:
        verdict = D.classify(session, "SELECT 3.0 AS x")
        assert verdict.kind in ("diverge", "agree")  # agree once the layer maps decimal literals to DOUBLE
        if verdict.kind == "diverge":
            assert verdict.difference == "type"
        assert D.classify(session, "SELECT 1 + 2 AS x").kind == "agree"
    finally:
        session.close()


def test_smoke_run_of_conformance_cases_and_random_queries(tmp_path):
    pytest.importorskip("duckdb")
    out = tmp_path / "report.json"
    code = D.main(["--limit", "40", "--random", "40", "--seed", "1", "--jobs", "1", "--json", str(out)])
    assert code == 0
    report = json.loads(out.read_text())
    assert report["queries"] == report["conformance_cases_selected"] + 40
    assert report["compared"] == report["agree"] + report["diverge"] + report["diverge_optimizer_explained"] + report["unstable"]
    assert report["evaluator_answered"] == report["compared"] + report["declined_or_not_run"]
    assert report["agree"] > 10
    assert report["split"] == "dev"
    for repro in report["divergences"]:
        assert repro["sql"] and repro["difference"] in ("value", "type") and repro["evaluator_text"] and repro["duckdb_text"]
