"""Replay a database on which two queries are claimed to differ: the judge of every synthesized refutation.

A counterexample from :mod:`kumosql.refutation_synthesis` (or from any other search) is reported
only after this judge confirms it, and the evals replay every reported counterexample through it
again. The judge loads the database into a fresh DuckDB connection with the declared column types
and runs both queries. It says the pair *differs* only when

* the database keeps every declared NOT NULL column, key and foreign key;
* both queries run (a query that fails, including a BigQuery guard firing, is no evidence);
* the result bags differ, also after rounding floats to 6 significant digits;
* DuckDB with its optimizer off returns the same bags (DuckDB 1.5's optimizer returns wrong rows
  for some correlated subqueries; see :func:`kumosql.duckdb_load.run_unoptimized`); and
* each query returns the same bag when every table's rows are stored reversed, rotated and
  shuffled, so the difference never rests on a tie broken by position or an arbitrary pick.

BigQuery SQL runs through :mod:`kumosql.bigquery_on_duckdb` (sqlglot spells out BigQuery's NULL order, ``NULLS FIRST`` ascending, against DuckDB's default; guards that
fail where BigQuery fails; results read as BigQuery returns them); a query with no faithful DuckDB
reading cannot be judged, so nothing is confirmed for it. DuckDB SQL runs as written. MySQL SQL
(the VeriEQL and Calcite-family evals) runs through :func:`kumosql.counterexample.to_duckdb`, the
translation those evals already use as their oracle.
"""

from __future__ import annotations

import math
import random
import re
from collections import Counter
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence

import sqlglot

from .duckdb_load import insert_rows, run_unoptimized


_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Verdict(str, Enum):
    DIFFERS = "differs"  # a confirmed, replayable difference
    SAME = "same"  # both queries return the same bag
    ERROR = "error"  # a query failed or cannot be run faithfully: no evidence either way
    UNSTABLE = "unstable"  # the difference depends on row order, an arbitrary pick or DuckDB's optimizer
    ILLEGAL = "illegal"  # the database breaks a declared NOT NULL column, key or foreign key


Rows = Mapping[str, Sequence[Sequence[Any]]]


def duckdb_type(declared: str, dialect: str) -> str:
    """The DuckDB column type for ``declared`` in ``dialect``."""

    if dialect == "duckdb":
        return declared
    if dialect == "mysql":
        from .counterexample import Column, _duck_type

        return _duck_type(Column("c", declared))
    from .bounded_equivalence import BColumn, _duck_type as bounded_type

    return bounded_type(BColumn("c", declared), bigquery=dialect == "bigquery")


def _normal(value: Any, digits: int | None) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float) and digits is not None and math.isfinite(value) and value != 0:
        return float(f"{value:.{digits}g}")
    if isinstance(value, Decimal) and digits is not None:
        return float(f"{float(value):.{digits}g}") if value else 0
    if isinstance(value, (list, tuple)):
        return tuple(_normal(v, digits) for v in value)
    return value


def bag(rows: Sequence[Sequence[Any]], digits: int | None = None) -> Counter:
    return Counter(tuple(_normal(v, digits) for v in row) for row in rows)


class Judge:
    """Both queries prepared once over one schema; :meth:`verdict` judges one database at a time.

    ``schema`` maps each table to its columns and declared types (in column order); ``keys``,
    ``not_null`` and ``foreign_keys`` are the declared facts (``foreign_keys`` as
    ``(table, columns, parent, parent_columns)``). Names compare case-insensitively.
    """

    def __init__(
        self,
        left: str,
        right: str,
        schema: Mapping[str, Mapping[str, str]],
        *,
        dialect: str = "bigquery",
        keys: Mapping[str, Sequence[Sequence[str]]] | None = None,
        not_null: Mapping[str, Sequence[str]] | None = None,
        foreign_keys: Sequence[tuple] = (),
        setup: str = "",
        shuffles: int = 3,
    ):
        import duckdb

        self.duckdb = duckdb
        self.dialect = dialect
        self.schema = {t: dict(cols) for t, cols in schema.items()}
        self.keys = {t.lower(): [tuple(c.lower() for c in k) for k in ks] for t, ks in (keys or {}).items()}
        self.not_null = {t.lower(): {c.lower() for c in cs} for t, cs in (not_null or {}).items()}
        self.foreign_keys = [
            (t.lower(), tuple(c.lower() for c in cs), p.lower(), tuple(c.lower() for c in pcs)) for t, cs, p, pcs in foreign_keys
        ]
        self.shuffles = shuffles
        self.problem: str | None = None
        # a table named ``project.dataset.table`` is created under a plain local name and the queries renamed to it
        self.local = {t: t if _PLAIN.fullmatch(t) else "src__" + re.sub(r"[^0-9A-Za-z_]", "_", t) for t in self.schema}
        self.db = duckdb.connect(":memory:")
        self.bigquery = dialect == "bigquery"
        try:
            self.left, self.right = self._translate(left), self._translate(right)
        except (sqlglot.errors.SqlglotError, RecursionError, ValueError) as error:
            self.problem = f"cannot translate: {error}"[:200]
            return
        if self.bigquery:
            from .bigquery_on_duckdb import configure

            configure(self.db)
        if setup:
            self.db.execute(setup)
        else:
            for table, columns in self.schema.items():
                spelled = ", ".join(f'"{c}" {duckdb_type(t, dialect)}' for c, t in columns.items())
                self.db.execute(f'CREATE TABLE "{self.local[table]}" ({spelled})')

    def _translate(self, sql: str) -> str:
        renamed = any(t != local for t, local in self.local.items())
        if self.dialect == "duckdb":
            return self._renamed(sqlglot.parse_one(sql, read="duckdb")).sql(dialect="duckdb") if renamed else sql
        if self.dialect == "bigquery":
            from .bigquery_on_duckdb import faithful

            return faithful(self._renamed(sqlglot.parse_one(sql, read="bigquery"))).sql(dialect="duckdb")
        from .counterexample import to_duckdb

        if renamed:
            sql = self._renamed(sqlglot.parse_one(sql, read=self.dialect)).sql(dialect=self.dialect)
        known = {c.lower() for cols in self.schema.values() for c in cols}
        return to_duckdb(sql, self.dialect, known)

    def _renamed(self, tree):
        """``tree`` reading each declared table under its local name (an unaliased table keeps its name as alias)."""

        from sqlglot import exp

        by_name = {t.lower(): t for t in self.schema}
        by_last: dict[str, list[str]] = {}
        for t in self.schema:
            by_last.setdefault(t.split(".")[-1].lower(), []).append(t)
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        for table in list(tree.find_all(exp.Table)):
            full = ".".join(p for p in (table.catalog, table.db, table.name) if p).lower()
            if not full or (not table.db and full in ctes):
                continue
            key = by_name.get(full) or (by_last[full][0] if len(by_last.get(full, ())) == 1 else None)
            if key is None or self.local[key] == key:
                continue
            if not table.alias:
                table.set("alias", exp.TableAlias(this=exp.to_identifier(table.name)))
            table.set("catalog", None)
            table.set("db", None)
            table.set("this", exp.to_identifier(self.local[key]))
        return tree

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Judge":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- data ---------------------------------------------------------------------------------------

    def legal(self, data: Rows) -> bool:
        index = {t.lower(): {c.lower(): i for i, c in enumerate(cols)} for t, cols in self.schema.items()}
        rows = {t.lower(): list(r) for t, r in data.items()}
        for table, columns in index.items():
            content = rows.get(table, [])
            required = self.not_null.get(table, set()) | {c for k in self.keys.get(table, ()) for c in k}
            if any(c not in columns for c in required):
                return False
            if any(row[columns[c]] is None for row in content for c in required):
                return False
            for key in self.keys.get(table, ()):
                seen = [tuple(row[columns[c]] for c in key) for row in content]
                if len(seen) != len(set(seen)):
                    return False
        for child, cols, parent, pcols in self.foreign_keys:
            if child not in index or parent not in index:
                return False
            ci, pi = index[child], index[parent]
            if any(c not in ci for c in cols) or any(c not in pi for c in pcols):
                return False
            present = {tuple(r[pi[c]] for c in pcols) for r in rows.get(parent, [])}
            for row in rows.get(child, []):
                values = tuple(row[ci[c]] for c in cols)
                if None not in values and values not in present:
                    return False
        return True

    def _load(self, data: Rows) -> None:
        by_lower = {t.lower(): r for t, r in data.items()}
        for table in self.schema:
            self.db.execute(f'DELETE FROM "{self.local[table]}"')
            insert_rows(self.db, f'"{self.local[table]}"', [tuple(r) for r in by_lower.get(table.lower(), [])])

    def _read(self, rows):
        if not self.bigquery:
            return rows
        from .bigquery_on_duckdb import UnfaithfulOutput, bigquery_rows

        try:
            return bigquery_rows(rows)
        except UnfaithfulOutput as error:
            raise self.duckdb.InvalidInputException(str(error)) from error

    def outputs(self, data: Rows) -> tuple[list, list]:
        """Both queries' rows on ``data``; raises a DuckDB error when either fails."""

        self._load(data)
        return self._read(self.db.execute(self.left).fetchall()), self._read(self.db.execute(self.right).fetchall())

    # -- the verdict --------------------------------------------------------------------------------

    def verdict(self, data: Rows) -> Verdict:
        if self.problem is not None:
            return Verdict.ERROR
        if not self.legal(data):
            return Verdict.ILLEGAL
        try:
            a, b = self.outputs(data)
            if bag(a) == bag(b) or bag(a, 6) == bag(b, 6):
                return Verdict.SAME
            expected = (bag(a), bag(b))
            unoptimized = run_unoptimized(self.db, self.left, self.right)
            if tuple(bag(self._read(rows)) for rows in unoptimized) != expected:
                return Verdict.UNSTABLE
            for shuffled in _reorders(data, random.Random(0), self.shuffles):
                a2, b2 = self.outputs(shuffled)
                if (bag(a2), bag(b2)) != expected:
                    return Verdict.UNSTABLE
        except self.duckdb.Error:
            return Verdict.ERROR
        except (OverflowError, ValueError, TypeError):
            return Verdict.ERROR
        return Verdict.DIFFERS


def _reorders(data: Rows, rng: random.Random, shuffles: int):
    yield {n: list(reversed(rows)) for n, rows in data.items()}
    yield {n: list(rows[1:]) + list(rows[:1]) for n, rows in data.items()}
    for _ in range(shuffles):
        yield {n: rng.sample(list(rows), len(rows)) for n, rows in data.items()}


def positional(tables: Mapping[str, Sequence[Any]], schema: Mapping[str, Mapping[str, str]]) -> dict[str, list[tuple]]:
    """Rows given as dicts (``{column: value}``, a :class:`~kumosql.smt_equivalence.Counterexample`'s
    form) or as positional lists, as positional tuples in the schema's column order."""

    by_lower = {t.lower(): t for t in schema}
    out: dict[str, list[tuple]] = {t: [] for t in schema}
    for name, rows in tables.items():
        table = by_lower.get(name.lower()) or by_lower.get(name.split(".")[-1].lower())
        if table is None:
            continue
        columns = list(schema[table])
        for row in rows:
            if isinstance(row, Mapping):
                lower = {k.lower(): v for k, v in row.items()}
                out[table].append(tuple(lower.get(c.lower()) for c in columns))
            else:
                out[table].append(tuple(row))
    return out


def _filler(declared: str, dialect: str, serial: int) -> Any:
    """A fresh non-NULL value of ``declared`` (distinct per ``serial``), or ``None`` when the type has no obvious one."""

    kind = duckdb_type(declared, dialect).upper()
    if kind.startswith(("TINYINT", "SMALLINT", "INT", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT")):
        return 1_000_000 + serial
    if kind.startswith(("VARCHAR", "TEXT", "STRING", "CHAR")):
        return f"filler{serial}"
    if kind.startswith(("DOUBLE", "FLOAT", "REAL", "DECIMAL", "NUMERIC")):
        return float(1_000_000 + serial)
    if kind.startswith("BOOL"):
        return bool(serial % 2)
    return None


def completed(
    tables: Mapping[str, Sequence[Any]],
    schema: Mapping[str, Mapping[str, str]],
    *,
    dialect: str,
    keys: Mapping[str, Sequence[Sequence[str]]] | None = None,
    not_null: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, list]:
    """``tables`` with the NOT NULL and key columns a dict row leaves out filled with fresh distinct values.

    A solver's model lists only the columns a query reads. A column it leaves out is free, so the
    database it describes can be completed in any legal way; the declared NOT NULL and key columns
    must get a value (a key a distinct one) for the completed database to be legal. Other missing
    columns stay NULL, and rows that are not dicts are left as they are.
    """

    by_lower = {t.lower(): t for t in schema}
    required = {t.lower(): {c.lower() for c in cs} for t, cs in (not_null or {}).items()}
    for t, ks in (keys or {}).items():
        required.setdefault(t.lower(), set()).update(c.lower() for k in ks for c in k)
    out: dict[str, list] = {}
    serial = 0
    for name, rows in tables.items():
        table = by_lower.get(name.lower()) or by_lower.get(name.split(".")[-1].lower())
        out[name] = list(rows)
        if table is None:
            continue
        needs = required.get(table.lower(), set())
        for i, row in enumerate(out[name]):
            if not isinstance(row, Mapping):
                continue
            row = dict(row)
            present = {k.lower() for k in row}
            for column, declared in schema[table].items():
                if column.lower() in needs and column.lower() not in present:
                    serial += 1
                    value = _filler(declared, dialect, serial)
                    if value is not None:
                        row[column] = value
            out[name][i] = row
    return out


def witness_differs(
    left: str,
    right: str,
    witness: Rows,
    *,
    schema: Mapping[str, Mapping[str, str]],
    dialect: str,
    setup: str = "",
    engine: str = "duckdb",
) -> bool:
    """Whether a case's own witness database separates the pair (DuckDB with the optimizer off and on,
    or SQLite as written for a pair DuckDB cannot run)."""

    if engine == "sqlite":
        import sqlite3

        db = sqlite3.connect(":memory:")
        try:
            return bag(db.execute(left).fetchall()) != bag(db.execute(right).fetchall())
        finally:
            db.close()
    with Judge(left, right, schema, dialect=dialect, setup=setup, shuffles=0) as judge:
        if judge.problem is not None:
            return False
        try:
            judge._load(positional(witness, schema))
            first = [bag(judge._read(r)) for r in run_unoptimized(judge.db, judge.left, judge.right)]
        except judge.duckdb.Error:
            return False
        return first[0] != first[1]


def replay_counterexample(
    left: str,
    right: str,
    counterexample: Any,
    *,
    schema: Mapping[str, Mapping[str, str]],
    dialect: str,
    keys: Mapping[str, Sequence[Sequence[str]]] | None = None,
    not_null: Mapping[str, Sequence[str]] | None = None,
    foreign_keys: Sequence[tuple] = (),
) -> bool:
    """Whether a reported counterexample (``.tables`` of dict rows, or a mapping of rows) separates the pair
    under the judge's rules."""

    tables = getattr(counterexample, "tables", counterexample)
    if not isinstance(tables, Mapping):
        return False
    with Judge(left, right, schema, dialect=dialect, keys=keys, not_null=not_null, foreign_keys=foreign_keys) as judge:
        data = completed(tables, schema, dialect=dialect, keys=keys, not_null=not_null)
        return judge.verdict(positional(data, schema)) is Verdict.DIFFERS
