"""tests/conftest.py runs DuckDB on one thread unless a caller sets ``threads``, keeping every other argument, and
marks a missing pandas as missing so DuckDB stops searching for it on every bound parameter."""

import importlib
import importlib.util
import sys

import pytest

duckdb = pytest.importorskip("duckdb")


def _threads(con):
    return con.execute("SELECT current_setting('threads')").fetchone()[0]


def test_connections_default_to_one_thread():
    assert _threads(duckdb.connect()) == 1
    assert _threads(duckdb.connect(":memory:")) == 1
    assert _threads(duckdb.connect(database=":memory:", config={"default_order": "DESC"})) == 1


def test_a_thread_count_the_caller_sets_is_kept():
    assert _threads(duckdb.connect(config={"threads": 2})) == 2
    assert _threads(duckdb.connect(":memory:", False, {"threads": 3})) == 3
    assert _threads(duckdb.connect(config={"worker_threads": 2})) == 2


def test_other_arguments_pass_through_unchanged(tmp_path):
    path = str(tmp_path / "t.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE TABLE t AS SELECT 1 AS a")
    con.close()
    config = {"default_order": "DESC"}
    for con in (duckdb.connect(path, True, config), duckdb.connect(path, read_only=True, config=config)):
        assert _threads(con) == 1
        assert con.execute("SELECT current_setting('default_order')").fetchone()[0] == "DESC"
        with pytest.raises(duckdb.Error):
            con.execute("INSERT INTO t VALUES (2)")
        con.close()
    assert config == {"default_order": "DESC"}  # the caller's dict is not changed


def test_a_missing_pandas_stays_missing_and_an_installed_one_is_untouched():
    if importlib.util.find_spec("pandas") is None:  # not installed: marked missing, and still missing
        assert "pandas" in sys.modules and sys.modules["pandas"] is None
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("pandas")
    else:
        assert importlib.import_module("pandas") is not None
    con = duckdb.connect()
    con.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [[1, "x"], [2, None]])
    assert con.execute("SELECT * FROM t WHERE a = ?", [2]).fetchall() == [(2, None)]
