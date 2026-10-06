"""Rule-level differential soundness harness for the algebraic normalizer.

``prove_equivalent_algebraic`` is only as sound as every rewrite ``algebraic_equivalence.normalize`` applies: a
proof compares normal forms, so one unsound rewrite anywhere is a false proof waiting for the right pair. This
tool checks the rewrites one at a time, on the inputs they actually fire on:

1. it wraps every rewrite ``normalize`` calls (the whole-tree passes, the per-node ``step`` rules, the fixpoint
   passes and the final canonicalization), runs ``normalize`` on a corpus of queries, and records each firing as
   the whole query just before and just after that rule;
2. it runs both whole queries on DuckDB databases built for the query's typed schema and declared constraints
   (seeded random ones, NULL-heavy, duplicate-heavy, empty, one-row, tie-heavy and large-integer ones, using the
   query's own constants); a difference counts only when DuckDB's unoptimized run agrees
   (``kumosql.duckdb_load.run_unoptimized``, see #347) and both sides give the same bag with every table's rows
   reversed (no dependence on row order or ties);
3. it reduces each confirmed difference to a small witness (query, then rows) while the same rule still fires
   and still changes the result.

Because a firing is checked on the exact tree the rule saw, a difference is a bug in that rule and nowhere
else. Runtime errors are not modeled by the prover (``BASE_ASSUMPTIONS``), so a firing where either side fails
is "unchecked", never a bug. Result column types are not compared, so ``3`` and ``3.0`` are equal; numbers are
otherwise exact (an integer and a float must have the same value), and two non-integral floats are compared to
10 significant digits (summation order).

    python tools/rule_fuzz.py run --corpus gen --seed 1 --count 2000 --jobs 4 --out run.json
    python tools/rule_fuzz.py run --corpus evals --jobs 4 --out evals.json
    python tools/rule_fuzz.py report run.json                 # fired / checked / bugs per rule
    python tools/rule_fuzz.py show run.json --rule distinct_rules
    python tools/rule_fuzz.py query "SELECT ..." --schema t:id=INT64,x=INT64 --key t:id

Corpora: ``gen`` (a typed random query generator biased toward the edge cases behind past false proofs:
empty tables, NULLs, duplicates, ties, grouping sets, DISTINCT ON, correlated and shadowed scopes, set-operation
tails, mixed numeric types), ``fuzz`` (both sides of ``tools/soundness_fuzz.py`` template pairs, when that tool
is present) and ``evals`` (the SQLSolver-family eval queries with their schemas: SQLSolver Calcite, Spark,
TPC-H and TPC-C, QED, R-Bot and the mined Calcite pairs; held-out pairs are never read).
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import datetime as _dt
from decimal import Decimal
from fractions import Fraction
import functools
import inspect
import json
import math
import os
from pathlib import Path
import queue
import random
import subprocess
import sys
import threading
import time
import traceback

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

KNOWN_PATH = ROOT / "tests" / "fixtures" / "rule_fuzz" / "known_rule_bugs.json"

# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------
#
# A case is a JSON-safe dict:
#   sql          the query
#   dialect      sqlglot dialect it is written in (default bigquery)
#   schema       {table: [[column, TYPE], ...]}  BigQuery type names (INT64, FLOAT64, NUMERIC, STRING, BOOL, DATE,
#                TIMESTAMP, DATETIME)
#   constraints  {table: {"not_null": [...], "keys": [[...]], "foreign_keys": [[[cols], parent, [cols]]]}}
#   options      {"group_by_constants": bool, "pass_schema": bool}
#   source       where it came from (corpus / eval / generator family)

SUPPORTED_TYPES = {"INT64", "FLOAT64", "NUMERIC", "STRING", "BOOL", "DATE", "TIMESTAMP", "DATETIME"}


def _type_name(raw: str) -> str | None:
    """A BigQuery type name for a declared type, or ``None`` when the harness cannot generate its values."""

    text = (raw or "").strip().upper()
    base = text.split("(")[0].strip()
    aliases = {
        "INT64": "INT64", "INT": "INT64", "INTEGER": "INT64", "BIGINT": "INT64", "SMALLINT": "INT64", "TINYINT": "INT64",
        "FLOAT64": "FLOAT64", "FLOAT": "FLOAT64", "DOUBLE": "FLOAT64", "REAL": "FLOAT64", "DOUBLE PRECISION": "FLOAT64",
        "NUMERIC": "NUMERIC", "DECIMAL": "NUMERIC", "BIGNUMERIC": None,
        "STRING": "STRING", "VARCHAR": "STRING", "CHAR": "STRING", "TEXT": "STRING",
        "BOOL": "BOOL", "BOOLEAN": "BOOL", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "DATETIME": "DATETIME",
    }
    return aliases.get(base)


def _required(rules: dict) -> set:
    required = {c.lower() for c in rules.get("not_null", [])}
    for key in rules.get("keys", []):
        required.update(c.lower() for c in key)  # KumoSQL keys are NOT NULL unique keys
    return required


def query_constants(sql: str, dialect: str) -> dict[str, set]:
    """Numbers and strings written in ``sql``: databases use them (and integer neighbours) as column values."""

    found: dict[str, set] = {"INT64": set(), "FLOAT64": set(), "STRING": set()}
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return found
    for literal in tree.find_all(exp.Literal):
        if literal.is_string:
            if len(literal.this) <= 12 and literal.this.isascii() and "\\" not in literal.this:
                found["STRING"].add(literal.this)
            continue
        try:
            value = Fraction(literal.this)
        except (ValueError, ZeroDivisionError):
            continue
        if value.denominator == 1 and abs(value) < 10**6:
            found["INT64"].add(int(value))
        elif abs(value) < 10**6:
            found["FLOAT64"].add(float(value))
    return found


# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------

_BASE_VALUES = {
    "INT64": [-2, -1, 0, 1, 2, 3],
    "FLOAT64": [-1.5, -1.0, 0.0, 0.5, 1.0, 2.0, 0.1],
    "NUMERIC": [Decimal("-1.5"), Decimal("0"), Decimal("0.1"), Decimal("1"), Decimal("2.25")],
    "STRING": ["", "a", "b", "A", "ab", "a "],
    "BOOL": [True, False],
    "DATE": [_dt.date(2023, 12, 31), _dt.date(2024, 1, 1), _dt.date(2024, 1, 31), _dt.date(2024, 2, 29), _dt.date(2024, 3, 1)],
    "TIMESTAMP": [_dt.datetime(2024, 1, 1, 0, 0), _dt.datetime(2024, 1, 1, 12, 30), _dt.datetime(2024, 2, 29, 23, 59, 59)],
    "DATETIME": [_dt.datetime(2024, 1, 1, 0, 0), _dt.datetime(2024, 1, 1, 12, 30), _dt.datetime(2024, 2, 29, 23, 59, 59)],
}
_LARGE_INTS = [2**53, 2**53 + 1, -(2**53) - 1, 2**62, 1, 0]


def _domain(kind: str, constants: dict[str, set]) -> list:
    values = list(_BASE_VALUES[kind])
    if kind == "INT64":
        values += sorted({c + d for c in constants.get("INT64", ()) for d in (-1, 0, 1) if abs(c + d) < 10**6})
    elif kind == "FLOAT64":
        values += sorted(constants.get("FLOAT64", ())) + [float(c) for c in sorted(constants.get("INT64", ()))[:4]]
    elif kind == "STRING":
        values += sorted(constants.get("STRING", ()))
    return list(dict.fromkeys(values))


def _fresh_key_values(kind: str, count: int, rng: random.Random, mode: str) -> list:
    if kind == "INT64":
        pool = list(range(-2, max(6, count + 3)))
        if mode == "large":
            pool = _LARGE_INTS + pool
        return rng.sample(pool, count)
    if kind == "STRING":
        pool = ["", "a", "b", "c", "A", "ab", "ba", "z"] + [f"k{i}" for i in range(count)]
        return rng.sample(pool, count)
    pool = list(dict.fromkeys(_BASE_VALUES[kind] + ([Decimal(i) for i in range(count)] if kind == "NUMERIC" else [float(i) for i in range(count)] if kind == "FLOAT64" else [])))
    if len(pool) < count:
        return []
    return rng.sample(pool, count)


def build_databases(case: dict, seed: int = 0, randoms: int = 6) -> list[dict]:
    """Legal databases for ``case``: ``[{"name": ..., "tables": {table: [row, ...]}}]``."""

    schema = case["schema"]
    rules_by = {t.lower(): r for t, r in case.get("constraints", {}).items()}
    constants = query_constants(case["sql"], case.get("dialect", "bigquery"))
    rng = random.Random(seed * 7919 + len(case["sql"]))
    modes = [f"random_{i}" for i in range(randoms)] + ["null_heavy", "duplicates", "empty", "one_row", "ties", "large", "distinct"]
    if len(schema) > 1:
        # one table empty (or holding one row) while the others are not: global aggregates, outer joins, NOT IN
        modes += [f"empty:{t}" for t in schema] + [f"one_row:{t}" for t in schema]
    out = []
    for mode in modes:
        tables = {}
        for table, columns in schema.items():
            rules = rules_by.get(table.lower(), {})
            required = _required(rules)
            if mode == "empty" or mode == f"empty:{table}":
                count = 0
            elif mode == "one_row" or mode == f"one_row:{table}":
                count = 1
            elif mode in ("null_heavy", "duplicates", "ties"):
                count = 4
            elif mode == "distinct":
                count = 5
            else:
                count = rng.choice([0, 1, 2, 3, 3, 4, 5, 6])
            names = [c[0].lower() for c in columns]
            keyed = {}
            for key in rules.get("keys", []):
                for column in key:
                    index = names.index(column.lower()) if column.lower() in names else None
                    if index is not None and column.lower() not in keyed:
                        values = _fresh_key_values(columns[index][1], count, rng, mode)
                        if len(values) < count:
                            count = len(values)
                        keyed[column.lower()] = values
            rows = []
            tie_values = {name: rng.choice(_domain(kind, constants)) for name, kind in columns}
            for i in range(count):
                row = []
                for name, kind in columns:
                    lname = name.lower()
                    domain = _domain(kind, constants)
                    if lname in keyed:
                        value = keyed[lname][i]
                    elif mode == "null_heavy" and lname not in required:
                        value = None
                    elif mode == "duplicates":
                        value = domain[1 % len(domain)]
                    elif mode == "ties":
                        value = tie_values[name] if rng.random() < 0.75 else rng.choice(domain)
                    elif mode == "distinct":
                        value = domain[i % len(domain)]
                    elif mode == "large" and kind == "INT64":
                        value = rng.choice(_LARGE_INTS)
                    else:
                        value = rng.choice(domain + ([None, None] if lname not in required else []))
                    if value is None and lname in required:
                        value = domain[0]
                    row.append(value)
                rows.append(row)
            if mode == "duplicates" and rows and not rules.get("keys"):
                rows = [list(rows[0]) for _ in rows]
            # composite keys: drop rows that repeat a key
            for key in rules.get("keys", []):
                idx = [names.index(c.lower()) for c in key if c.lower() in names]
                seen, kept = set(), []
                for row in rows:
                    value = tuple(row[i] for i in idx)
                    if value not in seen:
                        seen.add(value)
                        kept.append(row)
                rows = kept
            tables[table] = rows
        # MATCH SIMPLE foreign keys: a child row with every column non-NULL points at an existing parent
        for table, columns in schema.items():
            rules = rules_by.get(table.lower(), {})
            names = [c[0].lower() for c in columns]
            for child, parent, parent_cols in rules.get("foreign_keys", []):
                ptable = next((t for t in schema if t.lower() == parent.lower()), None)
                if ptable is None:
                    continue
                pnames = [c[0].lower() for c in schema[ptable]]
                parents = [tuple(r[pnames.index(c.lower())] for c in parent_cols) for r in tables[ptable]]
                required = _required(rules)
                kept = []
                for row in tables[table]:
                    values = [row[names.index(c.lower())] for c in child]
                    if any(v is None for v in values):
                        kept.append(row)
                    elif parents:
                        for c, v in zip(child, rng.choice(parents)):
                            row[names.index(c.lower())] = v
                        kept.append(row)
                    elif not any(c.lower() in required for c in child):
                        for c in child:
                            row[names.index(c.lower())] = None
                        kept.append(row)
                    # else: no parent to point at and the child columns are NOT NULL: drop the row
                tables[table] = kept
        out.append({"name": mode, "tables": tables})
    return out


def fixture_errors(case: dict, tables: dict) -> list[str]:
    """Key uniqueness, NOT NULL and foreign keys of ``tables`` (the generator's own guard)."""

    errors = []
    rules_by = {t.lower(): r for t, r in case.get("constraints", {}).items()}
    for table, columns in case["schema"].items():
        rules = rules_by.get(table.lower(), {})
        names = [c[0].lower() for c in columns]
        for row in tables.get(table, []):
            for name in _required(rules):
                if name in names and row[names.index(name)] is None:
                    errors.append(f"{table}.{name} NULL")
        for key in rules.get("keys", []):
            idx = [names.index(c.lower()) for c in key if c.lower() in names]
            values = [tuple(r[i] for i in idx) for r in tables.get(table, [])]
            if len(values) != len(set(values)):
                errors.append(f"{table} duplicate key")
        for child, parent, parent_cols in rules.get("foreign_keys", []):
            ptable = next((t for t in case["schema"] if t.lower() == parent.lower()), None)
            if ptable is None:
                continue
            pnames = [c[0].lower() for c in case["schema"][ptable]]
            parents = {tuple(r[pnames.index(c.lower())] for c in parent_cols) for r in tables[ptable]}
            for row in tables.get(table, []):
                value = tuple(row[names.index(c.lower())] for c in child)
                if None not in value and value not in parents:
                    errors.append(f"{table} FK violation")
    return errors


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------


class Unsupported(ValueError):
    """The query is outside what the DuckDB oracle runs with BigQuery's meaning."""


_DENIED = {
    # values or semantics that differ between BigQuery and DuckDB, or are nondeterministic by design
    "Rand", "Uuid", "AnyValue", "ArrayAgg", "Struct", "JSONExtract", "JSONExtractScalar", "ParseJSON", "RegexpExtract",
    "RegexpReplace", "RegexpLike", "Format", "StrToTime", "StrToDate", "TimeToStr", "UnixToTime", "Explode",
    "GenerateSeries", "GenerateDateArray", "TableSample", "Pivot", "Unpivot", "Collate", "Hll",
    "ApproxDistinct", "ApproxQuantile", "Quantile", "PercentileCont", "PercentileDisc", "Stddev", "StddevPop",
    "StddevSamp", "Variance", "VariancePop", "Corr", "CovarPop", "CovarSamp", "Bytes", "Unhex", "MD5", "SHA", "SHA2",
    "Initcap", "ArrayToString", "Split", "StringToArray", "Repeat", "Lpad", "Rpad", "Translate", "Soundex",
    "CurrentTimestamp", "CurrentDate", "CurrentDatetime", "CurrentTime",
}
_DENIED_EXTRACT_UNITS = {"DAYOFWEEK", "DOW", "WEEK", "ISOWEEK", "ISOYEAR", "DAYOFYEAR", "DOY", "QUARTER"}
_OPERATORS = (exp.Binary, exp.Not, exp.Between, exp.In, exp.Like, exp.Neg, exp.Is)


def to_duckdb(tree: exp.Expression, dialect: str = "bigquery") -> str:
    """DuckDB SQL with ``tree``'s meaning, or :class:`Unsupported`."""

    tree = tree.copy()
    if not isinstance(tree, exp.Query):
        raise Unsupported("not a query")
    for node in list(tree.walk()):
        name = type(node).__name__
        if name in _DENIED:
            raise Unsupported(name)
        if isinstance(node, exp.Anonymous):
            fname = node.name.upper()
            if fname in ("ERROR",):
                continue
            raise Unsupported(f"function {fname}")
        if isinstance(node, exp.Extract) and node.this is not None and node.this.name.upper() in _DENIED_EXTRACT_UNITS:
            raise Unsupported(f"EXTRACT {node.this.name}")
        if isinstance(node, exp.DataType) and node.this in (exp.DataType.Type.DECIMAL, exp.DataType.Type.BIGDECIMAL) and not node.expressions:
            node.replace(exp.DataType.build("DECIMAL(38, 9)"))
        if isinstance(node, exp.Table) and (node.args.get("db") or node.args.get("catalog")):
            node.set("db", None)
            node.set("catalog", None)
        if isinstance(node, exp.Literal) and node.is_string and "\\" in node.this:
            raise Unsupported("backslash in a string literal (escapes differ)")
        if isinstance(node, exp.Cast) and node.to.this in (exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR):
            source = node.this
            if not isinstance(source, (exp.Column, exp.Literal, exp.Null)) or (isinstance(source, exp.Literal) and not source.is_string and "." in source.this):
                raise Unsupported("CAST to STRING of a computed value (number formatting differs)")
    # BigQuery fails on a zero divisor where DuckDB returns NULL or infinity; DATE + INTERVAL is a TIMESTAMP in DuckDB;
    # DuckDB's count_if is NULL over no rows or only NULLs where BigQuery's COUNTIF is 0
    def guard(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.CountIf):
            return exp.Count(this=exp.Case(ifs=[exp.If(this=node.this, true=exp.Literal.number(1))]))
        if isinstance(node, (exp.Div, exp.IntDiv, exp.Mod)) and not node.args.get("safe"):
            zero = exp.EQ(this=exp.Paren(this=node.expression.copy()), expression=exp.Literal.number(0))
            error = exp.Anonymous(this="ERROR", expressions=[exp.Literal.string("division by zero")])
            return exp.If(this=zero, true=error, false=node)
        if isinstance(node, (exp.DateAdd, exp.DateSub)) and not isinstance(node.parent, exp.Cast):
            return exp.Cast(this=node, to=exp.DataType.build("DATE"))
        return node

    tree = tree.transform(guard, copy=False)
    # a set operation operand that is itself a set operation, or carries ORDER BY/LIMIT, prints without parentheses
    for node in list(tree.find_all(exp.SetOperation)):
        for side in ("this", "expression"):
            child = node.args.get(side)
            if isinstance(child, exp.SetOperation) or (isinstance(child, exp.Select) and any(child.args.get(k) for k in ("order", "limit", "offset"))):
                child.replace(exp.Subquery(this=child.copy()))
    # sqlglot writes operators with its own precedence, which is not DuckDB's (``NOT y IS NULL IS NULL``)
    for node in list(tree.find_all(*_OPERATORS)):
        for arg in ("this", "expression"):
            operand = node.args.get(arg)
            if isinstance(operand, _OPERATORS) and not isinstance(operand, exp.Paren):
                paren = exp.Paren()
                operand.replace(paren)
                paren.set("this", operand)
    try:
        return tree.sql(dialect="duckdb", unsupported_level=sqlglot.ErrorLevel.RAISE)
    except (sqlglot.errors.SqlglotError, ValueError) as error:
        raise Unsupported(f"serialization: {error}") from error


def canonical_value(value):
    """Exact numbers (``3`` equals ``3.0``), floats to 10 significant digits, dates as ISO text."""

    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("n", Fraction(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise Unsupported("non-finite decimal")
        return ("n", Fraction(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise Unsupported("non-finite float")
        if value == int(value):
            return ("n", Fraction(int(value)))
        rounded = float(f"{value:.10g}")
        if rounded == int(rounded):
            return ("n", Fraction(int(rounded)))
        return ("f", rounded)
    if isinstance(value, _dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        if value.time() == _dt.time(0):
            return ("date", value.date().isoformat())
        return ("datetime", value.isoformat())
    if isinstance(value, _dt.date):
        return ("date", value.isoformat())
    if isinstance(value, str):
        return ("s", value)
    if isinstance(value, (list, tuple)):
        return ("list", tuple(canonical_value(v) for v in value))
    if isinstance(value, dict):
        return ("struct", tuple((k, canonical_value(v)) for k, v in value.items()))
    return ("other", repr(value))


def bag(rows) -> Counter:
    return Counter(tuple(canonical_value(v) for v in row) for row in rows)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


_DUCK_TYPES = {"INT64": "BIGINT", "FLOAT64": "DOUBLE", "NUMERIC": "DECIMAL(38, 9)", "STRING": "VARCHAR", "BOOL": "BOOLEAN", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "DATETIME": "TIMESTAMP"}


def _tie_variants(sql: str) -> tuple[str, str] | None:
    """``sql`` with every LIMIT/OFFSET query ordered by all its output columns ascending, and descending; ``None`` without one."""

    tree = sqlglot.parse_one(sql, read="duckdb")
    nodes = [n for n in tree.walk() if isinstance(n, (exp.Select, exp.SetOperation)) and (n.args.get("limit") or n.args.get("offset"))]
    if not nodes:
        return None
    variants = []
    for descending in (False, True):
        copy = tree.copy()
        targets = [n for n in copy.walk() if isinstance(n, (exp.Select, exp.SetOperation)) and (n.args.get("limit") or n.args.get("offset"))]
        for node in targets:
            leftmost = node
            while isinstance(leftmost, exp.SetOperation):
                leftmost = leftmost.this
            while isinstance(leftmost, exp.Subquery):
                leftmost = leftmost.this
            if not isinstance(leftmost, exp.Select):
                return None
            if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in leftmost.expressions):
                return None
            keys = [exp.Ordered(this=exp.Literal.number(i + 1), desc=descending) for i in range(len(leftmost.expressions))]
            order = node.args.get("order")
            if order is None:
                node.set("order", exp.Order(expressions=keys))
            else:
                order.set("expressions", list(order.expressions) + keys)
        variants.append(copy.sql(dialect="duckdb"))
    return variants[0], variants[1]


class Oracle:
    """One DuckDB connection holding every database of a case, one schema each (plus row-reversed copies)."""

    def __init__(self, case: dict, databases: list[dict], query_seconds: float = 5.0):
        import duckdb

        self.duckdb = duckdb
        self.case = case
        self.databases = databases
        self.query_seconds = query_seconds
        self.connection = duckdb.connect(":memory:")
        self.connection.execute("SET threads=1")
        self.connection.execute("SET memory_limit='512MB'")
        self.loaded: set = set()

    def close(self):
        self.connection.close()

    def _load(self, index: int, reverse: bool = False) -> str:
        from kumosql.duckdb_load import insert_rows

        name = f"db{index}{'r' if reverse else ''}"
        if name in self.loaded:
            return name
        self.connection.execute(f"CREATE SCHEMA {name}")
        for table, columns in self.case["schema"].items():
            ddl = ", ".join(f"{_quote(c.lower())} {_DUCK_TYPES[_type_name(t)]}" for c, t in columns)
            self.connection.execute(f"CREATE TABLE {name}.{_quote(table.lower())} ({ddl})")
            rows = self.databases[index]["tables"].get(table, [])
            insert_rows(self.connection, f"{name}.{_quote(table.lower())}", list(reversed(rows)) if reverse else rows)
        self.loaded.add(name)
        return name

    def _run(self, schema_name: str, sql: str, optimized: bool = True):
        """Rows, or an exception instance when the query fails or times out."""

        self.connection.execute(f"SET schema = '{schema_name}'")
        timer = threading.Timer(self.query_seconds, self.connection.interrupt)
        timer.start()
        try:
            if not optimized:
                self.connection.execute("PRAGMA disable_optimizer")
            try:
                return self.connection.execute(sql).fetchmany(5001)
            finally:
                if not optimized:
                    self.connection.execute("PRAGMA enable_optimizer")
        except Exception as error:  # noqa: BLE001 - binding, typing, runtime errors and interrupts are not evidence
            return error
        finally:
            timer.cancel()

    def _column_types(self, schema_name: str, sql: str):
        self.connection.execute(f"SET schema = '{schema_name}'")
        try:
            return [row[1] for row in self.connection.execute(f"DESCRIBE {sql}").fetchall()]
        except Exception:  # noqa: BLE001
            return None

    def _tie_free(self, schema_name: str, sql: str, rows) -> bool:
        """Whether the rows ``sql`` returns do not depend on how its LIMITs break ties.

        Every ORDER BY that feeds a LIMIT or OFFSET is extended by all output columns, ascending and then
        descending; ties among whole rows then no longer matter. The query's own rows must equal both, or its
        cut depended on how ties were broken.
        """

        variants = _tie_variants(sql)
        if variants is None:
            return True
        ran = [self._run(schema_name, variant) for variant in variants]
        if any(isinstance(r, Exception) for r in ran):
            return False
        return bag(ran[0]) == bag(ran[1]) == bag(rows)

    def compare(self, before_sql: str, after_sql: str, stop_at_first: bool = True) -> dict:
        """Run both queries on every database. ``status``: equal, differs, unchecked."""

        ran = 0
        errors = Counter()
        for index in range(len(self.databases)):
            schema_name = self._load(index)
            a = self._run(schema_name, before_sql)
            b = self._run(schema_name, after_sql)
            if isinstance(a, Exception) or isinstance(b, Exception):
                errors["before" if isinstance(a, Exception) else "after"] += 1
                if isinstance(a, Exception) and not isinstance(b, Exception):
                    errors["before_only"] += 1
                if isinstance(b, Exception) and not isinstance(a, Exception):
                    errors["after_only"] += 1
                last_error = str(a if isinstance(a, Exception) else b)[:300]
                continue
            if len(a) > 5000 or len(b) > 5000:
                continue
            try:
                same = bag(a) == bag(b)
            except Unsupported:
                continue
            ran += 1
            if same:
                continue
            plain = [self._run(schema_name, sql, optimized=False) for sql in (before_sql, after_sql)]
            if any(isinstance(p, Exception) for p in plain) or bag(plain[0]) == bag(plain[1]):
                continue  # a DuckDB optimizer disagreement (#347) is not evidence
            reversed_name = self._load(index, reverse=True)
            again = [self._run(reversed_name, sql) for sql in (before_sql, after_sql)]
            if any(isinstance(r, Exception) for r in again):
                continue
            if bag(again[0]) != bag(a) or bag(again[1]) != bag(b):
                continue  # depends on row order (LIMIT without a total order, ties, ANY_VALUE): not evidence
            if not (self._tie_free(schema_name, before_sql, a) and self._tie_free(schema_name, after_sql, b)):
                continue  # which rows a LIMIT keeps among ties is unspecified: not evidence
            if self._column_types(schema_name, before_sql) != self._column_types(schema_name, after_sql):
                continue  # the prover does not compare result types, and DuckDB coerces a mixed-type UNION BigQuery rejects
            return {"status": "differs", "database": index, "database_name": self.databases[index]["name"], "before_rows": _jsonable_rows(a), "after_rows": _jsonable_rows(b), "ran": ran}
        if ran == 0:
            return {"status": "unchecked", "errors": dict(errors), "error": locals().get("last_error")}
        return {"status": "equal", "ran": ran, "errors": dict(errors)}


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _jsonable_rows(rows):
    return sorted((_jsonable(list(r)) for r in rows), key=repr)


def _restore(value, kind: str):
    if value is None:
        return None
    if kind == "NUMERIC":
        return Decimal(value)
    if kind == "DATE":
        return _dt.date.fromisoformat(value) if isinstance(value, str) else value
    if kind in ("TIMESTAMP", "DATETIME"):
        return _dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    return value


def databases_to_json(case: dict, databases: list[dict]) -> list[dict]:
    return [{"name": d["name"], "tables": {t: [_jsonable(r) for r in rows] for t, rows in d["tables"].items()}} for d in databases]


def databases_from_json(case: dict, databases: list[dict]) -> list[dict]:
    kinds = {t: [_type_name(k) for _, k in cols] for t, cols in case["schema"].items()}
    return [{"name": d["name"], "tables": {t: [[_restore(v, k) for v, k in zip(r, kinds[t])] for r in rows] for t, rows in d["tables"].items()}} for d in databases]


# ---------------------------------------------------------------------------
# Tracing normalize
# ---------------------------------------------------------------------------

# Names ``normalize`` calls that do not rewrite the query.
_NOT_RULES = {"check_modeled", "_derived_output_names", "_keeps_names", "faithful_sql", "strip_positions"}


def _path(node: exp.Expression) -> list:
    steps = []
    while node.parent is not None:
        steps.append((node.arg_key, node.index))
        node = node.parent
    return steps[::-1]


def _at(root: exp.Expression, path: list) -> exp.Expression | None:
    node = root
    for key, index in path:
        value = node.args.get(key)
        node = value[index] if index is not None and isinstance(value, list) else value
        if not isinstance(node, exp.Expression):
            return None
    return node


def _replace_at(root: exp.Expression, path: list, replacement: exp.Expression) -> exp.Expression:
    if not path:
        return replacement
    target = _at(root, path)
    if target is None:
        raise LookupError("path vanished")
    parent = target.parent
    key, index = path[-1]
    parent.set(key, replacement, index)
    return root


class Fire:
    __slots__ = ("rule", "module", "before", "after", "note")

    def __init__(self, rule, module, before, after, note=""):
        self.rule, self.module, self.before, self.after, self.note = rule, module, before, after, note


def rule_names() -> dict[str, str]:
    """``{name: module}`` for every rewrite ``normalize`` calls by a module-level name."""

    import types

    from kumosql import algebraic_equivalence as ae

    names: set = set()

    def visit(code):
        names.update(code.co_names)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                visit(const)

    visit(inspect.unwrap(ae.normalize).__code__)  # normalize is wrapped by solver_lock.serialized
    out = {}
    for name in sorted(names):
        value = ae.__dict__.get(name)
        if isinstance(value, types.FunctionType) and name not in _NOT_RULES and name != "normalize":
            out[name] = value.__module__.rsplit(".", 1)[-1]
    out["qualify"] = "algebraic_equivalence"  # the local ``qualify`` step, through _qualify_outer_join_columns
    return out


def _fingerprint(node: exp.Expression) -> int:
    """A cheap structural hash of ``node``'s subtree (node types and their non-tree arguments)."""

    parts = []
    for n in node.walk():
        parts.append(type(n).__name__)
        for key, value in n.args.items():
            if value is not None and not isinstance(value, (exp.Expression, list)):
                parts.append((key, value if isinstance(value, (str, int, float, bool)) else repr(value)))
    return hash(tuple(parts))


class Tracer:
    """Wrap ``normalize``'s rewrites; each firing at the top level becomes a :class:`Fire` (whole query before/after).

    ``probe`` mode only notes which top-level calls changed the tree (a cheap fingerprint per call); ``record`` mode
    snapshots the whole query around the calls in ``targets`` (all calls when ``targets`` is None). ``normalize`` is
    deterministic, so probing first and recording only the calls that changed something keeps tracing cheap.
    """

    def __init__(self, only: set | None = None, mode: str = "record", targets: set | None = None):
        from kumosql import algebraic_equivalence as ae

        self.ae = ae
        self.only = only
        self.mode = mode
        self.targets = targets
        self.fires: list[Fire] = []
        self.changed: set = set()
        self.depth = 0
        self.counter = 0
        self.saved: dict = {}
        self.calls: Counter = Counter()

    def __enter__(self):
        for name, module in rule_names().items():
            if name == "qualify":
                continue
            original = self.ae.__dict__[name]
            self.saved[name] = original
            self.ae.__dict__[name] = self._wrap(name, module, original)
        return self

    def __exit__(self, *exc):
        for name, original in self.saved.items():
            self.ae.__dict__[name] = original
        self.saved.clear()

    def _wrap(self, name: str, module: str, fn):
        tracer = self

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            node = args[0] if args else None
            if tracer.depth or not isinstance(node, exp.Expression):
                return fn(*args, **kwargs)
            if name == "_qualify_outer_join_columns":
                caller = sys._getframe(1)
                if caller.f_code.co_name == "qualify":
                    # the step's ``qualify`` qualifies a copy of ``select``; trace it against the real tree
                    original = caller.f_locals.get("select")
                    if not isinstance(original, exp.Select):
                        return fn(*args, **kwargs)
                    return tracer._traced("qualify", module, fn, args, kwargs, anchor=original)
            return tracer._traced(name, module, fn, args, kwargs, anchor=node)

        return wrapper

    def _watched(self, rule: str, module: str) -> bool:
        return self.only is None or rule in self.only or module in self.only

    def _call(self, fn, args, kwargs):
        self.depth += 1
        try:
            return fn(*args, **kwargs)
        finally:
            self.depth -= 1

    def _traced(self, rule, module, fn, args, kwargs, anchor):
        index = self.counter
        self.counter += 1
        self.calls[rule] += 1
        if not self._watched(rule, module):
            return self._call(fn, args, kwargs)
        root = anchor.root()
        path = _path(anchor)
        names = None
        if isinstance(anchor, exp.Select) and isinstance(anchor.parent, (exp.Subquery, exp.CTE)):
            names = self.ae._derived_output_names(anchor)
        if self.mode == "probe":
            before = _fingerprint(anchor)
            result = self._call(fn, args, kwargs)
            replacement = result if isinstance(result, exp.Expression) else None
            if rule == "qualify":
                changed = replacement is not None and _fingerprint(replacement) != before
            elif replacement is not None and replacement is not anchor:
                changed = names is None or self.ae._keeps_names(names, self.ae._derived_output_names(replacement))
            else:
                changed = _at(root, path) is not anchor or _fingerprint(anchor) != before
            if changed:
                self.changed.add(index)
            return result
        if self.targets is not None and index not in self.targets:
            return self._call(fn, args, kwargs)
        before = root.copy()
        result = self._call(fn, args, kwargs)
        try:
            replacement = result if isinstance(result, exp.Expression) else None
            if rule == "qualify":
                replacement = result if isinstance(result, exp.Expression) and result.sql() != anchor.sql() else None
                after = _replace_at(root.copy(), path, replacement.copy()) if replacement is not None else None
            elif replacement is not None and replacement is not anchor:
                if names is not None and not self.ae._keeps_names(names, self.ae._derived_output_names(replacement)):
                    return result  # normalize reverts a rewrite that renames a derived table's outputs
                after = _replace_at(root.copy(), path, replacement.copy())
            else:
                after = root.copy()
            if after is None:
                return result
            if after.sql() != before.sql():
                note = "" if replacement is not None else "changed in place, returned None"
                self.fires.append(Fire(rule, module, before, after, note))
        except LookupError:
            pass
        return result


def normalize_kwargs(case: dict, keyed_distinct: int = 0) -> dict:
    """The keyword arguments ``_prove_algebraic`` passes to ``normalize`` for this case."""

    constraints = case.get("constraints", {})
    schema = {t: [c[0] for c in cols] for t, cols in case["schema"].items()}
    options = case.get("options", {})
    return {
        "schema": schema if options.get("pass_schema", True) else None,
        "dialect": case.get("dialect", "bigquery"),
        "not_null": {t: frozenset(c.get("not_null", [])) | frozenset(x for k in c.get("keys", []) for x in k) for t, c in constraints.items()},
        "keys": {t.lower(): [tuple(k) for k in c.get("keys", [])] for t, c in constraints.items()},
        "types": {t: {c: k for c, k in cols} for t, cols in case["schema"].items()} if options.get("pass_schema", True) else None,
        "group_by_constants": options.get("group_by_constants", False),
        "keyed_distinct": keyed_distinct,
        "foreign_keys": {t.lower(): [(tuple(a), p, tuple(b)) for a, p, b in c.get("foreign_keys", [])] for t, c in constraints.items() if c.get("foreign_keys")},
    }


def _not_null_as_prover(case: dict) -> dict:
    # _prove_algebraic passes TableConstraints.not_null only (keys are separately NOT NULL by declaration)
    return {t: frozenset(c.get("not_null", [])) for t, c in case.get("constraints", {}).items()}


def trace_case(case: dict, only: set | None = None) -> tuple[list[Fire], list[str]]:
    """Every rule firing ``normalize`` makes on ``case`` (keyed_distinct 0, 1 and 2), and any crashes."""

    from kumosql import algebraic_equivalence as ae

    fires: list[Fire] = []
    crashes = []
    # keyed_distinct 1 and 2 only add rules that drop a DISTINCT
    levels = (0, 1, 2) if case.get("constraints") and "DISTINCT" in case["sql"].upper() else (0,)
    sql = case["sql"]
    dialect = case.get("dialect", "bigquery")
    if dialect == "bigquery":
        sql = ae.canonical_literals(sql)
    for level in levels:
        kwargs = normalize_kwargs(case, level)
        kwargs["not_null"] = _not_null_as_prover(case)
        with Tracer(only, mode="probe") as probe:
            try:
                ae.normalize(sql, **kwargs)
            except Exception as error:  # noqa: BLE001 - a rule crash is recorded, never a verdict
                crashes.append(f"{type(error).__name__}: {error}"[:300])
        if not probe.changed:
            continue
        with Tracer(only, mode="record", targets=probe.changed) as tracer:
            try:
                ae.normalize(sql, **kwargs)
            except Exception:  # noqa: BLE001 - recorded by the probe
                pass
        if tracer.counter != probe.counter:
            # not deterministic after all: record every call
            with Tracer(only, mode="record") as tracer:
                try:
                    ae.normalize(sql, **kwargs)
                except Exception:  # noqa: BLE001
                    pass
        fires.extend(tracer.fires)
    seen = set()
    unique = []
    for fire in fires:
        key = (fire.rule, fire.before.sql(), fire.after.sql())
        if key not in seen:
            seen.add(key)
            unique.append(fire)
    return unique, crashes


def typed_case(case: dict) -> dict | None:
    """``case`` with BigQuery type names in its schema, or ``None`` when a column's values cannot be generated."""

    schema = {}
    for table, columns in case["schema"].items():
        typed = [[c, _type_name(t)] for c, t in columns]
        if any(t is None for _, t in typed):
            return None
        schema[table] = typed
    return dict(case, schema=schema)


def check_case(case: dict, only: set | None = None, seed: int = 0, query_seconds: float = 5.0, databases: list[dict] | None = None) -> dict:
    """Trace ``case`` and run every firing on its databases. JSON-safe."""

    fires, crashes = trace_case(case, only)
    result = {"fires": [], "crashes": crashes}
    if not fires:
        return result
    typed = typed_case(case)
    if typed is None:
        result["fires"] = [{"rule": f.rule, "module": f.module, "status": "unchecked", "reason": "unsupported column type"} for f in fires]
        return result
    case = typed
    if databases is None:
        databases = build_databases(case, seed)
    oracle = Oracle(case, databases, query_seconds)
    dialect = case.get("dialect", "bigquery")
    try:
        for fire in fires:
            entry = {"rule": fire.rule, "module": fire.module}
            try:
                before_sql, after_sql = to_duckdb(fire.before, dialect), to_duckdb(fire.after, dialect)
            except Unsupported as why:
                entry.update(status="unchecked", reason=str(why)[:200])
                result["fires"].append(entry)
                continue
            verdict = oracle.compare(before_sql, after_sql)
            entry["status"] = verdict["status"]
            if verdict["status"] == "differs":
                entry.update(
                    before=_text(fire.before, dialect),
                    after=_text(fire.after, dialect),
                    before_duckdb=before_sql,
                    after_duckdb=after_sql,
                    database=databases_to_json(case, [databases[verdict["database"]]])[0],
                    before_rows=verdict["before_rows"],
                    after_rows=verdict["after_rows"],
                    note=fire.note,
                )
            elif verdict["status"] == "unchecked":
                entry["reason"] = (verdict.get("error") or "")[:200]
            result["fires"].append(entry)
    finally:
        oracle.close()
    return result


def _text(tree: exp.Expression, dialect: str) -> str:
    try:
        return tree.sql(dialect=dialect)
    except Exception:  # noqa: BLE001
        return repr(tree)


# ---------------------------------------------------------------------------
# Reduction
# ---------------------------------------------------------------------------


def _candidates(tree: exp.Expression):
    """Smaller variants of ``tree``: a list item or optional clause dropped, a node replaced by a child."""

    nodes = list(tree.walk())
    for node in nodes:
        for key, value in list(node.args.items()):
            if isinstance(value, list) and len(value) > 1 and all(isinstance(v, exp.Expression) for v in value):
                for i in range(len(value)):
                    copy = tree.copy()
                    target = _at(copy, _path(node))
                    if target is None:
                        continue
                    items = list(target.args[key])
                    del items[i]
                    target.set(key, items)
                    yield copy
            elif isinstance(value, exp.Expression) and key in ("where", "having", "qualify", "order", "limit", "offset", "group", "distinct", "with_", "with", "joins"):
                copy = tree.copy()
                target = _at(copy, _path(node))
                if target is not None:
                    target.set(key, None)
                    yield copy
    for node in nodes:
        if node is tree:
            continue
        for child in node.iter_expressions():
            if isinstance(child, type(node).__mro__[0]) or isinstance(node, (exp.Connector, exp.Not, exp.Paren, exp.Case, exp.If, exp.Coalesce, exp.Subquery, exp.SetOperation, exp.Binary, exp.Func)):
                if isinstance(node, exp.SetOperation) and not isinstance(child, exp.Query):
                    continue
                copy = tree.copy()
                target = _at(copy, _path(node))
                if target is None:
                    continue
                inner = _at(copy, _path(child))
                if inner is None:
                    continue
                if target is copy:
                    yield inner.copy()
                else:
                    target.replace(inner.copy())
                    yield copy
        if isinstance(node, (exp.Connector, exp.Predicate)) and not isinstance(node.parent, (exp.Select,)):
            copy = tree.copy()
            target = _at(copy, _path(node))
            if target is not None and target is not copy:
                target.replace(exp.true())
                yield copy


def reduce_bug(case: dict, rule: str, seed: int = 0, budget_seconds: float = 120.0, query_seconds: float = 5.0) -> dict | None:
    """A smaller case on which ``rule`` still fires and still changes the result, with its witness."""

    deadline = time.monotonic() + budget_seconds
    dialect = case.get("dialect", "bigquery")
    case = typed_case(case) or case

    def witness(candidate: dict):
        try:
            result = check_case(candidate, only={rule}, seed=seed, query_seconds=query_seconds)
        except Exception:  # noqa: BLE001
            return None
        return next((f for f in result["fires"] if f["rule"] == rule and f["status"] == "differs"), None)

    best = witness(case)
    if best is None:
        return None
    current = case
    tree = sqlglot.parse_one(case["sql"], read=dialect)
    progress = True
    while progress and time.monotonic() < deadline:
        progress = False
        size = len(tree.sql(dialect=dialect))
        for candidate_tree in _candidates(tree):
            if time.monotonic() > deadline:
                break
            try:
                text = candidate_tree.sql(dialect=dialect)
            except Exception:  # noqa: BLE001
                continue
            if len(text) >= size:
                continue
            candidate = dict(current, sql=text)
            found = witness(candidate)
            if found is not None:
                tree, current, best, progress = candidate_tree, candidate, found, True
                break
    # rows: drop whole tables, then single rows, while the firing still differs on the witness database
    database = databases_from_json(current, [best["database"]])[0]
    tables = {t: list(rows) for t, rows in database["tables"].items()}

    def differs(trial) -> dict | None:
        if fixture_errors(current, trial):
            return None
        try:
            result = check_case(current, only={rule}, databases=[{"name": "reduced", "tables": trial}], query_seconds=query_seconds)
        except Exception:  # noqa: BLE001
            return None
        return next((f for f in result["fires"] if f["rule"] == rule and f["status"] == "differs"), None)

    for table in sorted(tables):
        if tables[table] and time.monotonic() < deadline:
            trial = dict(tables, **{table: []})
            found = differs(trial)
            if found:
                tables, best = trial, found
    changed = True
    while changed and time.monotonic() < deadline:
        changed = False
        for table in sorted(tables):
            for i in range(len(tables[table])):
                trial = dict(tables, **{table: tables[table][:i] + tables[table][i + 1:]})
                found = differs(trial)
                if found:
                    tables, best, changed = trial, found, True
                    break
            if changed:
                break
    return {"case": current, "witness": best}


# ---------------------------------------------------------------------------
# Corpora
# ---------------------------------------------------------------------------


def gen_cases(seed: int, count: int) -> list[dict]:
    from rule_fuzz_gen import generate_cases

    return generate_cases(seed, count)


def fuzz_cases(seed: int, count: int) -> list[dict]:
    """Both sides of ``tools/soundness_fuzz.py`` template pairs, as single queries."""

    try:
        import soundness_fuzz as sf
    except ImportError:
        return []
    out = []
    for case in sf.generate_cases(seed, count) if hasattr(sf, "generate_cases") else []:
        schema = case["schema"]
        for side in ("left", "right"):
            out.append({"sql": case[side], "dialect": case.get("dialect", "bigquery"), "schema": schema, "constraints": case.get("constraints", {}), "options": {"pass_schema": case.get("pass_schema", True)}, "source": f"fuzz:{case.get('family', '?')}"})
    return out


def eval_cases() -> list[dict]:
    """The SQLSolver-family eval queries with typed schemas and keys. Held-out pairs (mined Calcite ``new``) are skipped."""

    import tempfile

    import sqlsolver_bench as sb

    def schema_of(tables) -> tuple[dict, dict]:
        schema = {t.name: [[c.name, c.type] for c in t.columns] for t in tables.values()}
        constraints = {
            t.name: {"not_null": [c.name for c in t.columns if c.not_null], "keys": [list(k) for k in ([t.primary_key] if t.primary_key else []) + list(t.unique)]}
            for t in tables.values()
        }
        return schema, constraints

    def tables_from_ddl(ddl: str):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "schema.sql"
            path.write_text(ddl, encoding="utf-8")
            return sb.load_schema(path)

    out = []
    for name, (pairs_file, schema_file) in sb.SUITES.items():
        tables = sb.load_schema(sb.FIXTURES / schema_file)
        schema, constraints = schema_of(tables)
        constants = name in sb.CONSTANT_GROUPING
        for index, (left, right) in enumerate(sb.load_pairs(sb.FIXTURES / pairs_file)):
            for side, sql in (("l", left), ("r", right)):
                if constants:
                    sql = sb.calcite_operators(sql)
                out.append({"sql": sb.spark_days(sql), "dialect": "mysql", "schema": schema, "constraints": constraints, "options": {"group_by_constants": constants}, "source": f"sqlsolver-{name}:{index}{side}"})
    try:
        import qed_bench

        for case in qed_bench.load_cases():
            schema, constraints = schema_of(tables_from_ddl(case["ddl"]))
            for side in ("sql_a", "sql_b"):
                out.append({"sql": sb.spark_days(case[side]), "dialect": "mysql", "schema": schema, "constraints": constraints, "options": {"group_by_constants": True}, "source": f"qed:{case['name']}"})
    except Exception as error:  # noqa: BLE001 - a missing fixture only shrinks the corpus
        print(f"QED corpus skipped: {error}", file=sys.stderr)
    try:
        import calcite_mined_bench as cm

        schemas = json.loads((cm.FIXTURES / "schemas.json").read_text(encoding="utf-8"))
        loaded: dict = {}
        for pair in cm.load_pairs():
            if pair.get("new"):
                continue  # held out
            key = pair["schema_id"]
            if key not in loaded:
                loaded[key] = schema_of(tables_from_ddl(schemas[key]["ddl"]))
            schema, constraints = loaded[key]
            for side in ("sql_a", "sql_b"):
                out.append({"sql": pair[side], "dialect": "mysql", "schema": schema, "constraints": constraints, "options": {}, "source": f"calcite-mined:{pair['name']}"})
    except Exception as error:  # noqa: BLE001
        print(f"calcite-mined corpus skipped: {error}", file=sys.stderr)
    try:
        import rbot_bench

        schema, constraints = schema_of(sb.load_schema(rbot_bench.FIXTURES / "create_tables.sql"))
        for name, left, right in rbot_bench.load_pairs():
            for sql in (left, right):
                try:
                    sql = rbot_bench.normalise(sql)
                except Exception:  # noqa: BLE001
                    continue
                out.append({"sql": sql, "dialect": "mysql", "schema": schema, "constraints": constraints, "options": {"group_by_constants": True}, "source": f"rbot:{name}"})
    except Exception as error:  # noqa: BLE001
        print(f"R-Bot corpus skipped: {error}", file=sys.stderr)
    # distinct queries only
    seen, unique = set(), []
    for case in out:
        key = (case["sql"], json.dumps(case["schema"], sort_keys=True), json.dumps(case["options"], sort_keys=True))
        if key not in seen:
            seen.add(key)
            unique.append(case)
    return unique


def target_cases(name: str, seed: int, count: int) -> list[dict]:
    """``tools/rule_fuzz_targets/<name>.py``'s ``cases(seed, count)``: generators aimed at one rule module's rules."""

    import importlib

    module = importlib.import_module(f"rule_fuzz_targets.{name}")
    return module.cases(seed, count)


def load_corpus(name: str, seed: int, count: int) -> list[dict]:
    if name.startswith("target:"):
        return [case for target in name.split(":", 1)[1].split(",") for case in target_cases(target, seed, count)]
    if name == "gen":
        return gen_cases(seed, count)
    if name == "fuzz":
        return fuzz_cases(seed, count)
    if name == "evals":
        return eval_cases()
    if name.endswith(".json") or name.endswith(".jsonl"):
        path = Path(name)
        text = path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()] if name.endswith(".jsonl") else json.loads(text)
    raise SystemExit(f"unknown corpus {name}")


# ---------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class Worker:
    """A long-lived child answering one JSON request per line; restarted after a timeout or crash."""

    def __init__(self):
        self._start()

    def _start(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env,
        )
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, args=(self.process, self.lines), daemon=True).start()

    @staticmethod
    def _read(process, lines):
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()

    def ask(self, request: dict, timeout: float) -> dict:
        try:
            self.process.stdin.write(_json(request) + "\n")
            self.process.stdin.flush()
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            self.close()
            self._start()
            return {"error": "timeout"}
        except OSError as error:
            self.close()
            self._start()
            return {"error": f"pipe: {error}"}
        if line is None:
            self.close()
            self._start()
            return {"error": "child exited"}
        return json.loads(line)


def _worker_loop() -> int:
    out = sys.stdout
    sys.stdout = sys.stderr
    import logging

    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            only = set(request["only"]) if request.get("only") else None
            if request.get("reduce"):
                answer = reduce_bug(request["case"], request["reduce"], seed=request.get("seed", 0), budget_seconds=request.get("budget", 120.0)) or {}
            else:
                answer = check_case(request["case"], only=only, seed=request.get("seed", 0))
        except Exception as error:  # noqa: BLE001
            answer = {"error": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc()[-1500:]}
        out.write(_json(answer) + "\n")
        out.flush()
    return 0


def run_cases(cases: list[dict], jobs: int, only: set | None, seed: int, timeout: float, progress: bool = True) -> list[dict]:
    workers: queue.Queue = queue.Queue()
    pool = [Worker() for _ in range(max(1, jobs))]
    for worker in pool:
        workers.put(worker)
    done = [0]
    started = time.monotonic()

    def one(case):
        worker = workers.get()
        try:
            answer = worker.ask({"case": case, "only": sorted(only) if only else None, "seed": seed}, timeout)
        finally:
            workers.put(worker)
        done[0] += 1
        if progress and done[0] % 200 == 0:
            print(f"  {done[0]}/{len(cases)} cases, {time.monotonic() - started:.0f} s", file=sys.stderr, flush=True)
        return answer

    try:
        with ThreadPoolExecutor(max_workers=len(pool)) as executor:
            return list(executor.map(one, cases))
    finally:
        for worker in pool:
            worker.close()


def reduce_many(bugs: list[dict], jobs: int, seed: int, budget: float) -> list[dict]:
    workers: queue.Queue = queue.Queue()
    pool = [Worker() for _ in range(max(1, jobs))]
    for worker in pool:
        workers.put(worker)

    def one(bug):
        worker = workers.get()
        try:
            return worker.ask({"case": bug["case"], "reduce": bug["rule"], "seed": seed, "budget": budget}, budget * 2 + 60)
        finally:
            workers.put(worker)

    try:
        with ThreadPoolExecutor(max_workers=len(pool)) as executor:
            return list(executor.map(one, bugs))
    finally:
        for worker in pool:
            worker.close()


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def known_bugs() -> list[dict]:
    if not KNOWN_PATH.exists():
        return []
    return json.loads(KNOWN_PATH.read_text(encoding="utf-8")).get("bugs", [])


def summarize(cases: list[dict], answers: list[dict]) -> dict:
    coverage: dict = defaultdict(lambda: Counter())
    modules: dict = {}
    bugs = []
    crashes = Counter()
    errors = Counter()
    for case, answer in zip(cases, answers):
        if "error" in answer:
            errors[answer["error"][:80]] += 1
            continue
        for crash in answer.get("crashes", []):
            crashes[crash[:120]] += 1
        for fire in answer.get("fires", []):
            stats = coverage[fire["rule"]]
            modules[fire["rule"]] = fire["module"]
            stats["fired"] += 1
            stats[fire["status"]] += 1
            if fire["status"] in ("equal", "differs"):
                stats["checked"] += 1
            if fire["status"] == "differs":
                bugs.append({"rule": fire["rule"], "module": fire["module"], "case": case, "fire": fire})
    return {
        "coverage": {rule: dict(stats, module=modules[rule]) for rule, stats in sorted(coverage.items())},
        "bugs": bugs,
        "crashes": dict(crashes.most_common(40)),
        "errors": dict(errors.most_common(20)),
        "cases": len(cases),
    }


def print_report(summary: dict, all_rules: bool = True) -> None:
    rules = rule_names() if all_rules else {}
    coverage = summary["coverage"]
    bug_rules = Counter(b["rule"] for b in summary["bugs"])
    names = sorted(set(rules) | set(coverage), key=lambda r: (rules.get(r) or coverage.get(r, {}).get("module", ""), r))
    print(f"{'module':28} {'rule':42} {'fired':>7} {'checked':>8} {'bugs':>5}")
    for name in names:
        stats = coverage.get(name, {})
        module = stats.get("module") or rules.get(name, "?")
        print(f"{module:28} {name:42} {stats.get('fired', 0):>7} {stats.get('checked', 0):>8} {bug_rules.get(name, 0):>5}")
    print(f"\n{summary['cases']} cases; {sum(bug_rules.values())} differing firings in {len(bug_rules)} rules")
    if summary.get("crashes"):
        print("crashes:", json.dumps(summary["crashes"], indent=1)[:2000])
    if summary.get("errors"):
        print("worker errors:", json.dumps(summary["errors"], indent=1))


def show_bug(bug: dict) -> None:
    fire = bug["fire"]
    print(f"--- {bug['rule']} ({bug['module']}) from {bug['case'].get('source', '?')}")
    print("query :", bug["case"]["sql"])
    print("before:", fire.get("before"))
    print("after :", fire.get("after"))
    print("db    :", json.dumps(fire.get("database", {}).get("tables")))
    print("rows  :", fire.get("before_rows"), "->", fire.get("after_rows"))
    if fire.get("note"):
        print("note  :", fire["note"])


def _parse_schema(specs: list[str]) -> dict:
    schema: dict = {}
    for spec in specs:
        table, _, cols = spec.partition(":")
        schema[table] = [list(c.split("=")) for c in cols.split(",") if c]
    return schema


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_worker"]:
        return _worker_loop()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="trace and check a corpus")
    run.add_argument("--corpus", default="gen", help="gen, fuzz, evals, target:<module>[,<module>] (tools/rule_fuzz_targets), or a .json/.jsonl file of cases")
    run.add_argument("--seed", type=int, default=1)
    run.add_argument("--count", type=int, default=500)
    run.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    run.add_argument("--only", default="", help="comma-separated rules or modules to check (default all)")
    run.add_argument("--timeout", type=float, default=120.0, help="seconds per case")
    run.add_argument("--reduce", type=int, default=3, help="reduce up to this many differing firings per rule")
    run.add_argument("--reduce-budget", type=float, default=90.0)
    run.add_argument("--out", help="write the run (coverage, bugs, reduced witnesses) as JSON")
    run.add_argument("--show", action="store_true", help="print each reduced witness")
    report = sub.add_parser("report", help="coverage per rule of a saved run")
    report.add_argument("path")
    show = sub.add_parser("show", help="print the witnesses of a saved run")
    show.add_argument("path")
    show.add_argument("--rule", default="")
    query = sub.add_parser("query", help="trace and check one query")
    query.add_argument("sql")
    query.add_argument("--schema", action="append", default=[], help="table:col=TYPE,col=TYPE")
    query.add_argument("--key", action="append", default=[], help="table:col[,col]")
    query.add_argument("--not-null", action="append", default=[], help="table:col[,col]")
    query.add_argument("--fk", action="append", default=[], help="table:col=parent:pcol")
    query.add_argument("--dialect", default="bigquery")
    query.add_argument("--only", default="")
    sub.add_parser("rules", help="list the traced rules and their modules")
    args = parser.parse_args(argv)

    if args.command == "rules":
        for name, module in sorted(rule_names().items(), key=lambda kv: (kv[1], kv[0])):
            print(f"{module:30} {name}")
        return 0
    if args.command == "query":
        constraints: dict = defaultdict(lambda: {"not_null": [], "keys": [], "foreign_keys": []})
        for spec in args.key:
            table, _, cols = spec.partition(":")
            constraints[table]["keys"].append(cols.split(","))
        for spec in args.not_null:
            table, _, cols = spec.partition(":")
            constraints[table]["not_null"].extend(cols.split(","))
        for spec in args.fk:
            child, _, parent = spec.partition("=")
            ctable, _, ccol = child.partition(":")
            ptable, _, pcol = parent.partition(":")
            constraints[ctable]["foreign_keys"].append([ccol.split(","), ptable, pcol.split(",")])
        case = {"sql": args.sql, "dialect": args.dialect, "schema": _parse_schema(args.schema), "constraints": dict(constraints)}
        only = set(filter(None, args.only.split(","))) or None
        result = check_case(case, only)
        for fire in result["fires"]:
            print(f"{fire['status']:9} {fire['module']:28} {fire['rule']}" + (f"  ({fire.get('reason')})" if fire.get("reason") else ""))
            if fire["status"] == "differs":
                show_bug({"rule": fire["rule"], "module": fire["module"], "case": case, "fire": fire})
        for crash in result["crashes"]:
            print("crash:", crash)
        return 0
    if args.command in ("report", "show"):
        data = json.loads(Path(args.path).read_text(encoding="utf-8"))
        if args.command == "report":
            print_report(data)
        else:
            for bug in data.get("reduced", []) or data.get("bugs", []):
                if not args.rule or args.rule in (bug["rule"], bug["module"]):
                    show_bug(bug)
        return 0

    cases = load_corpus(args.corpus, args.seed, args.count)
    only = set(filter(None, args.only.split(","))) or None
    print(f"{len(cases)} cases from {args.corpus}", file=sys.stderr)
    started = time.monotonic()
    answers = run_cases(cases, args.jobs, only, args.seed, args.timeout)
    summary = summarize(cases, answers)
    summary["seconds"] = round(time.monotonic() - started, 1)
    # reduce a few differing firings per rule
    by_rule: dict = defaultdict(list)
    for bug in summary["bugs"]:
        by_rule[bug["rule"]].append(bug)
    picked = [b for bugs in by_rule.values() for b in sorted(bugs, key=lambda b: len(b["case"]["sql"]))[: args.reduce]]
    reduced = []
    if picked and args.reduce:
        for bug, answer in zip(picked, reduce_many(picked, args.jobs, args.seed, args.reduce_budget)):
            if answer and "witness" in answer:
                reduced.append({"rule": bug["rule"], "module": bug["module"], "case": answer["case"], "fire": answer["witness"], "original": bug["case"]["sql"]})
    summary["reduced"] = reduced
    print_report(summary)
    if args.show:
        for bug in reduced:
            show_bug(bug)
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
