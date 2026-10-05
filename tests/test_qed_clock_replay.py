"""The replay helpers of tools/sqlsolver_bench.py that QED's Calcite cases need."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "sqlsolver_bench.py"
_spec = importlib.util.spec_from_file_location("sqlsolver_bench_for_clock", _path)
sb = importlib.util.module_from_spec(_spec)
sys.modules["sqlsolver_bench_for_clock"] = sb
_spec.loader.exec_module(sb)

DDL = "CREATE TABLE emp (id INTEGER NOT NULL, name VARCHAR(20) NOT NULL, hired TIMESTAMP NOT NULL, PRIMARY KEY (id));"


def _tables(tmp_path):
    path = tmp_path / "schema.sql"
    path.write_text(DDL)
    return sb.load_schema(path)


def test_fix_clock_reads_the_clock_as_a_given_day():
    assert sb.fix_clock("SELECT CURRENT_TIMESTAMP(), current_date FROM t", "1994-09-01") == "SELECT TIMESTAMP '1994-09-01 00:00:00', DATE '1994-09-01' FROM t"


def test_a_timestamp_column_is_stored_as_a_timestamp(tmp_path):
    columns = {c.name: c for c in _tables(tmp_path)["emp"].columns}
    assert sb._duck_type(columns["hired"]) == "TIMESTAMP"  # stored as a DATE, CAST(hired AS TIMESTAMP) would change the value


def test_a_text_to_number_cast_is_replayed_on_digit_strings(tmp_path):
    tables = _tables(tmp_path)
    db = sb.new_database(tables)
    # "a" cannot be cast, so the first replay is rejected; digit strings make the comparison runnable
    assert sb.differ("SELECT id FROM emp WHERE CAST(name AS SIGNED) = id", "SELECT id FROM emp WHERE id = CAST(name AS SIGNED)", tables, db, 10) is None
    assert sb.differ("SELECT id FROM emp WHERE CAST(name AS SIGNED) = id", "SELECT id FROM emp WHERE CAST(name AS SIGNED) = id + 1", tables, db, 30)


def test_the_clock_replay_can_tell_pairs_apart(tmp_path):
    tables = _tables(tmp_path)
    db = sb.new_database(tables)
    left, right = "SELECT id FROM emp WHERE hired = CURRENT_TIMESTAMP", "SELECT id FROM emp"
    assert sb.differ(left, right, tables, db, 30, clock=sb.DATES[2])
