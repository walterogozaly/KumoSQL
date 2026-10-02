"""Bulk row loading for DuckDB test databases.

``executemany`` binds parameters row by row, and for every bound value DuckDB probes for optional
Python modules (pandas, numpy, pyarrow, ...), which dominated the random-database searches. One
multi-row ``INSERT`` of literals loads the same rows several times faster. Values that cannot be
written as a plain literal (bytes, nested values, NaN or infinite floats) take the bound path.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Sequence


class _Unsupported(Exception):
    pass


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _Unsupported
        return f"CAST('{value!r}' AS DOUBLE)"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise _Unsupported
        return f"'{value}'"
    if isinstance(value, datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise _Unsupported


def insert_rows(db, table_sql: str, rows: Sequence[Sequence[Any]] | Iterable[Sequence[Any]]) -> None:
    """Insert ``rows`` into ``table_sql`` (already quoted if it needs to be)."""

    rows = list(rows)
    if not rows:
        return
    try:
        values = ", ".join("(" + ", ".join(_literal(v) for v in row) + ")" for row in rows)
    except _Unsupported:
        marks = ", ".join("?" * len(rows[0]))
        db.executemany(f"INSERT INTO {table_sql} VALUES ({marks})", rows)
        return
    db.execute(f"INSERT INTO {table_sql} VALUES {values}")
