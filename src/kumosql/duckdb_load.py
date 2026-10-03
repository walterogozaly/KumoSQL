"""Bulk row loading for DuckDB test databases.

``executemany`` binds parameters row by row, and for every bound value DuckDB probes for optional
Python modules (pandas, numpy, pyarrow, ...), which dominated the random-database searches. One
multi-row ``INSERT`` of literals loads the same rows several times faster. Values that cannot be
written as a plain literal (bytes, nested values, NaN or infinite floats) take the bound path.
``TableLoader`` and ``rows_key`` let a search that reloads its tables before every try skip the
statements (and whole tries) that would repeat one it already made.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence


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


def values_sql(rows: Sequence[Sequence[Any]]) -> str | None:
    """The rows as a ``VALUES`` list of literals (``""`` for no rows), or ``None`` if a value has no plain literal."""

    try:
        return ", ".join("(" + ", ".join(_literal(v) for v in row) + ")" for row in rows)
    except _Unsupported:
        return None


def _insert(db, table_sql: str, rows: list, values: str | None) -> None:
    if not rows:
        return
    if values is None:
        marks = ", ".join("?" * len(rows[0]))
        db.executemany(f"INSERT INTO {table_sql} VALUES ({marks})", rows)
        return
    db.execute(f"INSERT INTO {table_sql} VALUES {values}")


def insert_rows(db, table_sql: str, rows: Sequence[Sequence[Any]] | Iterable[Sequence[Any]]) -> None:
    """Insert ``rows`` into ``table_sql`` (already quoted if it needs to be)."""

    rows = list(rows)
    _insert(db, table_sql, rows, values_sql(rows))


def rows_key(tables: Mapping[str, Sequence[Sequence[Any]]]) -> tuple[str | None, ...]:
    """Each table's rows as literal text, in order. Two databases with the same key hold the same values; a key
    holding ``None`` (a value with no plain literal) must not be compared."""

    return tuple(values_sql(rows) for rows in tables.values())


class TableLoader:
    """Replace the rows of tables on one connection, skipping the statements a search repeats.

    Random-database searches reload every table before each try, though a table often holds the same rows as
    on the try before (no rows, or one row from a small domain). A table whose rows are unchanged (the same
    literal text) is left as it is, and a table known to be empty gets no ``DELETE``. ``empty`` names tables
    the caller has just created; any other table's contents are unknown until it is loaded.
    """

    def __init__(self, db, empty: Iterable[str] = ()):
        self.db = db
        self._loaded: dict[str, str] = {name: "" for name in empty}

    def load(self, tables: Mapping[str, Sequence[Sequence[Any]]], key: tuple[str | None, ...] | None = None) -> None:
        """Give each table (quoted name -> rows) exactly these rows; ``key`` is ``rows_key(tables)`` if known."""

        for (table_sql, rows), values in zip(tables.items(), key if key is not None else rows_key(tables)):
            current = self._loaded.pop(table_sql, None)  # unknown until this load succeeds
            if values is not None and values == current:
                self._loaded[table_sql] = values
                continue
            if current != "":
                self.db.execute(f"DELETE FROM {table_sql}")
            _insert(self.db, table_sql, list(rows), values)
            if values is not None:
                self._loaded[table_sql] = values


def run_unoptimized(db, *queries: str) -> list[list[tuple]]:
    """Each query's rows with DuckDB's optimizer turned off (it is turned back on afterwards).

    DuckDB 1.5's optimizer returns wrong rows for some correlated subqueries, for example
    ``EXISTS (SELECT 1 FROM u WHERE u.d <> t.a AND t.b > u.c)`` when the tables hold NULLs. Searches
    that use DuckDB as the oracle re-run a difference this way and count it only when both runs agree,
    so an optimizer bug can neither refute an equivalent pair nor fail a correct proof.
    """

    db.execute("PRAGMA disable_optimizer")
    try:
        return [db.execute(query).fetchall() for query in queries]
    finally:
        db.execute("PRAGMA enable_optimizer")
