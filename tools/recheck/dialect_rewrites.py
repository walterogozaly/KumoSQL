"""Proven pairs and rewrites of the cross-dialect, rewrite-benchmark and BigQuery-corpus evals.

Each adapter proves a pair with the same entry point and options as its eval and turns it into the Case that eval's
own executed check runs (or, where the eval runs nothing on a proof, the closest honest reading, said so below).

* ``dlbench`` (``tools/dlbench_bench.py``): DLBench translations proved equal to their source. The Case is the
  eval's first check: both queries as the prover read them (``as_source``), each in the source's engine, which
  for a SQLite source is SQLite itself (python ``sqlite3``, the declared column types), and for a MySQL or
  PostgreSQL source (where the eval runs nothing) DuckDB after sqlglot translates from that dialect.
* ``dlbench-target``: the source against the translation as its own dialect reads it, in DuckDB. For a DuckDB
  target it is the eval's second check (the translation "natively in DuckDB"); for the other five databases
  it is an extension the eval does not run (sqlglot's translation to DuckDB stands in for the target).
* ``llm-sql-solver-relaxed``, ``llm-sql-solver-negatives`` (``tools/llm_sql_solver_bench.py``): the counted
  proofs, run in SQLite as the eval runs its refutation; ``llm-sql-solver-uncounted`` holds the pairs the prover
  proves but the eval does not count (mixed-type comparisons).
* ``sql-rewritebench`` (``tools/rewrite_bench.py``), ``wetune-issues`` (``tools/wetune_bench.py``),
  ``clickbench-rewrites`` (``tools/clickbench_bench.py``): ``query_optimizer`` rewrites (and WeTune's developer
  rewrites the prover verifies), PostgreSQL or MySQL translated to DuckDB. The eval runs SQL-RewriteBench and
  ClickBench on PostgreSQL (with an ``EXPLAIN`` cost guard on the rewriter in the published run, which no adapter
  can reproduce: the Case holds every proven rewrite the optimizer emits without it); WeTune's eval runs nothing.
  ``wetune-issues-mysql-ci`` repeats WeTune's MySQL pairs with MySQL's case-insensitive string comparison.
* ``llm-r2-scale`` (``tools/llmr2_bench.py``, LLM-R2's test split, all held out; ``llm-r2-scale-train`` for the
  train split): proven rewrites of BigQuery queries, run as the eval runs them (``transformation_bench._duck``)
  over the DDL types of the data it loads (TPC-H, TPC-DS).
* ``spider2-bigquery`` (``tools/spider2_bench.py``): proven cleanup and format rewrites of Spider 2.0's BigQuery
  gold queries, run through ``kumosql.bigquery_on_duckdb`` over tables inferred from the queries (the eval runs
  nothing: the data are public BigQuery tables).

External data (never committed) is found through environment variables: ``KUMOSQL_BENCH_DIR`` (LLM-R2
queries and DSB's ``tpcds.sql``, as ``tools/benchmark_corpora.py`` fetches them), and
``KUMOSQL_RECHECK_REWRITEBENCH`` (a SQL-RewriteBench checkout), ``KUMOSQL_RECHECK_TPCDS_KIT`` (a
gregrahn/tpcds-kit checkout, for the TPC-DS catalog), ``KUMOSQL_RECHECK_WETUNE`` (WeTune-code) and
``KUMOSQL_RECHECK_CLICKBENCH`` (ClickBench). An eval whose data is missing lists no pairs.

Extensions of the engine (``engine.py`` is not edited; they are installed only when one of these
adapters builds a case, so other adapters in other processes never see them):

* ``HybridRunner``: a case whose ``meta["engines"]`` names ``sqlite`` runs that side in SQLite
  (tables from ``meta["sqlite_tables"]``, the declared types; dates are stored as ISO text, as SQLite
  stores them), the other side in DuckDB. ``meta["results"]`` post-processes rows: ``text-dates``
  writes dates and timestamps as ISO text (SQLite has no date type), ``bigquery`` reads rows as
  BigQuery returns them (``bigquery_on_duckdb.bigquery_rows``; a row BigQuery cannot return is a
  one-side error, as a guard is).
* ``literal_pool`` also turns year and year-month string literals (``'2021'``, ``'2008-11'``) and
  integer years into dates (first and last day), so date columns reach the years the queries test.
* Typed division: PostgreSQL, MonetDB and SQLite divide integers by truncating; sqlglot writes their
  ``/`` as DuckDB's float division, so it is written as ``kumo_tdiv`` (an error on zero, as
  PostgreSQL) or ``kumo_tdiv_null`` (NULL on zero, as SQLite), macros in ``Case.setup``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
import datetime as dt
from decimal import Decimal
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading

TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
for _path in (str(TOOLS), str(ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

from recheck import engine  # noqa: E402
from recheck.engine import Case, Column, Table  # noqa: E402

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

_INTS = "('BIGINT', 'INTEGER', 'HUGEINT', 'SMALLINT', 'TINYINT', 'UBIGINT', 'UINTEGER', 'USMALLINT', 'UTINYINT')"
MACROS = (
    f"CREATE OR REPLACE TEMP MACRO kumo_tdiv(a, b) AS CASE WHEN b = 0 THEN error('division by zero') "
    f"WHEN typeof(a) IN {_INTS} AND typeof(b) IN {_INTS} THEN a // b ELSE a / b END",
    f"CREATE OR REPLACE TEMP MACRO kumo_tdiv_null(a, b) AS CASE WHEN b = 0 THEN NULL "
    f"WHEN typeof(a) IN {_INTS} AND typeof(b) IN {_INTS} THEN a // b ELSE a / b END",
    # ClickHouse's byte-based substring (offset 0 or a length of 0 or less gives '')
    "CREATE OR REPLACE TEMP MACRO kumo_ch_substr2(s, o) AS CASE WHEN o = 0 THEN '' ELSE decode(array_slice(encode(s), o, strlen(s))) END",
    "CREATE OR REPLACE TEMP MACRO kumo_ch_substr3(s, o, n) AS CASE WHEN o = 0 OR n <= 0 THEN '' "
    "ELSE decode(array_slice(encode(s), o, o + n - 1)) END",
)


# --- translation ---------------------------------------------------------------------------------


def tree_to_duckdb(tree: exp.Expression, read: str = "", caseless: bool = False) -> str:
    """DuckDB SQL for a parsed tree, with typed (integer-truncating) division kept; for ClickHouse,
    ``length`` and ``substring`` count bytes (``lengthUTF8``/``substringUTF8`` count characters), which
    sqlglot writes as DuckDB's character functions. ``caseless`` reads ``LIKE`` as MySQL does (ignoring case)."""

    tree = tree.copy()

    def put(node, call):
        nonlocal tree
        if node is tree:
            tree = call
        else:
            node.replace(call)

    for div in reversed(list(tree.find_all(exp.Div))):
        if div.args.get("typed"):
            name = "kumo_tdiv_null" if div.args.get("safe") else "kumo_tdiv"
            put(div, exp.Anonymous(this=name, expressions=[div.this, div.expression]))
    if read == "clickhouse":
        for node in reversed(list(tree.find_all(exp.Length, exp.Substring))):
            if isinstance(node, exp.Length):
                put(node, exp.Anonymous(this="strlen", expressions=[node.this]))
            else:
                args = [node.this, node.args.get("start") or exp.Literal.number(1)]
                if node.args.get("length") is not None:
                    args.append(node.args["length"])
                put(node, exp.Anonymous(this=f"kumo_ch_substr{len(args)}", expressions=args))
    if caseless:
        for like in reversed(list(tree.find_all(exp.Like))):
            put(like, exp.ILike(this=like.this, expression=like.expression, escape=like.args.get("escape")))
    return tree.sql(dialect="duckdb")


def to_duckdb(sql: str, read: str, caseless: bool = False) -> str:
    return tree_to_duckdb(sqlglot.parse_one(sql, read=read), read, caseless)


def bigquery_to_duckdb(tree: exp.Expression) -> tuple[str, bool]:
    """DuckDB SQL that evaluates as BigQuery does (``bigquery_on_duckdb``), or sqlglot's plain
    translation (as ``transformation_bench._duck`` runs it) when no faithful reading exists."""

    from kumosql import bigquery_on_duckdb as bq

    try:
        return bq.to_duckdb_sql(tree), True
    except bq.Unfaithful:
        return tree.sql(dialect="duckdb"), False


def bigquery_setup() -> tuple[str, ...]:
    from kumosql import bigquery_on_duckdb as bq

    return tuple(bq.SETTINGS) + tuple(bq.MACROS)


# --- engine extensions ---------------------------------------------------------------------------


def _sqlite_value(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    return value


def _text_dates(value):
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.isoformat()
    return value


class HybridRunner:
    """The engine's ``Runner`` interface, with each side on its own engine (see the module doc)."""

    def __init__(self, case: Case, query_seconds: float, original):
        self.case = case
        self.query_seconds = query_seconds
        self.engines = tuple(case.meta.get("engines") or ("duckdb", "duckdb"))
        self.post = case.meta.get("results", "")
        self.duck = original(case, query_seconds) if "duckdb" in self.engines else None
        self.lite = None
        if "sqlite" in self.engines:
            self.lite = sqlite3.connect(":memory:", check_same_thread=False)
            for name, columns in case.meta["sqlite_tables"].items():
                body = ", ".join(f'"{c}" {t}'.rstrip() for c, t in columns)
                self.lite.execute(f'CREATE TABLE "{name}" ({body})')
        self.turn = 0

    @property
    def db(self):
        return self.duck.db if self.duck is not None else None

    def close(self) -> None:
        if self.duck is not None:
            self.duck.close()
        if self.lite is not None:
            self.lite.close()

    def load(self, data) -> None:
        self.turn = 0
        if self.duck is not None:
            self.duck.load(data)
        if self.lite is not None:
            try:
                for name in self.case.meta["sqlite_tables"]:
                    self.lite.execute(f'DELETE FROM "{name}"')
                for name, rows in data.items():
                    if rows:
                        marks = ", ".join("?" * len(rows[0]))
                        self.lite.executemany(f'INSERT INTO "{name}" VALUES ({marks})', [tuple(_sqlite_value(v) for v in row) for row in rows])
            except sqlite3.Error as error:
                raise engine.QueryError(f"load: {error}") from None

    def _post(self, rows: list[tuple]) -> list[tuple]:
        if self.post == "text-dates":
            return [tuple(_text_dates(v) for v in row) for row in rows]
        if self.post == "bigquery":
            from kumosql import bigquery_on_duckdb as bq

            try:
                return bq.bigquery_rows(rows)
            except bq.UnfaithfulOutput as error:
                raise engine.QueryError(str(error)[:300]) from None
        return rows

    def _sqlite(self, sql: str) -> list[tuple]:
        timer = threading.Timer(self.query_seconds, self.lite.interrupt)
        timer.start()
        try:
            return self.lite.execute(sql).fetchall()
        except (sqlite3.Error, OverflowError, ValueError) as error:
            raise engine.QueryError(f"sqlite: {type(error).__name__}: {str(error)[:300]}") from None
        finally:
            timer.cancel()

    def _execute(self, side: int, sql: str, unoptimized: bool = False) -> list[tuple]:
        if self.engines[side] == "sqlite":
            return self._post(self._sqlite(sql))
        if not unoptimized:
            return self._post(self.duck.run(sql))
        from kumosql.duckdb_load import run_unoptimized

        timer = threading.Timer(self.query_seconds * 2, self.duck.db.interrupt)
        timer.start()
        try:
            rows = run_unoptimized(self.duck.db, sql)[0]
        except self.duck.duckdb.Error as error:
            raise engine.QueryError(f"{type(error).__name__}: {str(error).splitlines()[0][:300]}") from None
        finally:
            timer.cancel()
        return self._post(rows)

    def run(self, sql: str) -> list[tuple]:
        # ``engine.compare`` runs the left query, then the right one, after each load
        side, self.turn = self.turn, self.turn ^ 1
        return self._execute(side, sql)

    def run_side(self, side: int, sql: str) -> list[tuple]:
        """``sql`` on the engine of ``side`` (0 left, 1 right), whatever turn it is (the tie probe uses this)."""

        return self._execute(side, sql)

    def dialect_of(self, side: int) -> str:
        return "sqlite" if self.engines[side] == "sqlite" else "duckdb"

    def unoptimized(self) -> tuple[list[tuple], list[tuple]]:
        self.turn = 0
        return self._execute(0, self.case.left, True), self._execute(1, self.case.right, True)


_YEAR = re.compile(r"^(1[89]\d\d|20\d\d|21\d\d)$")
_YEAR_MONTH = re.compile(r"^(1[89]\d\d|20\d\d|21\d\d)-(0[1-9]|1[0-2])$")


def _month_end(year: int, month: int) -> dt.date:
    return (dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1))


_ROW_OPTIONS_CAP = 4096


class CappedGenerator(engine.Generator):
    """``engine.Generator`` whose exhaustive row options are sampled when there are too many to list.

    ``Generator._row_options`` lists the whole product of every read column's values; under a ``*`` every
    column is read, so a TPC-DS fact table (23 to 34 columns, 3 values each) never finishes. Past
    ``_ROW_OPTIONS_CAP`` rows a random sample of the product is used (the exhaustive phase then samples,
    as it already does when the multisets are too many). Below the cap the rows are exactly the engine's."""

    def _row_options(self, name, values, data):
        import math as _math

        case = self.case
        table = case.tables[name]
        key_columns = {k.lower() for key in table.keys for k in key}
        parent_values = {}
        for columns, parent, parent_columns in table.foreign_keys:
            if len(columns) == 1 and parent in data and parent != name:
                ptable = case.tables[parent]
                position = ptable.index(parent_columns[0])
                parent_values[table.index(columns[0])] = sorted({r[position] for r in data[parent] if r[position] is not None}, key=repr)
        choices = []
        for position, column in enumerate(table.columns):
            used = self.star or column.name.lower() in self.columns
            if position in parent_values:
                options = list(parent_values[position])
            elif column.values:
                options = list(column.values)[:3]
            elif used:
                options = [v for v in values.get(column.kind, []) if engine.fits(column, v)] or [engine._default(column.kind)]
            else:
                options = ["__fill__"] if column.name.lower() in key_columns else [engine._default(column.kind)]
            if not column.not_null and (used or position in parent_values):
                options = [None] + options
            choices.append(options)
        total = _math.prod(len(c) for c in choices)
        if total <= _ROW_OPTIONS_CAP:
            return super()._row_options(name, values, data)
        if total == 0:
            return []
        seen = set()
        for _ in range(_ROW_OPTIONS_CAP * 2):
            seen.add(tuple(self.rng.choice(c) for c in choices))
            if len(seen) >= _ROW_OPTIONS_CAP:
                break
        return sorted(seen, key=repr)


_ORIGINALS: dict = {}


def install() -> None:
    """Install the extensions in this process (idempotent; ``uninstall`` undoes it)."""

    if _ORIGINALS:
        return
    _ORIGINALS.update(Runner=engine.Runner, literal_pool=engine.literal_pool, Generator=engine.Generator)
    original_runner, original_pool = engine.Runner, engine.literal_pool
    engine.Generator = CappedGenerator

    def dispatch(case, query_seconds: float = 10.0):
        if case.meta.get("engines") or case.meta.get("results"):
            return HybridRunner(case, query_seconds, original_runner)
        return original_runner(case, query_seconds)

    def pool(*queries, dialect: str = "duckdb"):
        found = original_pool(*queries, dialect=dialect)
        for text in list(found.strings):
            if _YEAR.match(text):
                year = int(text)
                found.dates.update({dt.date(year, 1, 1), dt.date(year, 12, 31)})
            elif _YEAR_MONTH.match(text):
                year, month = int(text[:4]), int(text[5:])
                found.dates.update({dt.date(year, month, 1), _month_end(year, month)})
        for value in list(found.ints):
            if 1900 <= value <= 2100:
                found.dates.add(dt.date(value, 1, 1))
        return found

    engine.Runner = dispatch
    engine.literal_pool = pool


def uninstall() -> None:
    """Put the engine's own ``Runner``, ``literal_pool`` and ``Generator`` back."""

    for name, original in _ORIGINALS.items():
        setattr(engine, name, original)
    _ORIGINALS.clear()


class Adapter:
    """``items()`` lists every pair (picklable); ``case(item)`` proves it as the eval does and returns a Case, or None."""

    name = ""

    def items(self) -> list[dict]:
        raise NotImplementedError

    def case(self, item: dict) -> Case | None:
        raise NotImplementedError


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _quiet() -> None:
    os.environ.setdefault("KUMOSQL_TIMING", "0")
    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)


# --- DLBench -------------------------------------------------------------------------------------

_DATE_FUNCTIONS = (exp.TimeToStr, exp.Date, exp.Extract, exp.DateTrunc, exp.TimestampTrunc, exp.StrToTime, exp.TsOrDsToDate,
                   exp.DateAdd, exp.DateSub, exp.DateDiff, exp.UnixToTime, exp.TimeToUnix, exp.Year, exp.Month, exp.Day)


def _date_columns(*trees) -> set[str]:
    found = set()
    for tree in trees:
        if tree is None:
            continue
        for node in tree.find_all(*_DATE_FUNCTIONS):
            for column in node.find_all(exp.Column):
                found.add(column.name.lower())
    return found


def _dl_kind(declared: str, dated: bool) -> str:
    """The engine kind of a DLBench column: its declared type, or a date when a date function reads it."""

    import dlbench_bench as dl

    lowered = declared.lower()
    if "datetime" in lowered or "timestamp" in lowered:
        return "timestamp"
    if lowered.startswith("date") or (dated and (dl._texty(lowered) or not lowered)):
        return "date"
    if dl._texty(lowered):
        return "text"
    if any(w in lowered for w in ("real", "float", "double", "dec", "num")):
        return "float"
    return "int"


class DLBench(Adapter):
    """Pairs ``dlbench_bench.decide`` counts as proven (same parse, dialect-gap and ``prove`` calls).

    ``target=False`` (``dlbench``): what the eval runs on a BIRDTrans proof, its first check. Both queries as
    the prover read them (the translation written in the source's dialect, ``dl.as_source``), in the source's
    engine: SQLite itself for a SQLite source (the eval's ``check_proof``, with its declared column types), DuckDB
    after sqlglot translates from MySQL or PostgreSQL otherwise (the eval runs no check there).

    ``target=True`` (``dlbench-target``): the source against the translation as its own dialect reads it, in
    DuckDB. For a DuckDB target this is the eval's second check (the translation "natively in DuckDB", written
    as ``plain_target.sql("duckdb")``); for the other five databases it asks whether the proof also holds under
    that database's reading, which the eval does not run (sqlglot's translation to DuckDB stands in for it).

    A column a date function reads is a date (SQLite: ISO text); the eval's own check fills every date column
    with random strings. A column typed DATE or TIMESTAMP only where DuckDB reads the query with a date function.
    """

    def __init__(self, target: bool):
        self.target = target
        self.name = "dlbench-target" if target else "dlbench"

    def items(self) -> list[dict]:
        import dlbench_bench as dl

        return [{"pair": p.id, **p.__dict__} for p in dl.load_pairs()]

    def case(self, item: dict) -> Case | None:
        import dlbench_bench as dl

        _quiet()
        install()
        pair = dl.Pair(**{k: v for k, v in item.items() if k != "pair"})
        # ``decide``: both sides parse, as_source writes both, no dialect gap, ``prove``
        source_tree, target_tree = dl.parse(pair.source_query, pair.source_dbms), dl.parse(pair.target_query, pair.target_dbms)
        if source_tree is None or target_tree is None:
            return None
        left, right = dl.as_source(pair)
        schema = dl.schema_of(pair)
        if left is None or right is None or dl.dialect_gap(pair, left, right, schema) is not None:
            return None
        source_dialect = dl.SQLGLOT[pair.source_dbms]
        target_dialect = dl.SQLGLOT[pair.target_dbms]
        if not dl.prove(left, right, schema, source_dialect):
            return None
        plain_target = dl._plain(dl.parse(pair.target_query, pair.target_dbms), pair.renames)
        plain_source = dl._plain(dl.parse(pair.source_query, pair.source_dbms), {})
        sqlite_source = pair.source_dbms == "sqlite"
        # a date column is DATE in DuckDB where a query DuckDB runs applies a date function to it
        duck_trees = []
        if self.target:
            duck_trees.append(plain_target)
        if not sqlite_source:
            duck_trees += [plain_source, dl.parse(right, pair.source_dbms)]
        target_dated = _date_columns(*duck_trees)
        dated = _date_columns(plain_source, plain_target, dl.parse(right, pair.source_dbms))
        tables, sqlite_tables = {}, {}
        for table, columns in schema.items():
            if not columns:  # a table the source describes with no columns cannot be created (and is never read)
                continue
            cols = []
            for column, declared in columns.items():
                kind = _dl_kind(declared, column in dated)
                if kind in ("date", "timestamp"):
                    sql_type = ("DATE" if kind == "date" else "TIMESTAMP") if column in target_dated else "VARCHAR"
                else:
                    sql_type = {"int": "BIGINT", "float": "DOUBLE", "text": "VARCHAR"}[kind]
                cols.append(Column(column, kind, sql_type=sql_type))
            tables[table] = Table(table, cols)
            sqlite_tables[table] = [(c, d or "TEXT") for c, d in columns.items()]
        if self.target:
            engines = ("sqlite" if sqlite_source else "duckdb", "duckdb")
            left_sql = left if sqlite_source else to_duckdb(left, source_dialect)
            if pair.target_dbms == "duckdb":
                right_sql = plain_target.sql(dialect="duckdb")  # as the harness's native check runs it
            else:
                right_sql = tree_to_duckdb(plain_target, target_dialect)
        else:
            engines = ("sqlite", "sqlite") if sqlite_source else ("duckdb", "duckdb")
            if sqlite_source:
                left_sql, right_sql = left, right
            else:
                left_sql, right_sql = to_duckdb(left, source_dialect), to_duckdb(right, source_dialect)
        ordered = dl._ordered(left, source_dialect)
        return Case(
            self.name, pair.id, left_sql, right_sql, tables, setup=MACROS, held_out=pair.held_out,
            source=(left, right), dialect=source_dialect, mode="list" if ordered else "bag",
            meta={"engines": engines, "results": "text-dates", "sqlite_tables": sqlite_tables, "label": pair.label,
                  "source_dbms": pair.source_dbms, "target_dbms": pair.target_dbms, "target_query": pair.target_query,
                  "source_query": pair.source_query},
        )


# --- LLM-SQL-Solver ------------------------------------------------------------------------------


_LSS_CASES: list = []


def _lss_cases() -> list:
    if not _LSS_CASES:
        import llm_sql_solver_bench as L

        _LSS_CASES.extend(L.load_cases())
    return _LSS_CASES


class LlmSqlSolver(Adapter):
    """The pairs ``llm_sql_solver_bench.decide`` counts as proven (the same adaptation, prover call and mixed-type rule).

    The eval runs no check on a proof (it only reads the label), so the Case runs the proved pair as the eval runs
    its refutation: both adapted queries in SQLite, with Spider's column types and, like the proof, no keys or
    foreign keys (a proof claims every database; the eval's listed keys are kept in ``meta``). ``llm-sql-solver-uncounted``
    holds the pairs the prover proves but the mixed-type rule leaves out of the count."""

    def __init__(self, suite: str, counted: bool = True):
        self.suite = suite
        self.counted = counted
        self.name = f"llm-sql-solver-{suite}" if counted else "llm-sql-solver-uncounted"

    def items(self) -> list[dict]:
        return [
            {"pair": c.id, "suite": c.suite, "index": c.index}
            for c in _lss_cases()
            if self.counted is False or c.suite == self.suite
        ]

    def case(self, item: dict) -> Case | None:
        import llm_sql_solver_bench as L

        _quiet()
        install()
        case = next(c for c in _lss_cases() if c.suite == item["suite"] and c.index == item["index"])
        sql1, sql2 = L.adapt(case.sql1, case.tables), L.adapt(case.sql2, case.tables)
        if not L.ordered(sql1):
            proved = L.prove(sql1, sql2, case.tables) == "proven"
        else:
            proved = L.ordered(sql2) and L.prove(L.for_prover(sql1), L.for_prover(sql2), case.tables) == "proven"
        mixed = L.mixed_type_comparison(sql1, case.tables) or L.mixed_type_comparison(sql2, case.tables)
        if not proved or mixed == self.counted:
            return None
        tables = {
            t: Table(t, [Column(c, "int" if k == "INTEGER" else "text", sql_type="BIGINT" if k == "INTEGER" else "VARCHAR") for c, k in cols.items()])
            for t, cols in case.tables.items()
        }
        return Case(
            self.name, item["pair"], sql1, sql2, tables, held_out=case.held_out, source=(sql1, sql2), dialect="sqlite",
            mode="list" if L.ordered(sql1) else "bag",
            meta={"engines": ("sqlite", "sqlite"), "sqlite_tables": {t: list(cols.items()) for t, cols in case.tables.items()},
                  "label": case.label, "label_error": L.same_query(sql1, sql2), "mixed_types": mixed,
                  "listed_keys": {t: list(k) for t, k in case.keys.items()}, "foreign_keys": list(case.foreign)},
        )


# --- PostgreSQL / MySQL catalogs -----------------------------------------------------------------

_PG_INFO = {"integer": "integer", "int": "integer", "int4": "integer", "bigint": "bigint", "int8": "bigint", "smallint": "smallint",
            "int2": "smallint", "char": "character", "character": "character", "bpchar": "character", "varchar": "character varying",
            "character varying": "character varying", "text": "text", "decimal": "numeric", "numeric": "numeric", "date": "date",
            "time": "time without time zone", "timestamp": "timestamp without time zone", "double": "double precision",
            "double precision": "double precision", "real": "real", "float": "double precision", "boolean": "boolean", "bool": "boolean"}


def engine_column(name: str, type_text: str, not_null: bool = False, precision=None, scale=None) -> Column:
    """An engine column for a PostgreSQL or MySQL type name (or an information_schema data_type)."""

    lowered = (type_text or "").lower().strip()
    numbers = re.findall(r"\d+", lowered)
    base = re.sub(r"\(.*", "", lowered).strip()
    if "[]" in lowered or base.endswith("array"):
        return Column(name, "text", not_null, "VARCHAR")
    unsigned = {"ubigint": "UBIGINT", "uint": "UINTEGER", "uinteger": "UINTEGER", "umediumint": "UINTEGER", "usmallint": "USMALLINT",
                "utinyint": "UTINYINT"}  # sqlglot writes MySQL's ``int unsigned`` as UINT
    if base in unsigned:
        return Column(name, "int", not_null, unsigned[base])
    if base in ("tinyint",) and numbers[:1] == ["1"]:
        return Column(name, "int", not_null, "TINYINT")
    if base in ("integer", "int", "int4", "mediumint", "serial") or base.startswith("int("):
        return Column(name, "int", not_null, "INTEGER")
    if base in ("bigint", "int8", "bigserial") or base.startswith("bigint"):
        return Column(name, "int", not_null, "BIGINT")
    if base in ("smallint", "int2", "tinyint", "smallserial"):
        return Column(name, "int", not_null, "SMALLINT" if base != "tinyint" else "TINYINT")
    if base in ("numeric", "decimal"):
        p = precision if precision else (int(numbers[0]) if numbers else 18)
        s = scale if scale is not None else (int(numbers[1]) if len(numbers) > 1 else 6)
        p = min(int(p), 38)
        return Column(name, "decimal", not_null, f"DECIMAL({p},{min(int(s), p)})")
    if base in ("real", "float", "float4", "float8", "double", "double precision"):
        return Column(name, "float", not_null, "DOUBLE")
    if base in ("boolean", "bool"):
        return Column(name, "bool", not_null, "BOOLEAN")
    if base == "date":
        return Column(name, "date", not_null, "DATE")
    if base.startswith("timestamp") or base == "datetime":
        return Column(name, "timestamp", not_null, "TIMESTAMP")
    if base.startswith("time"):
        return Column(name, "time", not_null, "TIME")
    return Column(name, "text", not_null, "VARCHAR")


def ddl_tables(text: str, dialect: str = "postgres") -> dict[str, dict]:
    """``{table: {"columns": [(name, type, not null)], "pk": [...]}}`` from CREATE TABLE statements."""

    out: dict[str, dict] = {}
    for statement in sqlglot.parse(text, read=dialect, error_level=sqlglot.ErrorLevel.IGNORE):
        if not isinstance(statement, exp.Create) or not isinstance(statement.this, exp.Schema):
            continue
        name = statement.this.this.name.lower()
        columns, pk = [], []
        for item in statement.this.expressions:
            if isinstance(item, exp.ColumnDef):
                kinds = [c.args.get("kind") for c in item.args.get("constraints") or []]
                not_null = any(isinstance(k, (exp.NotNullColumnConstraint, exp.PrimaryKeyColumnConstraint)) and not k.args.get("allow_null") for k in kinds)
                if any(isinstance(k, exp.PrimaryKeyColumnConstraint) for k in kinds):
                    pk.append(item.name.lower())
                kind = item.args.get("kind")
                columns.append((item.name.lower(), kind.sql(dialect="postgres") if kind is not None else "text", not_null))
            elif isinstance(item, exp.PrimaryKey):
                pk = [e.name.lower() for e in item.expressions]
        out[name] = {"columns": columns, "pk": pk}
    return out


def _read_tables(*sqls: str, dialect: str) -> set[str]:
    found = set()
    for sql in sqls:
        tree = sqlglot.parse_one(sql, read=dialect)
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        found |= {t.name.lower() for t in tree.find_all(exp.Table) if t.name and t.name.lower() not in ctes}
    return found


def catalog_tables(catalog, columns_of, names: set[str]) -> dict[str, Table]:
    """Engine tables for ``names`` from a ``query_optimizer.Catalog`` and a per-table column builder."""

    out = {}
    for name in sorted(names):
        if name not in catalog.columns:
            continue
        not_null = catalog.not_null.get(name, set())
        cols = [replace(columns_of(name, c), not_null=c in not_null) for c in catalog.columns[name]]
        keys = [tuple(k) for k in catalog.keys.get(name, []) if k]
        out[name] = Table(name, cols, keys)
    return out


def _optimizer_case(eval_name: str, pair: str, original: str, rewritten: str, dialect: str, catalog, columns_of, *,
                    held_out: bool = False, setup: tuple = (), caseless: bool = False, meta: dict | None = None) -> Case | None:
    from kumosql import query_optimizer as qo

    try:
        left, right = to_duckdb(original, dialect, caseless), to_duckdb(rewritten, dialect, caseless)
    except sqlglot.errors.SqlglotError:
        return None
    tables = catalog_tables(catalog, columns_of, _read_tables(original, rewritten, dialect=dialect))
    ordered = qo.has_top_level_order(original, dialect)
    return Case(eval_name, pair, left, right, tables, setup=MACROS + tuple(setup), held_out=held_out, source=(original, rewritten),
                dialect=dialect, mode="list" if ordered else "bag", meta={"engines": ("duckdb", "duckdb"), **(meta or {})})


# --- SQL-RewriteBench ----------------------------------------------------------------------------


def _env_path(name: str, default: str = "") -> Path | None:
    value = os.environ.get(name, default)
    return Path(value) if value and Path(value).exists() else None


class RewriteBench(Adapter):
    name = "sql-rewritebench"

    def items(self) -> list[dict]:
        bench = _env_path("KUMOSQL_RECHECK_REWRITEBENCH")
        if bench is None:
            _warn("sql-rewritebench: set KUMOSQL_RECHECK_REWRITEBENCH to a SQL-RewriteBench checkout")
            return []
        import rewrite_bench as rb

        return [{"pair": c["name"], "bench": str(bench)} for c in rb.load_cases(bench)]

    def _ddl(self, database: str) -> dict[str, dict]:
        if database == "dsb":
            base = _env_path("KUMOSQL_BENCH_DIR")
            path = base / "dsb" / "code" / "tools" / "tpcds.sql" if base else None
        else:
            kit = _env_path("KUMOSQL_RECHECK_TPCDS_KIT")
            path = kit / "tools" / "tpcds.sql" if kit else None
        if path is None or not path.exists():
            raise RuntimeError(f"no TPC-DS DDL for {database}")
        return ddl_tables(path.read_text(encoding="utf-8"))

    def catalog(self, case: dict):
        """The case's schema profile completed from the kit's DDL, as ``rewrite_bench._catalog`` completes it
        from the database's ``information_schema`` (the database was loaded with the kit's CREATE TABLE script)."""

        from kumosql import query_optimizer as qo

        tables = list((case["profile"] or {}).get("tables") or [])
        known = {str(t["name"]).lower() for t in tables}
        for name, info in self._ddl(case["database"]).items():
            if name in known:
                continue
            columns = []
            for position, (column, type_sql, not_null) in enumerate(info["columns"], 1):
                base = re.sub(r"\(.*", "", type_sql.lower()).strip()
                numbers = re.findall(r"\d+", type_sql)
                columns.append({
                    "column_name": column, "data_type": _PG_INFO.get(base, base),
                    "numeric_precision": int(numbers[0]) if base in ("decimal", "numeric") and numbers else None,
                    "numeric_scale": int(numbers[1]) if base in ("decimal", "numeric") and len(numbers) > 1 else None,
                    "is_nullable": "NO" if (not_null or column in info["pk"]) else "YES", "ordinal_position": position,
                })
            tables.append({"name": name, "columns": columns, "primary_key": info["pk"]})
        detail = {}
        for table in tables:
            for c in table.get("columns") or []:
                detail[(str(table["name"]).lower(), str(c["column_name"]).lower())] = c
        return qo.Catalog.from_schema_profile({"tables": tables}), detail

    def case(self, item: dict) -> Case | None:
        import rewrite_bench as rb
        from kumosql import query_optimizer as qo

        _quiet()
        install()
        case = rb.load_cases(Path(item["bench"]), [item["pair"]])[0]
        catalog, detail = self.catalog(case)
        try:
            outcome = qo.optimize(case["sql"], catalog, dialect="postgres", cost=None)
        except Exception:  # a crash is a failure to rewrite, never a rewrite
            return None
        if outcome.sql is None or rb._same_text(outcome.sql, case["sql"]):
            return None

        def columns_of(table, column):
            c = detail.get((table, column), {})
            return engine_column(column, c.get("data_type", "text"), precision=c.get("numeric_precision"), scale=c.get("numeric_scale"))

        return _optimizer_case(self.name, item["pair"], case["sql"].strip().rstrip(";"), outcome.sql, "postgres", catalog, columns_of,
                               held_out=rb.is_held_out(item["pair"]), meta={"steps": list(outcome.steps), "cost_guard": False})


# --- WeTune --------------------------------------------------------------------------------------


class WeTune(Adapter):
    def __init__(self, case_insensitive: bool = False):
        self.case_insensitive = case_insensitive
        self.name = "wetune-issues-mysql-ci" if case_insensitive else "wetune-issues"

    def _root(self) -> Path | None:
        return _env_path("KUMOSQL_RECHECK_WETUNE")

    def items(self) -> list[dict]:
        root = self._root()
        if root is None:
            _warn("wetune-issues: set KUMOSQL_RECHECK_WETUNE to a WeTune-code checkout")
            return []
        import wetune_bench as wb

        out = []
        for issue in wb.load_issues(root):
            for kind in ("developer", "kumosql"):
                out.append({"pair": f"{issue['id']}:{kind}", "id": issue["id"], "kind": kind})
        return out

    def case(self, item: dict) -> Case | None:
        import wetune_bench as wb
        from kumosql import query_optimizer as qo

        _quiet()
        install()
        root = self._root()
        issue = next(i for i in wb.load_issues(root) if i["id"] == item["id"])
        path = root.joinpath(*wb.SCHEMAS, f"{issue['app']}.base.schema.sql")
        catalog, dialect = wb.load_catalog(path)
        if self.case_insensitive and dialect != "mysql":
            return None
        if item["kind"] == "developer":
            try:
                verdict = qo.prove(issue["original"], issue["developer"], catalog, dialect=dialect)
            except Exception:
                return None
            if not verdict.proven:
                return None
            rewritten = issue["developer"]
        else:
            try:
                outcome = qo.optimize(issue["original"], catalog, dialect=dialect, deletion_budget_s=20.0)
            except Exception:
                return None
            if outcome.sql is None:
                return None
            rewritten = outcome.sql
        types = {}
        for name, info in ddl_tables(path.read_text(encoding="utf-8", errors="replace"), dialect).items():
            types[name] = {c: t for c, t, _ in info["columns"]}

        def columns_of(table, column):
            return engine_column(column, types.get(table, {}).get(column, "text"))

        setup = ("SET default_collation = 'nocase'",) if self.case_insensitive else ()
        return _optimizer_case(self.name, item["pair"], issue["original"], rewritten, dialect, catalog, columns_of, setup=setup,
                               caseless=self.case_insensitive, meta={"app": issue["app"], "issue_kind": issue["kind"], "rewrite": item["kind"]})


# --- ClickBench ----------------------------------------------------------------------------------


class ClickBench(Adapter):
    name = "clickbench-rewrites"

    def _root(self) -> Path | None:
        return _env_path("KUMOSQL_RECHECK_CLICKBENCH")

    def items(self) -> list[dict]:
        root = self._root()
        if root is None:
            _warn("clickbench-rewrites: set KUMOSQL_RECHECK_CLICKBENCH to a ClickBench checkout")
            return []
        queries = [q.strip() for q in (root / "postgresql" / "queries.sql").read_text().splitlines() if q.strip()]
        return [{"pair": f"Q{n}", "number": n} for n in range(1, len(queries) + 1)]

    def case(self, item: dict) -> Case | None:
        import clickbench_bench as cb
        from kumosql import query_optimizer as qo

        _quiet()
        install()
        root = self._root()
        create_sql = (root / "postgresql" / "create.sql").read_text()
        queries = [q.strip() for q in (root / "postgresql" / "queries.sql").read_text().splitlines() if q.strip()]
        sql = queries[item["number"] - 1]
        columns, catalog = cb.read_table(create_sql)
        try:
            outcome = qo.optimize(sql, catalog, dialect="postgres")
        except Exception:  # a crash is a failure to rewrite, never a rewrite
            return None
        if outcome.sql is None:
            return None
        types = {n.lower(): t for n, t in columns}

        def columns_of(table, column):
            return engine_column(column, types.get(column, "text"))

        return _optimizer_case(self.name, item["pair"], sql.rstrip(";"), outcome.sql, "postgres", catalog, columns_of,
                               meta={"steps": list(outcome.steps)})


# --- BigQuery corpora ----------------------------------------------------------------------------

_BQ_COLUMN = {"INT64": ("int", "BIGINT"), "FLOAT64": ("float", "DOUBLE"), "BOOL": ("bool", "BOOLEAN"), "DATE": ("date", "DATE"),
              "TIMESTAMP": ("timestamp", "TIMESTAMP"), "STRING": ("text", "VARCHAR")}


def _bigquery_case(eval_name: str, pair: str, original: str, rewritten: str, tables: dict[str, Table], *, held_out: bool,
                   meta: dict, rename: dict[str, str] | None = None, substitute: dict | None = None) -> Case | None:
    """A Case running two BigQuery queries through ``bigquery_on_duckdb``; ``rename`` maps a table path to its engine name,
    ``substitute`` replaces script variables by their literal default."""

    sides = []
    faithful = True
    for sql in (original, rewritten):
        tree = sqlglot.parse_one(sql, read="bigquery")
        if rename:
            for table in list(tree.find_all(exp.Table)):
                path = ".".join(p for p in (table.catalog, table.db, table.name) if p).lower()
                if path in rename:
                    alias = table.args.get("alias")
                    new = exp.Table(this=exp.to_identifier(rename[path], quoted=True))
                    if alias is None:
                        new.set("alias", exp.TableAlias(this=exp.to_identifier(table.name, quoted=True)))
                    else:
                        new.set("alias", alias)
                    table.replace(new)
        if substitute:
            for column in list(tree.find_all(exp.Column)):
                if not column.table and column.name.lower() in substitute:
                    column.replace(substitute[column.name.lower()].copy())
        text, ok = bigquery_to_duckdb(tree)
        faithful = faithful and ok
        sides.append(text)
    return Case(eval_name, pair, sides[0], sides[1], tables, setup=bigquery_setup(), held_out=held_out, source=(original, rewritten),
                dialect="bigquery", meta={**meta, "results": "bigquery", "faithful": faithful})


_DDL_CACHE: dict[str, dict] = {}
_LLMR2_QUERIES: dict[tuple, dict] = {}


class LlmR2(Adapter):
    """Proven rewrites of ``llmr2_bench.run_query``: one pair per (query, transformation), counted proven when the
    rewrite changes the query and the prover verifies it. The eval runs each changed query on real TPC-H, TPC-DS or
    IMDB data in DuckDB through ``transformation_bench._duck`` (sqlglot's BigQuery to DuckDB), so the Case holds
    exactly that SQL, over tables typed as the data's DDL types them (TPC-H's and TPC-DS's own CREATE TABLE
    scripts; no keys, no NOT NULL: the proof assumes none)."""

    DATASETS = {"tpch": "tpch", "dsb": "tpcds"}  # JOB-syn is flat joins no rule changes, and its DDL is not fetched here

    def __init__(self, split: str):
        self.split = split
        self.name = "llm-r2-scale" if split == "test" else "llm-r2-scale-train"

    def items(self) -> list[dict]:
        if _env_path("KUMOSQL_BENCH_DIR") is None:
            _warn("llm-r2-scale: set KUMOSQL_BENCH_DIR to the folder benchmark_corpora fetches into")
            return []
        import llmr2_bench as lb

        out = []
        for dataset in self.DATASETS:
            for qid, _ in lb.queries(dataset, self.split):
                for name in lb.transformations():
                    out.append({"pair": f"{qid}#{name}", "id": qid, "dataset": dataset, "transformation": name})
        return out

    def _ddl_tables(self, workload: str) -> dict[str, dict]:
        import benchmark_corpora as corpora

        ddl = corpora.TPCH_DDL if workload == "tpch" else (corpora.BENCH_DIR / "dsb" / "code" / "tools" / "tpcds.sql").read_text()
        if ddl not in _DDL_CACHE:
            _DDL_CACHE[ddl] = ddl_tables(ddl)
        return _DDL_CACHE[ddl]

    def case(self, item: dict) -> Case | None:
        import benchmark_corpora as corpora
        import llmr2_bench as lb
        import transformation_bench as tb
        from kumosql import rewrite
        from kumosql.rewrite import VerificationStatus

        _quiet()
        install()
        key = (str(corpora.BENCH_DIR), item["dataset"], self.split)
        if key not in _LLMR2_QUERIES:
            _LLMR2_QUERIES[key] = dict(lb.queries(item["dataset"], self.split))
        text = _LLMR2_QUERIES[key][item["id"]]
        try:
            sql = corpora.to_bigquery(text)
        except Exception:
            return None
        result = rewrite.apply_rules(lb.transformations()[item["transformation"]], sql)
        if result.sql == sql or result.verification.status != VerificationStatus.PROVEN:
            return None
        workload = self.DATASETS[item["dataset"]]
        raw = self._ddl_tables(workload)
        names = _read_tables(sql, result.sql, dialect="bigquery")
        tables = {
            name: Table(name, [engine_column(c, t) for c, t, _ in info["columns"]])
            for name, info in raw.items() if name in names
        }
        return Case(self.name, item["pair"], tb._duck(sql), tb._duck(result.sql), tables, held_out=self.split == "test",
                    source=(sql, result.sql), dialect="bigquery",
                    meta={"dataset": item["dataset"], "transformation": item["transformation"]})


# --- Spider 2.0: schema inference for BigQuery queries over public tables ------------------------


def _script(sql: str) -> tuple[exp.Expression | None, dict]:
    """The final query of a BigQuery script and its DECLAREd defaults (name -> literal)."""

    statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    defaults = {}
    for statement in statements[:-1]:
        if isinstance(statement, exp.Declare):
            for item in statement.expressions:
                default = item.args.get("default")
                for variable in (item.this if isinstance(item.this, list) else [item.this]):
                    if default is not None and variable is not None:
                        defaults[variable.name.lower()] = default
        elif not isinstance(statement, exp.Semicolon):
            return None, {}
    return (statements[-1] if statements else None), defaults


_KIND_RANK = {"int": 0, "float": 1, "date": 2, "timestamp": 2, "bool": 2, "text": 3}


def infer_bigquery_tables(*trees: exp.Expression) -> tuple[dict[str, dict[str, str]], str | None]:
    """``{table path: {column: kind}}`` for the base tables the queries read, or a reason it cannot be inferred
    (nested fields, UNNEST, a column that resolves to no table)."""

    from sqlglot.optimizer.scope import traverse_scope

    tables: dict[str, dict[str, str]] = {}

    def path(table: exp.Table) -> str:
        return ".".join(p for p in (table.catalog, table.db, table.name) if p).lower()

    for tree in trees:
        if tree.find(exp.Unnest) or tree.find(exp.Dot) or tree.find(exp.Bracket):
            return {}, "nested data (UNNEST, struct fields or arrays)"
        for scope in traverse_scope(tree):
            bases = {alias: source for alias, source in scope.sources.items() if isinstance(source, exp.Table)}
            for source in bases.values():
                tables.setdefault(path(source), {})
            derived_names = set()
            for alias, source in scope.sources.items():
                if not isinstance(source, exp.Table):
                    try:
                        derived_names |= {n.lower() for n in source.expression.named_selects}
                    except AttributeError:
                        pass
            projections = {s.alias_or_name.lower() for s in getattr(scope.expression, "selects", [])}
            for column in scope.columns:
                name = column.name.lower()
                if not name or isinstance(column.this, exp.Star):
                    continue
                if column.table:
                    source = scope.sources.get(column.table)
                    if source is None:
                        return {}, f"unresolved qualifier {column.table}.{column.name}"
                    if isinstance(source, exp.Table):
                        tables[path(source)].setdefault(name, _guess_kind(column))
                        tables[path(source)][name] = _merge_kind(tables[path(source)][name], _guess_kind(column))
                    continue
                if name in derived_names or name == "_table_suffix":
                    if name == "_table_suffix" and bases:
                        tables[path(next(iter(bases.values())))]["_table_suffix"] = "text"
                    continue
                if not bases:
                    if name in projections:
                        continue
                    continue
                owner = path(next(iter(bases.values())))
                kind = _guess_kind(column)
                tables[owner][name] = _merge_kind(tables[owner].get(name, kind), kind)
    return tables, None


def _merge_kind(a: str, b: str) -> str:
    return a if _KIND_RANK.get(a, 0) >= _KIND_RANK.get(b, 0) else b


def _guess_kind(column: exp.Column) -> str:
    parent = column.parent
    while isinstance(parent, exp.Paren):
        parent = parent.parent
    if isinstance(parent, (exp.Like, exp.ILike, exp.RegexpLike, exp.Lower, exp.Upper, exp.Concat, exp.Substring, exp.Length,
                           exp.StrPosition, exp.Trim, exp.RegexpExtract, exp.StartsWith)):
        return "text"
    if isinstance(parent, (exp.Date, exp.Extract, exp.DateTrunc, exp.DateDiff, exp.DateAdd, exp.DateSub, exp.TimestampTrunc,
                           exp.TimeToStr, exp.Year, exp.Month, exp.Day)):
        return "date"
    if isinstance(parent, (exp.StrToDate, exp.StrToTime)):
        return "text"
    if isinstance(parent, (exp.Avg, exp.Stddev, exp.StddevSamp, exp.StddevPop, exp.Variance, exp.Sqrt, exp.Ln, exp.Round)):
        return "float"
    if isinstance(parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In)):
        for other in parent.find_all(exp.Literal):
            if other.is_string:
                return "text"
            return "float" if "." in other.name else "int"
    if isinstance(parent, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Sum)):
        return "float"
    if isinstance(parent, (exp.Is,)) and isinstance(parent.expression, exp.Boolean):
        return "bool"
    return "int"


class Spider2(Adapter):
    name = "spider2-bigquery"

    def items(self) -> list[dict]:
        import spider2_bench as sb

        rules = list(_cleanup_rules()) + ["format_sql"]
        return [{"pair": f"{c['id']}#{rule}", "id": c["id"], "rule": rule} for c in sb.cases() for rule in rules]

    def case(self, item: dict) -> Case | None:
        import spider2_bench as sb
        from kumosql import rewrite

        _quiet()
        install()
        sql = sb.text_of(item["id"])
        try:
            result = rewrite.apply_rule(item["rule"], sql)
        except Exception:
            return None
        outcome = sb._rewrite_outcome(result, sql)
        if not outcome.get("changed") or not outcome.get("verified"):
            return None
        held_out = sb.split_of(item["id"]) == "held-out"
        meta = {"engines": ("duckdb", "duckdb"), "rule": item["rule"],
                "proof": "rule" if result.verification.status == rewrite.VerificationStatus.PROVEN else "smt fallback"}
        left_tree, left_defaults = _script(sql)
        right_tree, right_defaults = _script(result.sql)
        if left_tree is None or right_tree is None:
            meta["uninferable"] = "script with statements other than DECLARE"
            return _unrunnable(self.name, item["pair"], sql, result.sql, held_out, meta)
        tables, why = infer_bigquery_tables(left_tree, right_tree)
        if why is not None:
            meta["uninferable"] = why
            return _unrunnable(self.name, item["pair"], sql, result.sql, held_out, meta)
        rename, engine_tables = {}, {}
        used = Counter(p.split(".")[-1] for p in tables)
        for table_path, columns in sorted(tables.items()):
            short = table_path.split(".")[-1]
            local = short if used[short] == 1 else table_path.replace(".", "__")
            local = re.sub(r"[^a-z0-9_]", "_", local)
            rename[table_path] = local
            cols = [Column(c, kind, sql_type={"int": "BIGINT", "float": "DOUBLE", "text": "VARCHAR", "date": "DATE",
                                              "timestamp": "TIMESTAMP", "bool": "BOOLEAN"}[kind]) for c, kind in sorted(columns.items())]
            engine_tables[local] = Table(local, cols or [Column("id", "int", sql_type="BIGINT")])
        defaults = {**left_defaults, **right_defaults}
        case = _bigquery_case(self.name, item["pair"], left_tree.sql(dialect="bigquery"), right_tree.sql(dialect="bigquery"), engine_tables,
                              held_out=held_out, meta=meta, rename=rename, substitute=defaults)
        case.source = (sql, result.sql)
        return case


def _unrunnable(eval_name, pair, left, right, held_out, meta) -> Case:
    """A case the engine reports as unrunnable (its schema cannot be created), keeping the reason in ``meta``."""

    return Case(eval_name, pair, "SELECT 1", "SELECT 1", {"x": Table("x", [Column("a", "int", sql_type="NO SUCH TYPE")])},
                held_out=held_out, source=(left, right), dialect="bigquery", meta=meta)


def _cleanup_rules() -> tuple[str, ...]:
    import spider2_bench as sb

    return tuple(sb.cov.CLEANUP_RULES)


ADAPTERS = {
    a.name: a
    for a in [
        DLBench(False), DLBench(True),
        LlmSqlSolver("relaxed"), LlmSqlSolver("negatives"), LlmSqlSolver("all", counted=False),
        RewriteBench(), WeTune(False), WeTune(True), ClickBench(),
        LlmR2("test"), LlmR2("train"), Spider2(),
    ]
}
