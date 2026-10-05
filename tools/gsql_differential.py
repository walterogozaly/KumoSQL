"""Differential check: the pure-Python GoogleSQL evaluator against BigQuery-on-DuckDB.

For every query where the evaluator (:mod:`kumosql.gsql_eval`, ``mode="bigquery"``, UTC) returns a result, the
same query runs on the same tables through KumoSQL's BigQuery-on-DuckDB layer (``to_duckdb(sql, "bigquery")``,
:mod:`kumosql.bigquery_on_duckdb`) and the two results are compared. The tables are loaded into both engines
from the same typed payloads (never by running setup SQL). Queries come from

1. the claimed cases of the GoogleSQL conformance **dev** split (the held-out split is never read), with the
   fixture tables of their files, and
2. optionally a seeded generator of random expression queries over INT64, FLOAT64, NUMERIC, STRING and DATE.

    python tools/gsql_differential.py                         # every claimed dev case
    python tools/gsql_differential.py --limit 60              # about 60 cases, evenly spaced
    python tools/gsql_differential.py --random 3000 --seed 1  # also 3,000 random queries
    python tools/gsql_differential.py --json out.json         # the full report as JSON

Outcomes of a query the evaluator answered:

* **agree**: same rows (floats within 4 ULPs, arrays the evaluator marks unordered as multisets, an empty
  array equal to NULL as in BigQuery);
* **declined / not run**: the DuckDB layer refused the query (a guard, a construct without a faithful reading,
  a result BigQuery could not return), or DuckDB or sqlglot cannot run it. Not a divergence;
* **diverge**: both ran and the rows differ, and DuckDB with its optimizer off (``run_unoptimized``) returns
  the same rows as DuckDB with it on. Each is shrunk to a small repro (rows, then the query);
* **diverge, optimizer explained**: the rows differ but the optimizer-off run agrees with the evaluator
  (a DuckDB optimizer bug, not reported);
* **unstable**: the optimizer-off run agrees with neither (counted as not run).

Divergences from conformance cases also say whether the evaluator matches the compliance file's expected rows
(Google's reference implementation): if it does, DuckDB is the wrong one. Exit status is 0 (divergences are
findings, not failures) unless the run itself breaks.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import multiprocessing
import os
import random
import re
import signal
import struct
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

from kumosql.gsql_eval import AnalysisError, Database, EvalError, Table, Unsupported, evaluate  # noqa: E402
from kumosql.gsql_eval import types as T  # noqa: E402
from kumosql.gsql_eval import values as V  # noqa: E402

import logging  # noqa: E402

logging.getLogger("sqlglot").setLevel(logging.ERROR)  # "LIMIT inside ARRAY_AGG is not supported" and the like

TIME_ZONE = "UTC"  # BigQuery's default; the layer reads timestamps in UTC
QUERY_SECONDS = 20  # an evaluator or DuckDB run longer than this is interrupted and counts as not run


# --- outcomes -------------------------------------------------------------------------------------


class Skip(Exception):
    """The evaluator did not produce a result to compare (``reason`` says why)."""


class NotRun(Exception):
    """The DuckDB side did not produce rows: declined (a guard or a refusal) or unable to run."""

    def __init__(self, reason: str, declined: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.declined = declined


@dataclass
class Answer:
    """What the evaluator returned: ``columns`` is ``[(name, Type)]``, ``rows`` payload tuples."""

    columns: list
    rows: list
    ordered: bool = True
    inexact: bool = False


@dataclass
class Verdict:
    kind: str  # agree | diverge | optimizer | unstable | not_run | skipped
    reason: str = ""
    evaluator_rows: list | None = None
    duckdb_rows: list | None = None
    columns: list | None = None
    declined: bool = False
    duckdb_sql: str = ""
    difference: str = ""  # diverge: "value" or "type" (equal values, another number type)


# --- comparing results ----------------------------------------------------------------------------


def _float_order(x: float) -> int:
    i = struct.unpack("<q", struct.pack("<d", x))[0]
    return i if i >= 0 else -(2**63) - i


def float_close(a: float, b: float, ulps: int = 4, relative: float = 0.0) -> bool:
    """Equal doubles: NaN equals NaN, otherwise within ``ulps`` representable steps (or ``relative``)."""

    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    if a == b:
        return True
    if math.isinf(a) or math.isinf(b):
        return False
    if abs(_float_order(a) - _float_order(b)) <= ulps:
        return True
    return relative > 0 and abs(a - b) <= relative * max(abs(a), abs(b))


def _micros_to_datetime(micros: int) -> datetime:
    return V.EPOCH + timedelta(microseconds=micros)


def values_equal(t: T.Type, ev: Any, dk: Any, relative: float = 0.0, strict: bool = True) -> bool:
    """Whether the evaluator payload ``ev`` of type ``t`` equals DuckDB's value ``dk`` (read as BigQuery returns it).

    The DuckDB value must also have the Python type the evaluator's static type implies, so a column BigQuery types
    INT64 that DuckDB computed as a double is a difference. A ``DATETIME``/``TIMESTAMP`` at midnight arrives from
    the layer as a ``date`` (it cannot tell it from a ``DATE``) and is accepted as one.
    """

    kind = t.kind
    if kind == "ARRAY":
        ev = None if ev is not None and len(ev) == 0 else ev
        dk = None if dk is not None and len(dk) == 0 else dk
    if ev is None or dk is None:
        return ev is None and dk is None
    if kind in ("FLOAT64", "INT64", "NUMERIC", "BIGNUMERIC") and not strict:
        # the value alone, whichever number type DuckDB used for it
        if isinstance(dk, bool) or not isinstance(dk, (int, float, Decimal)):
            return False
        if isinstance(ev, float) or isinstance(dk, float):
            return float_close(float(ev), float(dk), relative=max(relative, 1e-12))
        return ev == dk
    if kind == "FLOAT64":
        return isinstance(dk, float) and float_close(ev, dk, relative=relative)
    if kind == "INT64":
        return isinstance(dk, int) and not isinstance(dk, bool) and ev == dk
    if kind in ("NUMERIC", "BIGNUMERIC"):
        return isinstance(dk, Decimal) and ev == dk
    if kind == "BOOL":
        return isinstance(dk, bool) and ev == dk
    if kind == "STRING":
        return isinstance(dk, str) and ev == dk
    if kind == "BYTES":
        return isinstance(dk, (bytes, bytearray)) and bytes(ev) == bytes(dk)
    if kind == "DATE":
        return isinstance(dk, date) and not isinstance(dk, datetime) and ev == dk
    if kind in ("DATETIME", "TIMESTAMP"):
        want = _micros_to_datetime(ev) if kind == "TIMESTAMP" else ev
        if isinstance(dk, datetime):
            return dk == want
        return isinstance(dk, date) and want == datetime(dk.year, dk.month, dk.day)
    if kind == "TIME":
        return isinstance(dk, dtime) and ev == dk
    if kind == "ARRAY":
        if not isinstance(dk, (tuple, list)) or len(ev) != len(dk):
            return False
        if isinstance(ev, V.UnorderedArray):
            return _multiset_equal(t.elem, list(ev), list(dk), relative, strict)
        return all(values_equal(t.elem, x, y, relative, strict) for x, y in zip(ev, dk))
    if kind == "STRUCT":
        if not isinstance(dk, (tuple, list)) or len(ev) != len(dk) or len(ev) != len(t.fields):
            return False
        return all(values_equal(ft, x, y, relative, strict) for (_, ft), x, y in zip(t.fields, ev, dk))
    raise Skip(f"result type {t}")


def _sort_key(t: T.Type, value: Any) -> Any:
    """A key close values share: floats are rounded to 6 significant digits, so a sort pairs near-equal rows."""

    if value is None:
        return (0,)
    kind = t.kind
    if kind == "FLOAT64":
        return (1, "nan") if math.isnan(value) else (1, float(f"{value:.6g}"))
    if kind in ("NUMERIC", "BIGNUMERIC"):
        return (1, str(value.normalize()) if value else "0")
    if kind == "ARRAY":
        keys = sorted((_sort_key(t.elem, v) for v in value), key=repr) if isinstance(value, V.UnorderedArray) else [
            _sort_key(t.elem, v) for v in value
        ]
        return (1, tuple(keys))
    if kind == "STRUCT":
        return (1, tuple(_sort_key(ft, v) for (_, ft), v in zip(t.fields, value)))
    if kind == "TIMESTAMP" and isinstance(value, int):
        return (1, value)
    return (1, repr(value))


def _multiset_equal(t: T.Type, left: list, right: list, relative: float = 0.0, strict: bool = True) -> bool:
    if len(left) != len(right):
        return False
    rest = list(right)
    for x in left:
        for index, y in enumerate(rest):
            if values_equal(t, x, y, relative, strict):
                del rest[index]
                break
        else:
            return False
    return True


def _dk_key(t: T.Type, value: Any) -> Any:
    """The sort key of a DuckDB value, matching :func:`_sort_key` of an equal evaluator value."""

    if value is None:
        return (0,)
    kind = t.kind
    if kind == "FLOAT64":
        return (1, "nan") if isinstance(value, float) and math.isnan(value) else (1, float(f"{float(value):.6g}"))
    if kind in ("NUMERIC", "BIGNUMERIC"):
        return (1, str(value.normalize()) if value else "0") if isinstance(value, Decimal) else (1, repr(value))
    if kind == "ARRAY":
        if not value:
            return (0,)
        return (1, tuple(sorted((_dk_key(t.elem, v) for v in value), key=repr)))
    if kind == "STRUCT" and isinstance(value, (tuple, list)):
        return (1, tuple(_dk_key(ft, v) for (_, ft), v in zip(t.fields, value)))
    if kind == "TIMESTAMP" and isinstance(value, (datetime, date)):
        base = value if isinstance(value, datetime) else datetime(value.year, value.month, value.day)
        return (1, int((base - V.EPOCH) / timedelta(microseconds=1)))
    if kind in ("DATETIME",) and isinstance(value, date) and not isinstance(value, datetime):
        return (1, repr(datetime(value.year, value.month, value.day)))
    return (1, repr(value))


def rows_equal(answer: Answer, rows: list, strict: bool = True) -> bool:
    """Whether the evaluator's answer and DuckDB's ``rows`` agree (column count, row count, values, order if ordered)."""

    types = [ct for _, ct in answer.columns]
    for _, ct in answer.columns:
        _check_readable(ct)
    if len(answer.rows) != len(rows):
        return False
    if any(len(r) != len(types) for r in rows) or any(len(r) != len(types) for r in answer.rows):
        return False
    relative = 1e-9 if answer.inexact else 0.0
    row_type = T.struct(answer.columns)
    if answer.ordered:
        return all(values_equal(row_type, tuple(x), tuple(y), relative, strict) for x, y in zip(answer.rows, rows))
    # unordered: sort both by a rounded key so near-equal rows pair up, then compare pairwise
    left = sorted(answer.rows, key=lambda r: repr(_sort_key(row_type, tuple(r))))
    right = sorted(rows, key=lambda r: repr(_dk_key(row_type, tuple(r))))
    if all(values_equal(row_type, tuple(x), tuple(y), relative, strict) for x, y in zip(left, right)):
        return True
    return len(left) <= 200 and _multiset_equal(row_type, [tuple(r) for r in answer.rows], [tuple(r) for r in rows], relative, strict)


def _check_readable(t: T.Type) -> None:
    if t.kind in ("INTERVAL", "JSON", "OTHER", "INT32", "UINT32", "UINT64", "FLOAT32", "BIGNUMERIC"):
        raise Skip(f"result type {t.kind}")
    if t.kind == "ARRAY":
        _check_readable(t.elem)
    if t.kind == "STRUCT":
        for _, ft in t.fields:
            _check_readable(ft)


# --- the verdict for one query --------------------------------------------------------------------


def classify(session, sql: str) -> Verdict:
    """Run ``sql`` on ``session.evaluate`` and ``session.duckdb`` and compare.

    ``session.evaluate(sql)`` returns an :class:`Answer` or raises :class:`Skip`; ``session.duckdb(sql, optimizer)``
    returns the rows as BigQuery returns them or raises :class:`NotRun`. A fake session can stand in for both.
    """

    try:
        answer = session.evaluate(sql)
    except Skip as skip:
        return Verdict("skipped", str(skip))
    try:
        first = session.duckdb(sql, True)
    except NotRun as problem:
        return Verdict("not_run", problem.reason, declined=problem.declined)
    try:
        if rows_equal(answer, first):
            return Verdict("agree")
    except Skip as skip:
        return Verdict("skipped", str(skip))
    base = dict(evaluator_rows=answer.rows, duckdb_rows=first, columns=answer.columns, duckdb_sql=getattr(session, "last_sql", ""))
    try:
        again = session.duckdb(sql, False)
    except NotRun as problem:
        return Verdict("unstable", "the optimizer-off run did not finish: " + problem.reason, **base)
    if rows_equal(answer, again):
        return Verdict("optimizer", "agrees with the evaluator when DuckDB's optimizer is off", **base)
    if duck_rows_equal(first, again, answer.ordered):
        try:
            same_values = rows_equal(answer, first, strict=False)
        except Skip:
            same_values = False
        return Verdict("diverge", difference="type" if same_values else "value", **base)
    return Verdict("unstable", "the optimizer-off rows differ from both", **base)


def plain_equal(a: Any, b: Any) -> bool:
    """Two DuckDB values (as BigQuery returns them) are equal; floats within 4 ULPs or 1e-9 relative."""

    if isinstance(a, float) and isinstance(b, float):
        return float_close(a, b, relative=1e-9)
    if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
        return len(a) == len(b) and all(plain_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def duck_rows_equal(left: list, right: list, ordered: bool) -> bool:
    if len(left) != len(rows := right):
        return False
    if ordered:
        return all(plain_equal(x, y) for x, y in zip(left, rows))
    rest = list(rows)
    for x in left:
        for index, y in enumerate(rest):
            if plain_equal(x, y):
                del rest[index]
                break
        else:
            return False
    return True


# --- tables ---------------------------------------------------------------------------------------

_SIMPLE = {
    "INT64": "BIGINT", "BOOL": "BOOLEAN", "STRING": "VARCHAR", "BYTES": "BLOB", "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMPTZ", "DATETIME": "TIMESTAMP", "TIME": "TIME", "NUMERIC": "DECIMAL(38, 9)", "FLOAT64": "DOUBLE",
}


class Unloadable(ValueError):
    pass


def duck_type(t: T.Type) -> str:
    if t.kind in _SIMPLE:
        return _SIMPLE[t.kind]
    if t.kind == "ARRAY":
        return duck_type(t.elem) + "[]"
    if t.kind == "STRUCT":
        names = [n or f"_field_{i + 1}" for i, (n, _) in enumerate(t.fields)]
        if len({n.lower() for n in names}) != len(names):
            raise Unloadable("duplicate struct field names")
        return "STRUCT(" + ", ".join(f'"{n}" {duck_type(ft)}' for n, (_, ft) in zip(names, t.fields)) + ")"
    raise Unloadable(f"{t.kind} column")


def duck_value(t: T.Type, value: Any) -> Any:
    """A payload as the Python value DuckDB binds for a column of type ``t``."""

    if value is None:
        return None
    if t.kind == "TIMESTAMP":
        return _micros_to_datetime(value)  # naive UTC, bound into a TIMESTAMPTZ column read in UTC
    if t.kind == "ARRAY":
        return [duck_value(t.elem, v) for v in value]
    if t.kind == "STRUCT":
        return {(n or f"_field_{i + 1}"): duck_value(ft, v) for i, ((n, ft), v) in enumerate(zip(t.fields, value))}
    return value


def has_unloadable(tables: dict[str, Table]) -> dict[str, list[str]]:
    """``{table: [columns DuckDB cannot hold]}`` for the tables that have any."""

    bad: dict[str, list[str]] = {}
    for name, table in tables.items():
        for column, t in table.columns:
            try:
                duck_type(t)
            except Unloadable:
                bad.setdefault(name, []).append(column)
    return bad


def _read_table(con, name: str) -> list:
    """The rows of ``name`` with TIMESTAMPTZ read as UTC naive timestamps (no pytz)."""

    description = con.execute(f"SELECT * FROM {name} LIMIT 0").description
    casts = []
    for i, d in enumerate(description, start=1):
        kind = str(d[1])
        casts.append(f"CAST(#{i} AS {kind.replace('TIMESTAMP WITH TIME ZONE', 'TIMESTAMP')})" if "WITH TIME ZONE" in kind else f"#{i}")
    return con.execute(f"SELECT {', '.join(casts) or '*'} FROM {name}").fetchall()


class CaseTimeout(BaseException):
    """Raised by the per-query alarm; a ``BaseException`` so an evaluator's ``except Exception`` cannot swallow it."""


def _alarm(signum, frame):
    raise CaseTimeout()


class Session:
    """One database in both engines: ``evaluate`` runs the evaluator, ``duckdb`` the BigQuery-on-DuckDB layer."""

    def __init__(self, tables: dict[str, Table]):
        import duckdb

        from kumosql import bigquery_on_duckdb as bq
        from kumosql.duckdb_load import insert_rows

        self.tables = tables
        self.database = Database(tables)
        self.dropped = has_unloadable(tables)  # columns the DuckDB copy lacks
        self.con = duckdb.connect(":memory:", config={"threads": 1})
        self.con.execute("SET memory_limit = '1GB'")
        bq.configure(self.con)
        self.loaded: set[str] = set()
        for name, table in tables.items():
            keep = [(i, c, t) for i, (c, t) in enumerate(table.columns) if c not in self.dropped.get(name, ())]
            if not keep:
                continue
            try:
                self.con.execute(f'CREATE TABLE "{name}" (' + ", ".join(f'"{c}" {duck_type(t)}' for _, c, t in keep) + ")")
                rows = [tuple(duck_value(t, row[i]) for i, _, t in keep) for row in table.rows]
                if rows:
                    insert_rows(self.con, f'"{name}"', rows)
                self.loaded.add(name.lower())
            except Exception:  # noqa: BLE001  - a table DuckDB cannot hold: a query reading it is not run
                self.con.execute(f'DROP TABLE IF EXISTS "{name}"')
        self.last_sql = ""

    def close(self) -> None:
        self.con.close()

    # the evaluator ------------------------------------------------------------------------------

    def evaluate(self, sql: str) -> Answer:
        armed = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
        try:
            if armed:
                signal.setitimer(signal.ITIMER_REAL, QUERY_SECONDS)
            result = evaluate(sql, self.database, TIME_ZONE, None, "bigquery")
        except Unsupported as problem:
            raise Skip("unsupported: " + (str(problem).strip().splitlines() or ["?"])[0][:100]) from None
        except AnalysisError as problem:
            raise Skip("analysis error: " + str(problem)[:100]) from None
        except EvalError as problem:
            raise Skip("evaluation error: " + str(problem)[:100]) from None
        except CaseTimeout:
            raise Skip("evaluator timeout") from None
        except RecursionError:
            raise Skip("evaluator recursion") from None
        except Exception as problem:  # noqa: BLE001  an evaluator crash is the conformance runner's business
            raise Skip(f"evaluator crash: {type(problem).__name__}") from None
        finally:
            if armed:
                signal.setitimer(signal.ITIMER_REAL, 0)
        if not result.deterministic:
            raise Skip("nondeterministic result")
        columns = [(name or "", t) for name, t in result.columns]
        for _, t in columns:
            _check_readable(t)
        return Answer(columns, [tuple(r) for r in result.rows], bool(result.ordered), bool(result.inexact))

    # the DuckDB layer ---------------------------------------------------------------------------

    def duck_sql(self, sql: str) -> str:
        from kumosql import bigquery_on_duckdb as bq
        from kumosql.counterexample import to_duckdb

        try:
            tree = sqlglot.parse_one(sql, read="bigquery")
        except Exception as problem:  # noqa: BLE001
            raise NotRun(f"sqlglot: {type(problem).__name__}") from None
        if self.dropped:
            names = {t.name.lower() for t in tree.find_all(exp.Table)} & {n.lower() for n in self.dropped}
            star = any(not isinstance(s.parent, exp.Count) for s in tree.find_all(exp.Star))
            if names and star:
                raise NotRun("a table has columns DuckDB cannot hold and the query uses *")
        used = {t.name.lower() for t in tree.find_all(exp.Table) if t.name}
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        missing = sorted(u for u in used - ctes if u in {n.lower() for n in self.tables} and u not in self.loaded)
        if missing:
            raise NotRun(f"table {missing[0]} cannot be loaded into DuckDB")
        try:
            return to_duckdb(sql, "bigquery")
        except bq.Unfaithful as problem:
            raise NotRun("refused: " + str(problem).split(": ", 1)[-1][:110], declined=True) from None
        except Exception as problem:  # noqa: BLE001
            raise NotRun(f"translation: {type(problem).__name__}: {str(problem)[:80]}") from None

    def duckdb(self, sql: str, optimizer: bool = True) -> list:
        import duckdb

        from kumosql import bigquery_on_duckdb as bq
        from kumosql.duckdb_load import run_unoptimized

        translated = self.duck_sql(sql)
        self.last_sql = translated
        statement = f"CREATE OR REPLACE TEMP TABLE kumo_diff_result AS {translated}"
        timer = threading.Timer(QUERY_SECONDS, self.con.interrupt)
        timer.start()
        try:
            self.con.execute("SET TimeZone = 'UTC'")
            if optimizer:
                self.con.execute(statement)
            else:
                run_unoptimized(self.con, statement)
            return bq.bigquery_rows(_read_table(self.con, "kumo_diff_result"))
        except bq.UnfaithfulOutput as problem:
            raise NotRun("output: " + str(problem).split(": ", 1)[-1][:100], declined=True) from None
        except duckdb.InterruptException:
            raise NotRun("duckdb timeout") from None
        except duckdb.Error as problem:
            text = str(problem)
            if bq.MARKER in text:
                raise NotRun("guard: " + text.split(bq.MARKER + ": ", 1)[-1].splitlines()[0][:100], declined=True) from None
            raise NotRun("duckdb: " + (text.splitlines() or ["?"])[0][:110]) from None
        except (OverflowError, ValueError, TypeError) as problem:
            raise NotRun(f"duckdb result: {type(problem).__name__}: {str(problem)[:80]}") from None
        finally:
            timer.cancel()


# --- shrinking a divergence -----------------------------------------------------------------------


def _used_tables(sql: str, known: set[str]) -> set[str]:
    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except Exception:  # noqa: BLE001
        return set(known)
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    return {t.name.lower() for t in tree.find_all(exp.Table) if t.name and t.name.lower() not in ctes} & known


def _children(node: exp.Expression) -> list[exp.Expression]:
    out = []
    for value in node.args.values():
        if isinstance(value, exp.Expression):
            out.append(value)
        elif isinstance(value, list):
            out.extend(v for v in value if isinstance(v, exp.Expression))
    return out


def _candidates(tree: exp.Expression):
    """Smaller trees to try: clauses dropped, select items dropped, a node replaced by one of its children or a literal."""

    for index, node in enumerate(list(tree.walk(bfs=False))):
        if isinstance(node, exp.Select):
            for key in ("where", "having", "qualify", "order", "limit", "group", "distinct", "offset", "joins", "windows", "from"):
                if node.args.get(key):
                    candidate = tree.copy()
                    target = list(candidate.walk(bfs=False))[index]
                    target.set(key, None)
                    yield candidate
            if len(node.expressions) > 1:
                for position in range(len(node.expressions)):
                    candidate = tree.copy()
                    target = list(candidate.walk(bfs=False))[index]
                    del target.expressions[position]
                    target.set("expressions", target.expressions)
                    yield candidate
    for index, node in enumerate(list(tree.walk(bfs=False))):
        if node is tree or isinstance(node, (exp.Select, exp.Table, exp.From, exp.Identifier, exp.DataType, exp.Var)):
            continue
        parent = node.parent
        if isinstance(node, exp.Alias):
            continue
        if isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal):
            continue  # DATE '2020-02-29' stays a date
        for child in _children(node):
            if isinstance(child, (exp.Identifier, exp.DataType, exp.Var)) and not isinstance(child, exp.Column):
                continue
            candidate = tree.copy()
            target = list(candidate.walk(bfs=False))[index]
            target.replace(child.copy())
            yield candidate
        if not isinstance(node, (exp.Null, exp.Literal, exp.Column, exp.Star)):
            for literal in ("NULL", "0", "1", "''"):
                candidate = tree.copy()
                target = list(candidate.walk(bfs=False))[index]
                target.replace(exp.maybe_parse(literal, dialect="bigquery"))
                yield candidate


def shrink(tables: dict[str, Table], sql: str, make_session: Callable[[dict[str, Table]], Any], budget: float = 6.0,
           max_checks: int = 160) -> tuple[dict[str, Table], str, Verdict | None, int]:
    """The smallest tables and query this search finds that still diverge (kind ``diverge``).

    Returns ``(tables, sql, verdict, checks)``; ``verdict`` is ``None`` when the input itself does not diverge.
    """

    deadline = time.perf_counter() + budget
    checks = 0

    def check(candidate_tables: dict[str, Table], candidate_sql: str) -> Verdict | None:
        nonlocal checks
        checks += 1
        session = make_session(candidate_tables)
        try:
            verdict = classify(session, candidate_sql)
        finally:
            close = getattr(session, "close", None)
            if close:
                close()
        return verdict if verdict.kind == "diverge" else None

    def out_of_budget() -> bool:
        return checks >= max_checks or time.perf_counter() > deadline

    verdict = check(tables, sql)
    if verdict is None:
        return tables, sql, None, checks
    # tables the query does not read
    used = _used_tables(sql, {n.lower() for n in tables})
    smaller = {n: t for n, t in tables.items() if n.lower() in used}
    if len(smaller) < len(tables):
        got = check(smaller, sql)
        if got is not None:
            tables, verdict = smaller, got
    # rows: remove chunks, then single rows
    for name in list(tables):
        size = max(1, len(tables[name].rows) // 2)
        while size >= 1 and not out_of_budget():
            start = 0
            while start < len(tables[name].rows) and not out_of_budget():
                rows = tables[name].rows
                trial_rows = rows[:start] + rows[start + size:]
                if len(trial_rows) == len(rows):
                    break
                trial = dict(tables)
                trial[name] = Table(tables[name].columns, trial_rows)
                got = check(trial, sql)
                if got is not None:
                    tables, verdict = trial, got
                else:
                    start += size
            size //= 2
    # the query: shrink until nothing smaller diverges
    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
        text = tree.sql(dialect="bigquery")
    except Exception:  # noqa: BLE001
        return tables, sql, verdict, checks
    if text != sql:
        got = check(tables, text)
        if got is None:
            return tables, sql, verdict, checks
        sql, verdict = text, got
    progress = True
    while progress and not out_of_budget():
        progress = False
        for candidate in _candidates(tree):
            if out_of_budget():
                break
            try:
                candidate_sql = candidate.sql(dialect="bigquery")
            except Exception:  # noqa: BLE001
                continue
            if len(candidate_sql) >= len(sql) or candidate_sql == sql:
                continue
            got = check(tables, candidate_sql)
            if got is not None:
                try:
                    tree = sqlglot.parse_one(candidate_sql, read="bigquery")
                except Exception:  # noqa: BLE001
                    continue
                sql, verdict, progress = candidate_sql, got, True
                break
    # rows once more, now the query is smaller
    for name in list(tables):
        index = 0
        while index < len(tables[name].rows) and not out_of_budget():
            rows = tables[name].rows
            trial = dict(tables)
            trial[name] = Table(tables[name].columns, rows[:index] + rows[index + 1:])
            got = check(trial, sql)
            if got is not None:
                tables, verdict = trial, got
            else:
                index += 1
    tables, verdict = _drop_columns(tables, sql, verdict, check, out_of_budget)
    return tables, sql, verdict, checks


def _drop_columns(tables, sql, verdict, check, out_of_budget):
    """Tables without the columns the query does not name (when the query has no ``*``)."""

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except Exception:  # noqa: BLE001
        return tables, verdict
    if any(not isinstance(s.parent, exp.Count) for s in tree.find_all(exp.Star)):
        return tables, verdict
    named = {c.name.lower() for c in tree.find_all(exp.Column)}
    trial = {}
    for name, table in tables.items():
        keep = [i for i, (c, _) in enumerate(table.columns) if c.lower() in named] or [0]  # a table keeps one column
        if len(keep) == len(table.columns):
            trial[name] = table
        else:
            trial[name] = Table([table.columns[i] for i in keep], [tuple(row[i] for i in keep) for row in table.rows])
    if not out_of_budget():
        got = check(trial, sql)
        if got is not None:
            return trial, got
    return tables, verdict


# --- reporting values -----------------------------------------------------------------------------


def show(value: Any, limit: int = 300) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + f"... ({len(text)} chars)"


def table_text(name: str, table: Table, limit: int = 12) -> str:
    columns = ", ".join(f"{c} {t}" for c, t in table.columns)
    rows = [show(tuple(_present(t, v) for (_, t), v in zip(table.columns, row)), 200) for row in table.rows[:limit]]
    more = f" ... {len(table.rows) - limit} more rows" if len(table.rows) > limit else ""
    return f"{name}({columns}): " + ("; ".join(rows) if rows else "no rows") + more


def _present(t: T.Type, value: Any) -> Any:
    """A payload as a reader would want it printed (a TIMESTAMP as a datetime, arrays as tuples)."""

    if value is None:
        return None
    if t.kind == "TIMESTAMP":
        return _micros_to_datetime(value)
    if t.kind == "ARRAY":
        return tuple(_present(t.elem, v) for v in value)
    if t.kind == "STRUCT":
        return tuple(_present(ft, v) for (_, ft), v in zip(t.fields, value))
    return value


def present_rows(columns: list, rows: list) -> list:
    return [tuple(_present(t, v) for (_, t), v in zip(columns, row)) for row in rows]


def functions_of(sql: str) -> list[str]:
    """The kinds of function, operator, cast and literal a query uses (what groups divergences with one cause)."""

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except Exception:  # noqa: BLE001
        return []
    names = set()
    for node in tree.walk():
        if isinstance(node, exp.Anonymous):
            names.add(str(node.this).upper())
        elif isinstance(node, exp.Cast):
            names.add("CAST->" + node.to.sql(dialect="bigquery"))
        elif isinstance(node, exp.Literal):
            if not node.is_string and re.search(r"[.eE]", node.this):
                names.add("float-literal")
        elif isinstance(node, (exp.Func, exp.Binary, exp.Unary)) and not isinstance(node, (exp.Alias, exp.Paren)):
            names.add(type(node).__name__)
    return sorted(names)


# --- the conformance corpus -----------------------------------------------------------------------


def load_conformance():
    path = ROOT / "tools" / "googlesql_conformance.py"
    spec = importlib.util.spec_from_file_location("googlesql_conformance", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses (postponed annotations) look their module up here
    spec.loader.exec_module(module)
    return module


_CONF = None


def conformance():
    global _CONF
    if _CONF is None:
        _CONF = load_conformance()
    return _CONF


def dev_cases(limit: int | None = None) -> list:
    """The claimed cases of the dev split (never the held-out split), at most ``limit`` of them, evenly spaced."""

    chosen = [c for c in conformance().cases("dev") if c.claimed]
    if limit is not None and limit < len(chosen):
        step = len(chosen) / max(1, limit)
        chosen = [chosen[int(i * step)] for i in range(limit)]
    return chosen


def corpus_skip(case, context) -> str | None:
    if case.options.get("parameters"):
        return "query parameters"
    conf = conformance()
    if conf.case_time_zone(case.options) != conf.DEFAULT_TIME_ZONE:
        return "case-specific time zone"
    blocked = context.blocked(case.sql)
    if blocked:
        return "fixture: " + blocked[:80]
    return None


def reference_exact(case, context) -> bool | None:
    """Whether the evaluator matches the compliance file's expected rows (Google's reference) on this case."""

    try:
        return conformance().run_case(case, context).verdict == "exact"
    except Exception:  # noqa: BLE001
        return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (int, bool, str)) or value is None:
        return value
    return repr(value)


def divergence_record(source: str, sql: str, tables: dict[str, Table], verdict: Verdict, original_sql: str, shrunk: bool,
                      reference: bool | None, checks: int) -> dict:
    return {
        "source": source,
        "sql": sql,
        "original_sql": original_sql,
        "minimized": shrunk,
        "tables": {
            n: {"columns": [[c, str(t)] for c, t in tb.columns], "rows": [_jsonable([_present(t, v) for (_, t), v in zip(tb.columns, r)]) for r in tb.rows]}
            for n, tb in tables.items()
        },
        "table_text": [table_text(n, tb) for n, tb in tables.items()],
        "result_columns": [[n, str(t)] for n, t in (verdict.columns or [])],
        "difference": verdict.difference,
        "evaluator_text": show(present_rows(verdict.columns or [], verdict.evaluator_rows or [])),
        "duckdb_text": show(verdict.duckdb_rows or []),
        "evaluator_rows": _jsonable(present_rows(verdict.columns or [], verdict.evaluator_rows or [])),
        "duckdb_rows": _jsonable(verdict.duckdb_rows or []),
        "duckdb_sql": verdict.duckdb_sql,
        "evaluator_matches_reference": reference,
        "functions": functions_of(sql),
        "shrink_checks": checks,
    }


# --- running a batch ------------------------------------------------------------------------------

MINIMIZE_BUDGET = 45.0  # seconds a worker spends shrinking, in all


def _tally(results: list[dict], sql: str, source: str, verdict: Verdict, extra: dict | None = None) -> None:
    results.append({"source": source, "kind": verdict.kind, "reason": verdict.reason, "declined": verdict.declined, **(extra or {})})


def run_batch(label: str, tables: dict[str, Table], queries: list[tuple[str, str, Any]], minimize: bool,
              reference: Callable[[Any, Any], bool | None] | None = None) -> tuple[list[dict], list[dict]]:
    """Run ``queries`` (``(source, sql, token)``) on one set of tables; returns ``(per-query results, divergences)``."""

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _alarm)
    sys.setrecursionlimit(4000)
    session = Session(tables)
    results: list[dict] = []
    divergences: list[dict] = []
    spent = 0.0
    try:
        for source, sql, token in queries:
            verdict = classify(session, sql)
            _tally(results, sql, source, verdict)
            if verdict.kind != "diverge":
                continue
            started = time.perf_counter()
            shrunk_tables, shrunk_sql, shrunk_verdict, checks = tables, sql, verdict, 0
            shrunk = False
            if minimize and spent < MINIMIZE_BUDGET:
                try:
                    t2, s2, v2, checks = shrink(tables, sql, Session)
                    if v2 is not None:
                        shrunk_tables, shrunk_sql, shrunk_verdict, shrunk = t2, s2, v2, True
                except CaseTimeout:
                    pass
                spent += time.perf_counter() - started
            ref = reference(token, session) if reference else None
            divergences.append(divergence_record(source, shrunk_sql, shrunk_tables, shrunk_verdict, sql, shrunk, ref, checks))
    finally:
        session.close()
    return results, divergences


_WORK: dict = {"units": {}, "minimize": True}


def _run_unit(key):
    kind, name = key
    unit = _WORK["units"][key]
    if kind == "file":
        conf = conformance()
        context = unit["context"]
        queries, skipped = [], []
        for case in unit["cases"]:
            reason = corpus_skip(case, context)
            source = f"{case.file}/{case.name}"
            if reason:
                skipped.append({"source": source, "kind": "skipped", "reason": reason, "declined": False})
            else:
                queries.append((source, case.sql, case))
        tables = dict(getattr(context.database, "_tables", {}))

        def reference(case, session):
            return reference_exact(case, context)

        results, divergences = run_batch(name, tables, queries, _WORK["minimize"], reference)
        return skipped + results, divergences
    queries = unit["queries"]
    results, divergences = run_batch(name, unit["tables"], queries, _WORK["minimize"])
    return results, divergences


def run_units(units: dict, jobs: int, minimize: bool) -> tuple[list[dict], list[dict]]:
    _WORK["units"] = units
    _WORK["minimize"] = minimize
    keys = sorted(units, key=lambda k: -units[k].get("weight", 1))
    results: list[dict] = []
    divergences: list[dict] = []
    if jobs > 1 and len(keys) > 1 and "fork" in multiprocessing.get_all_start_methods():
        with multiprocessing.get_context("fork").Pool(jobs) as pool:
            for r, d in pool.imap_unordered(_run_unit, keys):
                results.extend(r)
                divergences.extend(d)
    else:
        for key in keys:
            r, d = _run_unit(key)
            results.extend(r)
            divergences.extend(d)
    results.sort(key=lambda r: r["source"])
    divergences.sort(key=lambda d: (d["source"], d["sql"]))
    return results, divergences


# --- the random generator -------------------------------------------------------------------------

INT_LITERALS = ["0", "1", "-1", "2", "3", "7", "-7", "10", "100", "-100", "255", "1000000", "2147483648", "-2147483649"]
FLOAT_LITERALS = ["0.0", "1.5", "-2.5", "0.5", "2.5", "-0.5", "3.14159", "1e10", "-0.1", "1e-7", "100.0", "0.1", "2.0", "-3.5", "1234.5678"]
NUMERIC_LITERALS = ["0", "1.5", "-2.25", "123456.789", "0.000000001", "2.5", "3.5", "-0.5", "10", "0.1", "99999.999999999", "-1.000000005"]
STRING_LITERALS = ["''", "'a'", "'Hello World'", "' padded '", "'abc,def,ghi'", "'ÄÖü'", "'x.y.z'", "'123'", "'The Quick'", "'日本語'", "'AbC'", "'  '", "'ab'"]
DATE_LITERALS = ["2020-02-29", "2021-01-31", "1999-12-31", "2000-03-01", "2024-12-30", "2023-06-15", "2019-01-01", "2021-12-26", "1970-01-01"]
UNITS = ["DAY", "WEEK", "MONTH", "QUARTER", "YEAR"]
INT_COLUMNS = ["i", "j"]
FLOAT_COLUMNS = ["f", "g"]
NUMERIC_COLUMNS = ["n", "m"]
STRING_COLUMNS = ["s", "u"]
DATE_COLUMNS = ["d", "e"]



POOLS = {
    're_pattern': ["'a'", "'[aeiou]'", "'(.)(.)'", "'\\\\s+'", "'^'", "''"],
    're_replacement': ["'-'", "''", "'\\\\2\\\\1'", "'[\\\\0]'"],
    're_extract': ["'[a-z]+'", "'(\\\\d+)'", "'(.)(.)'", "'x'"],
    're_extract2': ["'[a-z]'", "'(.)'"],
    're_contains': ["'a'", "'^[A-Z]'", "'\\\\d'", "''"],
    'fmt_int': ["'%d'", "'%5d'", "'%05d'", "'%s'", "'[%s]'", "'%x'", "'%+d'"],
    'p0': ["' '", "'a'", "'ab'", "'x.'"],
    'p1': ["'*'", "'ab'", "' '"],
    'p2': ["'%Y-%m-%d'", "'%A %B %d'", "'%j'", "'%U %W %u %w'", "'%b %e %y'", "'%G-%V'"],
    'p3': ["','", "'.'", "''", "' '"],
    'p4': ["','", "'.'", "' '"],
    'p5': ["'|'", "''", "'--'"],
    'p6': ["'%s'", "'%10s'", "'%-6s|'", "'%.2s'"],
    'p7': ["'abc'", "'lo'", "''"],
    'p8': ["'xyz'", "'01'", "''"],
    'p9': ["'%Y-%m-%d %H:%M:%S'", "'%c'", "'%a %b'"],
    'p10': ["'%Y-%m-%d'", "'%d/%m/%Y'", "'%Y%m%d'", "'%b %d %Y'"],
    'p11': ["'%a%'", "'_b'", "'a%'", "'%'", "''"],
    'p_split': ["','", "'.'", "''", "' '"],
}


def _pick(rng: random.Random, items: list) -> Any:
    return items[rng.randrange(len(items))]


class Generator:
    """Typed random expressions, deterministic for a ``random.Random``. ``columns`` is whether a table is in scope."""

    def __init__(self, rng: random.Random, columns: bool = True):
        self.rng = rng
        self.columns = columns

    def expr(self, kind: str, depth: int) -> str:
        method = getattr(self, "gen_" + kind)
        return method(depth)

    def pool(self, name: str) -> str:
        return _pick(self.rng, POOLS[name])

    def number_text(self, depth: int) -> str:
        if self.rng.random() < 0.15:
            return self.gen_string(depth)
        return _pick(self.rng, ["'123'", "'-42'", "'1.5'", "'0'", "' 7 '", "'1e3'", "'0.1'", "'+5'", "'007'", "'9223372036854775807'"])

    def date_text(self, depth: int) -> str:
        return _pick(self.rng, ["'2020-03-01'", "'2021-1-5'", "'2020-02-29'", "'1999-12-31'", self.gen_string(depth)])

    def any_kind(self, kinds=("int", "float", "numeric", "string", "date")) -> str:
        return _pick(self.rng, list(kinds))

    # leaves ---------------------------------------------------------------------------------------

    def gen_int(self, depth: int) -> str:
        r = self.rng
        if depth <= 0 or r.random() < 0.18:
            if self.columns and r.random() < 0.6:
                return _pick(r, INT_COLUMNS)
            return _pick(r, INT_LITERALS)
        c = lambda: self.gen_int(depth - 1)  # noqa: E731
        choice = r.randrange(36)
        if choice < 4:
            return f"({c()} {_pick(r, ['+', '-', '*'])} {c()})"
        if choice == 4:
            return f"DIV({c()}, {c()})"
        if choice == 5:
            return f"MOD({c()}, {c()})"
        if choice == 6:
            return f"ABS({c()})"
        if choice == 7:
            return f"SIGN({c()})"
        if choice == 8:
            return f"LENGTH({self.gen_string(depth - 1)})"
        if choice == 9:
            return f"CAST({self.gen_float(depth - 1)} AS INT64)"
        if choice == 10:
            return f"CAST({self.gen_numeric(depth - 1)} AS INT64)"
        if choice == 11:
            return f"EXTRACT({_pick(r, ['YEAR', 'MONTH', 'DAY', 'DAYOFWEEK', 'DAYOFYEAR', 'WEEK', 'QUARTER', 'ISOWEEK', 'ISOYEAR'])} FROM {self.gen_date(depth - 1)})"
        if choice == 12:
            return f"DATE_DIFF({self.gen_date(depth - 1)}, {self.gen_date(depth - 1)}, {_pick(r, UNITS + ['ISOWEEK', 'ISOYEAR'])})"
        if choice == 13:
            return f"STRPOS({self.gen_string(depth - 1)}, {self.gen_string(depth - 1)})"
        if choice == 14:
            return f"IFNULL({c()}, {c()})"
        if choice == 15:
            return f"COALESCE({c()}, {c()}, {c()})"
        if choice == 16:
            return f"CASE WHEN {self.gen_bool(depth - 1)} THEN {c()} ELSE {c()} END"
        if choice == 17:
            return f"{_pick(r, ['GREATEST', 'LEAST'])}({c()}, {c()})"
        if choice == 18:
            return f"ASCII({self.gen_string(depth - 1)})"
        if choice == 19:
            return f"BIT_COUNT({c()})"
        if choice == 20:
            return f"({c()} {_pick(r, ['&', '|', '^'])} {c()})"
        if choice == 21:
            return f"({c()} {_pick(r, ['<<', '>>'])} {_pick(r, ['0', '1', '3', '63', '64', '65'])})"
        if choice == 22:
            return f"CAST({self.number_text(depth - 1)} AS INT64)"
        if choice == 23:
            return f"SAFE_CAST({self.gen_string(depth - 1)} AS INT64)"
        if choice == 24:
            return f"CAST({self.gen_bool(depth - 1)} AS INT64)"
        if choice == 25:
            return f"INSTR({self.gen_string(depth - 1)}, {self.gen_string(depth - 1)})"
        if choice == 26:
            return f"CHAR_LENGTH({self.gen_string(depth - 1)})"
        if choice == 27:
            return f"BYTE_LENGTH({self.gen_string(depth - 1)})"
        if choice == 28:
            return f"CAST(ROUND({self.gen_float(depth - 1)}) AS INT64)"
        if choice == 29:
            return f"CAST(FLOOR({self.gen_float(depth - 1)}) AS INT64)"
        if choice == 30:
            return f"SAFE_ADD({c()}, {c()})"
        if choice == 31:
            return f"SAFE_MULTIPLY({c()}, {c()})"
        if choice == 32:
            return f"UNIX_DATE({self.gen_date(depth - 1)})"
        if choice == 33:
            return f"-({c()})"
        if choice == 34:
            return f"RANGE_BUCKET({c()}, [0, 10, 100])"
        return f"(SELECT COUNT(*) FROM UNNEST(SPLIT({self.gen_string(depth - 1)}, {self.pool('p_split')})))"

    def gen_float(self, depth: int) -> str:
        r = self.rng
        if depth <= 0 or r.random() < 0.18:
            if self.columns and r.random() < 0.6:
                return _pick(r, FLOAT_COLUMNS)
            return _pick(r, FLOAT_LITERALS)
        c = lambda: self.gen_float(depth - 1)  # noqa: E731
        choice = r.randrange(30)
        if choice < 4:
            return f"({c()} {_pick(r, ['+', '-', '*', '/'])} {c()})"
        if choice == 4:
            return f"SQRT({c()})"
        if choice == 5:
            return f"{_pick(r, ['FLOOR', 'CEIL', 'TRUNC', 'ROUND', 'ABS'])}({c()})"
        if choice == 6:
            return f"ROUND({c()}, {_pick(r, ['0', '1', '2', '-1', '3'])})"
        if choice == 7:
            return f"TRUNC({c()}, {_pick(r, ['0', '1', '2', '-1'])})"
        if choice == 8:
            return f"{_pick(r, ['LN', 'LOG10', 'EXP', 'SIN', 'COS', 'ATAN', 'TANH', 'CBRT'])}({c()})"
        if choice == 9:
            return f"POW({c()}, {_pick(r, ['0.5', '2', '3', '-1', '0'])})"
        if choice == 10:
            return f"SAFE_DIVIDE({c()}, {c()})"
        if choice == 11:
            return f"CAST({self.gen_int(depth - 1)} AS FLOAT64)"
        if choice == 12:
            return f"CAST({self.gen_numeric(depth - 1)} AS FLOAT64)"
        if choice == 13:
            return f"IFNULL({c()}, {c()})"
        if choice == 14:
            return f"{_pick(r, ['GREATEST', 'LEAST'])}({c()}, {c()})"
        if choice == 15:
            return f"({c()} {_pick(r, ['+', '-', '*', '/'])} {self.gen_int(depth - 1)})"
        if choice == 16:
            return f"CASE WHEN {self.gen_bool(depth - 1)} THEN {c()} ELSE {c()} END"
        if choice == 17:
            return f"SAFE_CAST({self.gen_string(depth - 1)} AS FLOAT64)"
        if choice == 18:
            return f"LOG({c()}, {c()})"
        if choice == 19:
            return f"SIGN({c()})"
        if choice == 20:
            return f"IEEE_DIVIDE({c()}, {c()})"
        if choice == 21:
            return f"MOD({self.gen_int(depth - 1)}, {self.gen_int(depth - 1)}) + {c()}"
        if choice == 22:
            return f"ATAN2({c()}, {c()})"
        if choice == 23:
            return f"COT({c()})"
        if choice == 24:
            return f"SINH({c()})"
        if choice == 25:
            return f"TANH({c()})"
        if choice == 26:
            return f"-({c()})"
        if choice == 27:
            return f"CAST({self.gen_int(depth - 1)} AS FLOAT64) / {c()}"
        if choice == 28:
            return f"CAST({self.number_text(depth - 1)} AS FLOAT64)"
        return f"CAST(ROUND({c()}) AS FLOAT64)"

    def gen_numeric(self, depth: int) -> str:
        r = self.rng
        if depth <= 0 or r.random() < 0.18:
            if self.columns and r.random() < 0.6:
                return _pick(r, NUMERIC_COLUMNS)
            return f"NUMERIC '{_pick(r, NUMERIC_LITERALS)}'"
        c = lambda: self.gen_numeric(depth - 1)  # noqa: E731
        choice = r.randrange(20)
        if choice < 3:
            return f"({c()} {_pick(r, ['+', '-', '*', '/'])} {c()})"
        if choice == 3:
            return f"{_pick(r, ['FLOOR', 'CEIL', 'TRUNC', 'ROUND', 'ABS', 'SIGN'])}({c()})"
        if choice == 4:
            return f"ROUND({c()}, {_pick(r, ['0', '1', '2', '-1', '9', '10'])})"
        if choice == 5:
            return f"TRUNC({c()}, {_pick(r, ['0', '1', '2', '-1', '5'])})"
        if choice == 6:
            return f"CAST({self.gen_int(depth - 1)} AS NUMERIC)"
        if choice == 7:
            return f"CAST({self.gen_float(depth - 1)} AS NUMERIC)"
        if choice == 8:
            return f"IFNULL({c()}, {c()})"
        if choice == 9:
            return f"{_pick(r, ['GREATEST', 'LEAST'])}({c()}, {c()})"
        if choice == 10:
            return f"SAFE_DIVIDE({c()}, {c()})"
        if choice == 11:
            return f"({c()} {_pick(r, ['+', '-', '*'])} {self.gen_int(depth - 1)})"
        if choice == 12:
            return f"MOD({c()}, {c()})"
        if choice == 13:
            return f"DIV({c()}, {c()})" if False else f"CAST(DIV({self.gen_int(depth - 1)}, {self.gen_int(depth - 1)}) AS NUMERIC)"
        if choice == 14:
            return f"CASE WHEN {self.gen_bool(depth - 1)} THEN {c()} ELSE {c()} END"
        if choice == 15:
            return f"SAFE_CAST({self.gen_string(depth - 1)} AS NUMERIC)"
        if choice == 16:
            return f"CAST({self.number_text(depth - 1)} AS NUMERIC)"
        if choice == 17:
            return f"-({c()})"
        if choice == 18:
            return f"POW({c()}, {_pick(r, ['2', '3', '0', '-1'])})"
        return f"SQRT({c()})"

    def gen_string(self, depth: int) -> str:
        r = self.rng
        if depth <= 0 or r.random() < 0.18:
            if self.columns and r.random() < 0.6:
                return _pick(r, STRING_COLUMNS)
            return _pick(r, STRING_LITERALS)
        c = lambda: self.gen_string(depth - 1)  # noqa: E731
        choice = r.randrange(40)
        if choice == 0:
            return f"CONCAT({c()}, {c()})"
        if choice == 1:
            return f"{_pick(r, ['UPPER', 'LOWER', 'INITCAP', 'REVERSE', 'TRIM', 'LTRIM', 'RTRIM'])}({c()})"
        if choice == 2:
            return f"SUBSTR({c()}, {_pick(r, ['0', '1', '2', '-2', '5', '100'])})"
        if choice == 3:
            return f"SUBSTR({c()}, {_pick(r, ['0', '1', '2', '-2', '3'])}, {_pick(r, ['0', '1', '2', '5', '-1'])})"
        if choice == 4:
            return f"LEFT({c()}, {_pick(r, ['0', '1', '3', '10'])})"
        if choice == 5:
            return f"RIGHT({c()}, {_pick(r, ['0', '1', '3', '10'])})"
        if choice == 6:
            return f"REPLACE({c()}, {_pick(r, STRING_LITERALS)}, {_pick(r, STRING_LITERALS)})"
        if choice == 7:
            return f"{_pick(r, ['LTRIM', 'RTRIM', 'TRIM'])}({c()}, {self.pool('p0')})"
        if choice == 8:
            return f"{_pick(r, ['LPAD', 'RPAD'])}({c()}, {_pick(r, ['0', '1', '5', '12'])}, {self.pool('p1')})"
        if choice == 9:
            return f"REPEAT({c()}, {_pick(r, ['0', '1', '3'])})"
        if choice == 10:
            return f"CAST({self.gen_int(depth - 1)} AS STRING)"
        if choice == 11:
            return f"CAST({self.gen_date(depth - 1)} AS STRING)"
        if choice == 12:
            return f"CAST({self.gen_numeric(depth - 1)} AS STRING)"
        if choice == 13:
            return f"CAST({self.gen_float(depth - 1)} AS STRING)"
        if choice == 14:
            return f"CAST({self.gen_bool(depth - 1)} AS STRING)"
        if choice == 15:
            return f"IFNULL({c()}, {c()})"
        if choice == 16:
            return f"CASE WHEN {self.gen_bool(depth - 1)} THEN {c()} ELSE {c()} END"
        if choice == 17:
            return f"REGEXP_REPLACE({c()}, {self.pool('re_pattern')}, {self.pool('re_replacement')})"
        if choice == 18:
            return f"REGEXP_EXTRACT({c()}, {self.pool('re_extract')})"
        if choice == 19:
            return f"FORMAT_DATE({self.pool('p2')}, {self.gen_date(depth - 1)})"
        if choice == 20:
            return f"SPLIT({c()}, {self.pool('p3')})[SAFE_OFFSET({_pick(r, ['0', '1', '2', '-1'])})]"
        if choice == 21:
            return f"ARRAY_TO_STRING(SPLIT({c()}, {self.pool('p4')}), {self.pool('p5')})"
        if choice == 22:
            return f"TO_HEX(CAST({c()} AS BYTES))"
        if choice == 23:
            return f"CHR({_pick(r, ['65', '97', '233', '8364', '0', '48'])})"
        if choice == 24:
            return f"SOUNDEX({c()})"
        if choice == 25:
            return f"FORMAT({self.pool('fmt_int')}, {self.gen_int(depth - 1)})"
        if choice == 26:
            return f"FORMAT({self.pool('p6')}, {c()})"
        if choice == 27:
            return f"TRANSLATE({c()}, {self.pool('p7')}, {self.pool('p8')})"
        if choice == 28:
            return f"NORMALIZE({c()}, {_pick(r, ['NFC', 'NFD', 'NFKC'])})"
        if choice == 29:
            return f"SAFE_CONVERT_BYTES_TO_STRING(CAST({c()} AS BYTES))"
        if choice == 30:
            return f"CAST({self.gen_date(depth - 1)} AS STRING)"
        if choice == 31:
            return f"UPPER(CAST({self.gen_int(depth - 1)} AS STRING))"
        if choice == 32:
            return f"({c()} || {c()})"
        if choice == 33:
            return f"CAST(CAST({self.gen_date(depth - 1)} AS DATETIME) AS STRING)"
        if choice == 34:
            return f"REGEXP_EXTRACT({c()}, {self.pool('re_extract2')})"
        if choice == 35:
            return f"LOWER({c()})"
        if choice == 36:
            return f"CAST(TIMESTAMP_SECONDS({self.gen_int(depth - 1)}) AS STRING)"
        if choice == 37:
            return f"FORMAT_TIMESTAMP({self.pool('p9')}, TIMESTAMP_SECONDS({_pick(r, ['0', '86399', '1700000000', '-1'])}))"
        if choice == 38:
            return f"CAST(CAST({self.date_text(depth - 1)} AS DATE) AS STRING)"
        return f"SUBSTRING({c()}, {_pick(r, ['1', '2', '3'])})"

    def gen_date(self, depth: int) -> str:
        r = self.rng
        if depth <= 0 or r.random() < 0.2:
            if self.columns and r.random() < 0.6:
                return _pick(r, DATE_COLUMNS)
            return f"DATE '{_pick(r, DATE_LITERALS)}'"
        c = lambda: self.gen_date(depth - 1)  # noqa: E731
        choice = r.randrange(13)
        if choice < 2:
            return f"{_pick(r, ['DATE_ADD', 'DATE_SUB'])}({c()}, INTERVAL {_pick(r, ['0', '1', '-1', '2', '7', '12', '30', '400'])} {_pick(r, UNITS)})"
        if choice == 2:
            return f"DATE_TRUNC({c()}, {_pick(r, UNITS + ['ISOWEEK', 'ISOYEAR', 'WEEK(MONDAY)', 'WEEK(SATURDAY)'])})"
        if choice == 3:
            return f"LAST_DAY({c()}{_pick(r, ['', ', MONTH', ', YEAR', ', QUARTER', ', WEEK', ', ISOWEEK'])})"
        if choice == 4:
            return f"DATE({self.gen_int(depth - 1)}, {_pick(r, ['1', '2', '12'])}, {_pick(r, ['1', '28', '31'])})"
        if choice == 5:
            return f"IFNULL({c()}, {c()})"
        if choice == 6:
            return f"{_pick(r, ['GREATEST', 'LEAST'])}({c()}, {c()})"
        if choice == 7:
            return f"DATE_FROM_UNIX_DATE({self.gen_int(depth - 1)})"
        if choice == 8:
            return f"CASE WHEN {self.gen_bool(depth - 1)} THEN {c()} ELSE {c()} END"
        if choice == 9:
            return f"SAFE_CAST({self.gen_string(depth - 1)} AS DATE)"
        if choice == 10:
            return f"CAST({self.date_text(depth - 1)} AS DATE)"
        if choice == 11:
            return f"PARSE_DATE({self.pool('p10')}, {self.gen_string(depth - 1)})"
        return f"DATE_ADD({c()}, INTERVAL {self.gen_int(depth - 1)} {_pick(r, UNITS)})"

    def gen_bool(self, depth: int) -> str:
        r = self.rng
        if depth <= 0:
            return _pick(r, ["TRUE", "FALSE", "NULL"]) if not self.columns or r.random() < 0.4 else "b"
        kind = self.any_kind()
        choice = r.randrange(16)
        if choice < 4:
            a, b = self.gen_for_compare(kind, depth - 1)
            return f"({a} {_pick(r, ['=', '<', '>', '<=', '>=', '!=', '<>'])} {b})"
        if choice == 4:
            return f"({self.expr(kind, depth - 1)} IS {_pick(r, ['NULL', 'NOT NULL'])})"
        if choice == 5:
            return f"({self.gen_string(depth - 1)} LIKE {self.pool('p11')})"
        if choice == 6:
            return f"{_pick(r, ['STARTS_WITH', 'ENDS_WITH'])}({self.gen_string(depth - 1)}, {self.gen_string(depth - 1)})"
        if choice == 7:
            return f"REGEXP_CONTAINS({self.gen_string(depth - 1)}, {self.pool('re_contains')})"
        if choice == 8:
            a = self.expr(kind, depth - 1)
            return f"({a} IN ({self.expr(kind, depth - 1)}, {self.expr(kind, depth - 1)}))"
        if choice == 9:
            a, b = self.gen_for_compare(kind, depth - 1)
            return f"({a} BETWEEN {b} AND {self.expr(kind, depth - 1)})"
        if choice == 10:
            return f"({self.gen_bool(depth - 1)} {_pick(r, ['AND', 'OR'])} {self.gen_bool(depth - 1)})"
        if choice == 11:
            return f"(NOT {self.gen_bool(depth - 1)})"
        if choice == 12:
            a, b = self.gen_for_compare(kind, depth - 1)
            return f"({a} IS {_pick(r, ['DISTINCT FROM', 'NOT DISTINCT FROM'])} {b})"
        if choice == 13:
            return f"IFNULL({self.gen_bool(depth - 1)}, {self.gen_bool(depth - 1)})"
        if choice == 14:
            return f"(SAFE_CAST({self.gen_string(depth - 1)} AS BOOL))"
        return f"({self.gen_int(depth - 1)} {_pick(r, ['=', '<', '>'])} {self.gen_float(depth - 1)})"

    def gen_for_compare(self, kind: str, depth: int) -> tuple[str, str]:
        if kind in ("int", "float", "numeric") and self.rng.random() < 0.3:
            other = self.any_kind(("int", "float", "numeric"))
            if {kind, other} != {"float", "numeric"}:  # BigQuery has no FLOAT64 / NUMERIC supertype for comparison
                return self.expr(kind, depth), self.expr(other, depth)
        return self.expr(kind, depth), self.expr(kind, depth)

    def gen_array(self, depth: int) -> str:
        r = self.rng
        kind = _pick(r, ["int", "float", "string", "date"])
        c = lambda: self.expr(kind, depth - 1)  # noqa: E731
        choice = r.randrange(8)
        if choice == 0:
            return f"[{c()}, {c()}, {c()}]"
        if choice == 1:
            return f"GENERATE_ARRAY({_pick(r, ['1', '0', '-3'])}, {_pick(r, ['5', '0', '10'])}, {_pick(r, ['1', '2', '3', '-1'])})"
        if choice == 2:
            return f"SPLIT({self.gen_string(depth - 1)}, {self.pool('p4')})"
        if choice == 3:
            return f"ARRAY_REVERSE([{c()}, {c()}])"
        if choice == 4:
            return f"GENERATE_DATE_ARRAY({self.gen_date(depth - 1)}, DATE_ADD({self.gen_date(depth - 1)}, INTERVAL {_pick(r, ['0', '3', '40'])} DAY), INTERVAL {_pick(r, ['1', '2', '1'])} {_pick(r, ['DAY', 'WEEK', 'MONTH'])})"
        if choice == 5:
            return f"ARRAY(SELECT x FROM UNNEST([{c()}, {c()}, {c()}]) AS x WHERE x IS NOT NULL ORDER BY x {_pick(r, ['ASC', 'DESC'])})"
        if choice == 6:
            return f"ARRAY_CONCAT([{c()}], [{c()}, {c()}])"
        return f"ARRAY(SELECT x FROM UNNEST({self.gen_array(depth - 1)}) AS x ORDER BY x)" if depth > 1 else f"[{c()}]"


def random_tables(seed: int) -> dict[str, Table]:
    """The one table every random query reads, deterministic for ``seed``: edge values and NULLs, 10 rows."""

    rng = random.Random(f"tables:{seed}")
    ints = [0, 1, -1, 2, 7, -7, 10, 255, 2**31, -(2**31), 12345, None]
    floats = [0.0, 1.5, -2.5, 0.5, 2.5, -0.5, 3.14159, 1e10, -0.1, 1e-7, 100.0, 1234.5678, None]
    numerics = ["0", "1.5", "-2.25", "123456.789", "0.000000001", "2.5", "3.5", "-0.5", "10", "99999.999999999", None]
    strings = ["", "a", "Hello World", " padded ", "abc,def,ghi", "ÄÖü", "x.y.z", "123", "The Quick", "日本語", "AbC", "  ", "-42", None]
    dates = ["2020-02-29", "2021-01-31", "1999-12-31", "2000-03-01", "2024-12-30", "2023-06-15", "2019-01-01", "2021-12-26", "1970-01-01", None]

    def many(items, k):
        return [items[rng.randrange(len(items))] for _ in range(k)]

    rows = []
    for index in range(10):
        n1, n2 = many(numerics, 2)
        d1, d2 = many(dates, 2)
        rows.append((
            index, *many(ints, 2), *many(floats, 2),
            Decimal(n1) if n1 is not None else None, Decimal(n2) if n2 is not None else None,
            *many(strings, 2),
            date.fromisoformat(d1) if d1 else None, date.fromisoformat(d2) if d2 else None,
            rng.choice([True, False, None]),
        ))
    columns = [("id", T.INT64), ("i", T.INT64), ("j", T.INT64), ("f", T.FLOAT64), ("g", T.FLOAT64), ("n", T.NUMERIC), ("m", T.NUMERIC),
               ("s", T.STRING), ("u", T.STRING), ("d", T.DATE), ("e", T.DATE), ("b", T.BOOL)]
    return {"t": Table(columns, rows)}


def random_query(seed: int, index: int) -> str:
    """The ``index``-th random query of ``seed``: one of a few shapes over the table ``t`` (or no table)."""

    rng = random.Random(f"query:{seed}:{index}")
    depth = _pick(rng, [1, 2, 2, 3, 3, 4])
    shape = rng.randrange(20)
    gen = Generator(rng, columns=shape >= 4)
    kind = _pick(rng, ["int", "float", "numeric", "string", "date", "bool", "int", "string", "array"])
    if kind == "array":
        expression = gen.gen_array(max(1, depth - 1))
    else:
        expression = gen.expr(kind, depth)
    if shape < 4:
        return f"SELECT {expression} AS x"
    if shape < 14:
        return f"SELECT {expression} AS x FROM t ORDER BY id"
    if shape < 16:
        return f"SELECT id, {expression} AS x FROM t WHERE {gen.gen_bool(2)} ORDER BY id"
    if shape < 18:
        agg_kind = _pick(rng, ["int", "float", "numeric", "string", "date"])
        value = gen.expr(agg_kind, max(1, depth - 2))
        funcs = {
            "int": ["SUM", "AVG", "MIN", "MAX", "COUNT", "COUNT DISTINCT", "ANY_VALUE", "BIT_OR", "BIT_XOR", "STDDEV", "VARIANCE"],
            "float": ["SUM", "AVG", "MIN", "MAX", "COUNT", "STDDEV_POP", "VAR_SAMP", "STDDEV"],
            "numeric": ["SUM", "AVG", "MIN", "MAX", "COUNT"],
            "string": ["MIN", "MAX", "COUNT", "COUNT DISTINCT", "STRING_AGG", "STRING_AGG ORDERED"],
            "date": ["MIN", "MAX", "COUNT", "COUNT DISTINCT"],
        }[agg_kind]
        func = _pick(rng, funcs)
        if func == "COUNT DISTINCT":
            call = f"COUNT(DISTINCT {value})"
        elif func == "STRING_AGG":
            call = f"STRING_AGG({value}, '|')"
        elif func == "STRING_AGG ORDERED":
            call = f"STRING_AGG({value}, '|' ORDER BY {value}, id)"
        else:
            call = f"{func}({value})"
        group = _pick(rng, ["", "", "GROUP BY b ORDER BY b", "GROUP BY MOD(id, 3) ORDER BY MOD(id, 3)"])
        lead = {"": "", "GROUP BY b ORDER BY b": "b, ", "GROUP BY MOD(id, 3) ORDER BY MOD(id, 3)": "MOD(id, 3) AS k, "}[group]
        return f"SELECT {lead}{call} AS x FROM t {group}".strip()
    if shape == 18:
        return f"SELECT {expression} AS x, {gen.expr(_pick(rng, ['int', 'float', 'string', 'date']), depth)} AS y FROM t ORDER BY id"
    function = _pick(rng, ["SUM", "MIN", "MAX", "COUNT"])
    return f"SELECT id, {function}({gen.gen_int(2)}) OVER (ORDER BY id{_pick(rng, ['', ' ROWS BETWEEN 1 PRECEDING AND CURRENT ROW', ' ROWS BETWEEN CURRENT ROW AND 2 FOLLOWING'])}) AS x FROM t ORDER BY id"


def random_units(count: int, seed: int, jobs: int) -> dict:
    if count <= 0:
        return {}
    tables = random_tables(seed)
    chunk = max(25, -(-count // (jobs * 4)))
    units = {}
    for start in range(0, count, chunk):
        stop = min(count, start + chunk)
        queries = [(f"random:{seed}:{i}", random_query(seed, i), None) for i in range(start, stop)]
        units[("random", f"{seed}:{start}")] = {"tables": tables, "queries": queries, "weight": stop - start}
    return units


def corpus_units(limit: int | None) -> tuple[dict, int]:
    conf = conformance()
    by_file: dict[str, list] = {}
    for case in dev_cases(limit):
        by_file.setdefault(case.file, []).append(case)
    units = {}
    for name, cases_here in by_file.items():
        units[("file", name)] = {"cases": cases_here, "context": conf.FileContext(cases_here[0].tables), "weight": len(cases_here)}
    return units, sum(len(v) for v in by_file.values())


# --- the report -----------------------------------------------------------------------------------


def reason_group(reason: str) -> str:
    text = " ".join(reason.split())
    text = re.sub(r"(?<![A-Za-z_\d])\d+(?:\.\d+)?", "#", text)
    return text[:100]


def summarize(results: list[dict], divergences: list[dict], meta: dict) -> dict:
    counts = Counter(r["kind"] for r in results)
    not_run = [r for r in results if r["kind"] == "not_run"]
    answered = len(results) - counts["skipped"]
    summary = {
        **meta,
        "queries": len(results),
        "evaluator_answered": answered,
        "evaluator_skipped": counts["skipped"],
        "compared": counts["agree"] + counts["diverge"] + counts["optimizer"] + counts["unstable"],
        "agree": counts["agree"],
        "declined_or_not_run": len(not_run),
        "declined": sum(1 for r in not_run if r["declined"]),
        "not_run": sum(1 for r in not_run if not r["declined"]),
        "diverge": counts["diverge"],
        "diverge_optimizer_explained": counts["optimizer"],
        "unstable": counts["unstable"],
        "by_source": {
            kind: {
                "answered": sum(1 for r in results if r["source"].startswith("random:") == (kind == "random") and r["kind"] != "skipped"),
                "agree": sum(1 for r in results if r["source"].startswith("random:") == (kind == "random") and r["kind"] == "agree"),
                "diverge": sum(1 for r in results if r["source"].startswith("random:") == (kind == "random") and r["kind"] == "diverge"),
            }
            for kind in ("conformance", "random")
        },
        "evaluator_skipped_reasons": Counter(reason_group(r["reason"]) for r in results if r["kind"] == "skipped").most_common(15),
        "not_run_reasons": Counter(reason_group(r["reason"]) for r in not_run).most_common(25),
        "divergences": dedupe(divergences),
    }
    summary["groups"] = group_divergences(summary["divergences"])
    return summary


def dedupe(divergences: list[dict]) -> list[dict]:
    """One entry per distinct (minimized query, tables); the other sources that reach it are listed under ``also``."""

    merged: dict[str, dict] = {}
    for d in divergences:
        key = json.dumps([d["sql"], d["tables"]], sort_keys=True)
        if key in merged:
            merged[key].setdefault("also", []).append(d["source"])
        else:
            merged[key] = dict(d)
    return list(merged.values())


def group_divergences(divergences: list[dict]) -> list[dict]:
    """Divergences grouped by what they share (kind of difference, result types, functions): each group is one likely cause."""

    groups: dict[str, list[dict]] = {}
    for d in divergences:
        key = json.dumps([d["difference"], [t for _, t in d["result_columns"]], d["functions"]])
        groups.setdefault(key, []).append(d)
    out = []
    for key, members in groups.items():
        members.sort(key=lambda d: (len(d["sql"]), sum(len(r) for r in d["table_text"]), d["source"]))
        difference, types, functions = json.loads(key)
        out.append({"difference": difference, "result_types": types, "functions": functions, "count": len(members),
                    "example": members[0], "others": [d["sql"] for d in members[1:6]]})
    out.sort(key=lambda g: (-g["count"], g["example"]["sql"]))
    return out


def render(summary: dict, show_all: int = 40) -> str:
    out = []
    out.append(
        f"GoogleSQL evaluator vs BigQuery-on-DuckDB ({summary['seconds']}s): {summary['queries']} queries, "
        f"{summary['evaluator_answered']} answered by the evaluator, {summary['evaluator_skipped']} skipped (unsupported, error, parameters...)"
    )
    out.append(f"  compared                    {summary['compared']:>6}")
    out.append(f"  agree                       {summary['agree']:>6}")
    out.append(f"  declined / not run          {summary['declined_or_not_run']:>6}  ({summary['declined']} declined by the layer, {summary['not_run']} DuckDB or sqlglot could not run)")
    out.append(f"  DIVERGE                     {summary['diverge']:>6}  ({len(summary['divergences'])} distinct after shrinking)")
    out.append(f"  diverge, optimizer explained{summary['diverge_optimizer_explained']:>5}")
    if summary["unstable"]:
        out.append(f"  unstable (optimizer-off run agrees with neither) {summary['unstable']}")
    for kind, s in summary["by_source"].items():
        out.append(f"    {kind:<12} answered {s['answered']:>5}  agree {s['agree']:>5}  diverge {s['diverge']:>4}")
    out.append("")
    out.append("DuckDB side declined or not run, by reason:")
    for reason, n in summary["not_run_reasons"][:12]:
        out.append(f"  {n:>5}  {reason}")
    out.append("")
    out.append("evaluator did not answer, by reason:")
    for reason, n in summary["evaluator_skipped_reasons"][:8]:
        out.append(f"  {n:>5}  {reason}")
    groups = summary["groups"]
    if groups:
        out.append("")
        out.append(f"divergences, {len(summary['divergences'])} distinct repros in {len(groups)} groups (same difference, result types and functions); shrunk:")
    for index, g in enumerate(groups[:show_all], 1):
        d = g["example"]
        ref = {True: "the evaluator matches Google's expected rows, so DuckDB is wrong", False: "the evaluator does not match the expected rows", None: ""}[
            d["evaluator_matches_reference"]
        ]
        also = f" (+{len(d.get('also', []))} same repro)" if d.get("also") else ""
        out.append(f"--- {index}. [{g['difference']}] {g['count']} repro{'s' if g['count'] != 1 else ''}; {d['source']}{also}" + ("" if d["minimized"] else " [not shrunk]"))
        out.append("    functions: " + ", ".join(g["functions"]))
        out.append("    query:     " + d["sql"].replace("\n", "\n               "))
        for line in d["table_text"]:
            out.append("    table:     " + line)
        out.append("    columns:   " + ", ".join(f"{n or '_'} {t}" for n, t in d["result_columns"]))
        out.append("    evaluator: " + d["evaluator_text"])
        out.append("    duckdb:    " + d["duckdb_text"])
        out.append("    duckdb sql: " + d["duckdb_sql"].replace("\n", " ")[:400])
        if ref:
            out.append("    note:      " + ref)
        if g["others"]:
            out.append("    also:      " + " | ".join(q[:90] for q in g["others"][:3]))
    if len(groups) > show_all:
        out.append(f"... {len(groups) - show_all} more groups in the JSON report")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="run about this many conformance dev cases, evenly spaced (default: all claimed cases)")
    parser.add_argument("--random", type=int, default=0, metavar="N", help="also run N random expression queries (default 0)")
    parser.add_argument("--seed", type=int, default=0, help="seed of the random queries and their table (default 0)")
    parser.add_argument("--no-conformance", action="store_true", help="skip the conformance cases (random queries only)")
    parser.add_argument("--json", metavar="PATH", help="write the full report as JSON")
    parser.add_argument("--no-shrink", action="store_true", help="report divergences as found, without shrinking them")
    parser.add_argument("--show", type=int, default=40, help="how many divergences to print (default 40)")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1), help="worker processes (default min(4, cpus))")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    units: dict = {}
    corpus_count = 0
    if not args.no_conformance:
        units, corpus_count = corpus_units(args.limit)
    units.update(random_units(args.random, args.seed, max(1, args.jobs)))
    results, divergences = run_units(units, max(1, args.jobs), not args.no_shrink)
    meta = {
        "split": "dev",
        "conformance_cases_selected": corpus_count,
        "random_queries": args.random,
        "seed": args.seed,
        "limit": args.limit,
        "seconds": round(time.perf_counter() - started, 1),
    }
    summary = summarize(results, divergences, meta)
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=1, default=str) + "\n", encoding="utf-8")
    print(render(summary, args.show))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
