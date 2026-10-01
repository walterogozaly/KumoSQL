"""Counterexample search: run two queries on small random databases and compare their results.

Given a schema with integrity constraints, ``find_counterexample`` builds many small
databases that satisfy every constraint, executes both queries on DuckDB, and returns
the first database on which the result bags differ. A returned counterexample is a
*refutation*: it was observed by running both queries, so it needs no trust in the
prover. Finding none proves nothing.

The generator draws values from small domains seeded with the literals of the two
queries (a constant, one below, one above), so predicates, joins and ties are hit
often; it honors NOT NULL, primary keys, foreign keys, ``CHECK``-style predicates
(a NULL never satisfies one, which is the strict reading and so valid under both),
consecutive-id columns, and cross-table implications.
"""

from __future__ import annotations

import datetime as _dt
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

import sqlglot
from sqlglot import exp

try:
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None


@dataclass
class Column:
    name: str
    type: str  # INT, VARCHAR, DATE, NUMERIC, TIME, BOOL, or ENUM
    not_null: bool = False
    values: tuple = ()  # the allowed values of an ENUM


@dataclass
class Table:
    name: str
    columns: list[Column]
    primary_key: tuple[str, ...] = ()
    unique: list[tuple[str, ...]] = field(default_factory=list)
    sequential: tuple[str, ...] = ()  # columns holding consecutive integers in row order

    def column(self, name: str) -> Column:
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(name)


@dataclass
class Check:
    """A predicate over the rows of ``tables``: ``test`` gets one row (a dict) per table."""

    tables: tuple[str, ...]
    test: Callable[..., bool | None]


@dataclass
class Spec:
    tables: dict[str, Table]
    foreign_keys: list[tuple[str, str, str, str]] = field(default_factory=list)  # child table, column, parent table, column
    checks: list[Check] = field(default_factory=list)


@dataclass(frozen=True)
class Counterexample:
    tables: dict[str, list[tuple]]
    left_rows: list[tuple]
    right_rows: list[tuple]

    def script(self, spec: Spec) -> str:
        """A SQL script that recreates the database."""

        lines = []
        for name, rows in self.tables.items():
            table = spec.tables[name]
            lines.append(f"CREATE TABLE {name} ({', '.join(f'{c.name} {c.type}' for c in table.columns)});")
            for row in rows:
                lines.append(f"INSERT INTO {name} VALUES ({', '.join(_literal(v) for v in row)});")
        return "\n".join(lines)


def _literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


# --- domains -----------------------------------------------------------------------------

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")


@dataclass
class Constants:
    ints: set = field(default_factory=set)
    strings: set = field(default_factory=set)
    dates: set = field(default_factory=set)
    times: set = field(default_factory=set)


def constants_of(*queries: str, dialect: str = "mysql") -> Constants:
    found = Constants()
    for query in queries:
        try:
            tree = sqlglot.parse_one(query, read=dialect)
        except sqlglot.errors.SqlglotError:
            continue
        for literal in tree.find_all(exp.Literal):
            text = literal.this
            if literal.is_string:
                if _DATE.match(text):
                    found.dates.add(text)
                elif _TIME.match(text):
                    found.times.add(text if len(text) > 5 else text + ":00")
                else:
                    stripped = text.strip("%")
                    found.strings.update({text, stripped} - {""})
            else:
                try:
                    found.ints.add(int(float(text)))
                except ValueError:
                    pass
    return found


def _domain(column: Column, constants: Constants) -> list:
    kind = column.type.split("(")[0].upper()
    if kind == "ENUM":
        return list(column.values)
    if kind in ("INT", "INTEGER", "BIGINT", "SMALLINT"):
        base = {0, 1, 2, 3}
        for n in sorted(constants.ints)[:12]:
            base.update({n - 1, n, n + 1})
        return sorted(base)
    if kind in ("NUMERIC", "DECIMAL", "FLOAT", "DOUBLE"):
        base = {0, 1, 2, 3, 0.5, 1.5}
        for n in sorted(constants.ints)[:12]:
            base.update({n - 1, n, n + 1})
        return sorted(base)
    if kind == "BOOL" or kind == "BOOLEAN":
        return [True, False]
    if kind == "DATE":
        base = {"2020-01-01", "2020-01-02", "2020-01-03", "2021-01-01"}
        for text in sorted(constants.dates)[:8]:
            day = _dt.date.fromisoformat(text)
            base.update({(day + _dt.timedelta(days=d)).isoformat() for d in (-1, 0, 1)})
        return sorted(base)
    if kind == "TIME":
        return sorted({"00:00:00", "08:00:00", "12:00:00", "23:59:59"} | constants.times)
    base = {"a", "b", "c"}
    base.update(sorted(constants.strings)[:12])
    return sorted(base)


def _duck_type(column: Column) -> str:
    kind = column.type.split("(")[0].upper()
    return {
        "INT": "BIGINT", "INTEGER": "BIGINT", "BIGINT": "BIGINT", "SMALLINT": "BIGINT",
        "NUMERIC": "DOUBLE", "DECIMAL": "DOUBLE", "FLOAT": "DOUBLE", "DOUBLE": "DOUBLE",
        "BOOL": "BOOLEAN", "BOOLEAN": "BOOLEAN", "DATE": "DATE", "TIME": "TIME",
    }.get(kind, "VARCHAR")


# --- database generation -----------------------------------------------------------------


class _NoRow(Exception):
    pass


class _Generator:
    def __init__(self, spec: Spec, constants: Constants, rng: random.Random):
        self.spec, self.rng = spec, rng
        self.domains = {
            (t.name, c.name): _domain(c, constants) for t in spec.tables.values() for c in t.columns
        }
        self.order = self._table_order()
        self.single_checks: dict[str, list[Check]] = {}
        self.global_checks: list[Check] = []
        for check in spec.checks:
            if len(set(check.tables)) == 1:
                self.single_checks.setdefault(check.tables[0], []).append(check)
            else:
                self.global_checks.append(check)

    def _table_order(self) -> list[str]:
        parents = {name: set() for name in self.spec.tables}
        for child, _, parent, _ in self.spec.foreign_keys:
            if child != parent:
                parents[child].add(parent)
        order, seen = [], set()

        def visit(name, stack=()):
            if name in seen or name in stack:
                return
            for parent in sorted(parents[name]):
                visit(parent, stack + (name,))
            seen.add(name)
            order.append(name)

        for name in self.spec.tables:
            visit(name)
        return order

    def database(self, used: set[str], max_rows: int) -> dict[str, list[tuple]] | None:
        db: dict[str, list[tuple]] = {}
        for name in self.order:
            if name not in used:
                db[name] = []
                continue
            db[name] = self._rows(self.spec.tables[name], db, max_rows)
        for check in self.global_checks:
            if not all(t in used for t in check.tables):
                continue
            if not self._holds(check, db):
                return None
        return db

    def _holds(self, check: Check, db) -> bool:
        tables = [self.spec.tables[t] for t in check.tables]

        def rows_of(i):
            return [dict(zip((c.name for c in tables[i].columns), row)) for row in db[tables[i].name]]

        def go(i, picked):
            if i == len(tables):
                return check.test(*picked) is True
            return all(go(i + 1, picked + [row]) for row in rows_of(i))

        return go(0, [])

    def _rows(self, table: Table, db, max_rows: int) -> list[tuple]:
        rng = self.rng
        count = rng.choice([0, 1, 1, 2, 2, 3, 3, 4, 5][: 3 + max_rows * 2])
        count = min(count, max_rows)
        # a hot subset of each domain makes repeated values (ties, join matches) common
        hot = {}
        for column in table.columns:
            domain = self.domains[(table.name, column.name)]
            hot[column.name] = rng.sample(domain, min(len(domain), rng.choice([1, 2, 3]))) if domain else [None]
        keys = ([table.primary_key] if table.primary_key else []) + list(table.unique)
        fk_of = {c: (p, pc) for ch, c, p, pc in self.spec.foreign_keys if ch == table.name}
        taken = {i: set() for i in range(len(keys))}
        rows: list[tuple] = []
        for index in range(count):
            for _ in range(25):
                row = {}
                try:
                    for column in table.columns:
                        row[column.name] = self._value(table, column, hot, fk_of, db, index)
                except _NoRow:
                    return rows
                good = True
                for i, key in enumerate(keys):
                    value = tuple(row[k] for k in key)
                    if None not in value and value in taken[i]:
                        good = False
                if good and all(c.test(row) is True for c in self.single_checks.get(table.name, [])):
                    for i, key in enumerate(keys):
                        taken[i].add(tuple(row[k] for k in key))
                    rows.append(tuple(row[c.name] for c in table.columns))
                    break
        return rows

    def _value(self, table: Table, column: Column, hot, fk_of, db, index):
        rng = self.rng
        if column.name in table.sequential:
            return index + 1
        if column.name in fk_of:
            parent, parent_column = fk_of[column.name]
            parent_table = self.spec.tables[parent]
            position = [c.name for c in parent_table.columns].index(parent_column)
            values = [row[position] for row in db.get(parent, []) if row[position] is not None]
            if values:
                return rng.choice(values)
            raise _NoRow  # no parent row to point at; a NULL reference is not generated (the strict reading)
        if not column.not_null and column.name not in table.primary_key and rng.random() < 0.2:
            return None
        pool = hot[column.name] if rng.random() < 0.8 else self.domains[(table.name, column.name)]
        return rng.choice(pool)


# --- running -----------------------------------------------------------------------------


def _norm(value):
    if isinstance(value, float):
        return round(value, 6)
    if hasattr(value, "is_finite"):  # Decimal
        return round(float(value), 6)
    return value


def _bag(rows) -> Counter:
    return Counter(tuple(_norm(v) for v in row) for row in rows)


def to_duckdb(sql: str, dialect: str = "mysql") -> str:
    return sqlglot.transpile(sql, read=dialect, write="duckdb")[0]


class Searcher:
    """Reusable search over one schema: parse once, then try many databases."""

    def __init__(self, spec: Spec, left: str, right: str, *, dialect: str = "mysql"):
        self.spec = spec
        self.constants = constants_of(left, right, dialect=dialect)
        self.left_sql = to_duckdb(left, dialect)
        self.right_sql = to_duckdb(right, dialect)
        names = set()
        for sql in (left, right):
            for table in sqlglot.parse_one(sql, read=dialect).find_all(exp.Table):
                names.add(table.name.lower())
        by_lower = {n.lower(): n for n in spec.tables}
        self.used = {by_lower[n] for n in names if n in by_lower}
        self.db = duckdb.connect(":memory:")
        for name in self.used:
            table = spec.tables[name]
            columns = ", ".join(f'"{c.name}" {_duck_type(c)}' for c in table.columns)
            self.db.execute(f'CREATE TABLE "{name}" ({columns})')

    def runs(self) -> bool:
        """Whether DuckDB accepts both queries on an empty database."""

        try:
            self.db.execute(self.left_sql).fetchall()
            self.db.execute(self.right_sql).fetchall()
        except duckdb.Error:
            return False
        return True

    def search(self, trials: int = 150, seed: int = 0) -> Counterexample | None:
        rng = random.Random(seed)
        generator = _Generator(self.spec, self.constants, rng)
        for trial in range(trials):
            max_rows = 2 if trial < trials // 3 else 3 if trial < 2 * trials // 3 else 5
            data = generator.database(self.used, max_rows)
            if data is None:
                continue
            try:
                for name in self.used:
                    table = self.spec.tables[name]
                    self.db.execute(f'DELETE FROM "{name}"')
                    if data[name]:
                        marks = ", ".join("?" * len(table.columns))
                        self.db.executemany(f'INSERT INTO "{name}" VALUES ({marks})', data[name])
                a = self.db.execute(self.left_sql).fetchall()
                b = self.db.execute(self.right_sql).fetchall()
            except duckdb.Error:
                return None
            if _bag(a) != _bag(b):
                return Counterexample({n: data[n] for n in self.used}, a, b)
        return None


def find_counterexample(spec: Spec, left: str, right: str, *, dialect: str = "mysql", trials: int = 150, seed: int = 0):
    """A database on which the queries differ, ``None`` if none was found, ``False`` if DuckDB rejects a query."""

    try:
        searcher = Searcher(spec, left, right, dialect=dialect)
    except (sqlglot.errors.SqlglotError, duckdb.Error):
        return False
    if not searcher.runs():
        return False
    return searcher.search(trials, seed)
