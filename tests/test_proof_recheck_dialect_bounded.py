"""Round-two review of the dialect and bounded re-check adapters (tools/recheck/dialect_rewrites.py, bounded.py).

The adapters count what their eval's code counts today. The bounded adapter's legality check follows the
bounded encoding's own domain for a real column, so a witness never holds a value the encoding cannot model.
"""

from __future__ import annotations

import logging
from pathlib import Path
import sys

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("sqlglot")
pytest.importorskip("z3")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

from kumosql import bounded_equivalence as be  # noqa: E402
from recheck import bounded  # noqa: E402


def _schema() -> be.BoundedSchema:
    return be.BoundedSchema({"t": be.BTable("t", [be.BColumn("x", "NUMERIC(5, 2)"), be.BColumn("y", "DECIMAL(5, 2)")])})


def test_legal_follows_a_declared_numeric_domain():
    legal = bounded.Legal(_schema(), 3)
    assert legal({"t": [(999.99, 1.5)]})
    assert not legal({"t": [(1000, 1.5)]})  # NUMERIC(5, 2) holds |value| < 1000
    assert not legal({"t": [(1.234, 1.5)]})  # and two decimal digits


def test_legal_leaves_decimal_unrestricted_as_the_encoding_does():
    legal = bounded.Legal(_schema(), 3)
    assert legal({"t": [(1, 10**9)]})  # the encoding gives DECIMAL(p, s) no domain


def test_legal_caps_rows_per_table():
    legal = bounded.Legal(_schema(), 3)
    row = (1, 1)
    assert legal({"t": [row, row, row]})
    assert not legal({"t": [row, row, row, row]})


def test_zone_aware_and_naive_datetimes_of_one_instant_read_alike():
    import datetime as dt

    aware = dt.datetime(2020, 1, 3, tzinfo=dt.timezone.utc)
    assert bounded._naive(aware) == dt.datetime(2020, 1, 3)
    assert bounded._naive(dt.datetime(2020, 1, 3)) == dt.datetime(2020, 1, 3)
    assert bounded._naive(5) == 5
