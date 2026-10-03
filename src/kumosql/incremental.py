"""Incremental-versus-full-refresh correctness for Dataform incremental tables.

A Dataform incremental table runs its query in full the first time and later
runs only the ``when(incremental(), ...)`` form, appending (or merging on
``uniqueKey``) into the existing table. It is *correct* when, after every
batch of source changes, the table equals what a full refresh would produce.

This module models that run cycle in DuckDB and offers three things:

* :func:`parse_incremental_sqlx` reads a SQLX incremental model into its full
  and incremental queries (``when(incremental(), ...)``, ``${self()}``,
  ``${ref()}``, ``uniqueKey``, incremental ``pre_operations``).
* :class:`Simulation` replays batches of source DML and compares the
  incrementally maintained table with a full recompute after each one.
* :func:`check_incremental` decides, without a script, whether a model stays
  correct under a *contract* (the kinds of source change allowed). It answers
  ``safe`` only through a proof rule, ``diverges`` only with a minimised
  counterexample that replays in the simulator, and ``unknown`` otherwise.

BigQuery behaviour is explicit, not inherited from DuckDB: ``MERGE`` fails when
several source rows match one target row, NULL keys never match, and
``CURRENT_TIMESTAMP`` is pinned to a per-run clock so audit columns can be
excluded by name instead of making every run differ.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import datetime as dt
import random
import re
import time
from typing import Any, Iterable

import sqlglot
from sqlglot import exp

from .sqlx import _find_interpolation_end, split_sqlx_sections


class IncrementalError(ValueError):
    """The model or workload is outside what the simulator can run."""


class MergeConflict(IncrementalError):
    """BigQuery MERGE would fail: a target row matches several source rows.

    This is modelled BigQuery behaviour, so it counts as the run failing; every
    other :class:`IncrementalError` means the simulator could not run the model.
    """


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IncrementalModel:
    """One incremental table: the SQL of its first/full run and of later runs."""

    target: str
    full_sql: str
    incremental_sql: str
    unique_key: tuple[str, ...] = ()
    # Statements from ``pre_operations`` that run only on incremental runs.
    pre_operations: tuple[str, ...] = ()
    # Output columns allowed to differ (audit columns such as a load timestamp).
    ignore_columns: tuple[str, ...] = ()
    # sqlglot dialect the query and pre_operations are written in.
    dialect: str = "bigquery"


@dataclass(frozen=True)
class SourceTable:
    """A table the model reads. ``time_column`` is its event-time column, if any."""

    columns: dict[str, str]
    key: tuple[str, ...] = ()
    time_column: str | None = None
    # DuckDB default expressions for columns the source DML does not set.
    defaults: dict[str, str] = field(default_factory=dict)


_STRING = r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`(?:[^`\\]|\\.)*`'
_CONFIG_STR = re.compile(rf"\b{{key}}\s*:\s*(?:{_STRING})")


def _js_string(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] in "'\"`" and token[-1] == token[0]:
        return token[1:-1]
    return token


def _split_args(text: str) -> list[str]:
    """Split call arguments on top-level commas, respecting quotes and brackets."""

    args: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            current.append(c)
            if c == "\\" and i + 1 < len(text):
                i += 1
                current.append(text[i])
            elif c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
            current.append(c)
        elif c in "([{":
            depth += 1
            current.append(c)
        elif c in ")]}":
            depth -= 1
            current.append(c)
        elif c == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(c)
        i += 1
    if "".join(current).strip():
        args.append("".join(current).strip())
    return args


def _resolve(text: str, target: str, incremental: bool) -> str:
    """Evaluate the SQLX interpolations the incremental pattern uses."""

    out: list[str] = []
    cursor = 0
    while True:
        opening = text.find("${", cursor)
        if opening < 0:
            out.append(text[cursor:])
            break
        out.append(text[cursor:opening])
        closing = _find_interpolation_end(text, opening)
        out.append(_evaluate(text[opening + 2 : closing].strip(), target, incremental))
        cursor = closing + 1
    return "".join(out)


def _evaluate(expression: str, target: str, incremental: bool) -> str:
    if re.fullmatch(r"self\(\s*\)", expression):
        return target
    ref = re.fullmatch(r"ref\(\s*((?:%s)(?:\s*,\s*(?:%s))*)\s*\)" % (_STRING, _STRING), expression)
    if ref:
        return _js_string(_split_args(ref.group(1))[-1])
    ref_object = re.fullmatch(r"ref\(\s*\{.*\bname\s*:\s*(%s).*\}\s*\)" % _STRING, expression, re.DOTALL)
    if ref_object:
        return _js_string(ref_object.group(1))
    when = re.fullmatch(r"when\((.*)\)", expression, re.DOTALL)
    if when:
        args = _split_args(when.group(1))
        if len(args) not in (2, 3):
            raise IncrementalError("when() takes two or three arguments")
        condition = args[0].replace(" ", "")
        if condition == "incremental()":
            chosen = incremental
        elif condition == "!incremental()":
            chosen = not incremental
        else:
            raise IncrementalError(f"unsupported when() condition: {args[0]}")
        branch = args[1] if chosen else (args[2] if len(args) == 3 else '""')
        return _resolve(_js_string(branch), target, incremental)
    if expression in {"incremental()", "!incremental()"}:
        raise IncrementalError("a bare incremental() is only supported inside when()")
    raise IncrementalError(f"unsupported SQLX interpolation: ${{{expression}}}")


def _config_list(config: str, key: str) -> tuple[str, ...]:
    found = re.search(rf"\b{key}\s*:\s*(\[[^\]]*\]|{_STRING})", config)
    if not found:
        return ()
    value = found.group(1)
    if value.startswith("["):
        return tuple(_js_string(a) for a in _split_args(value[1:-1]))
    return (_js_string(value),)


def parse_incremental_sqlx(sqlx: str, target: str) -> IncrementalModel:
    """Read a SQLX incremental model. ``target`` is what ``${self()}`` resolves to."""

    sections = split_sqlx_sections(sqlx)
    config = next((t for kind, t in sections if kind == "block" and t.lstrip().startswith("config")), "")
    if config and not re.search(r"\btype\s*:\s*[\"']incremental[\"']", config):
        raise IncrementalError("config type is not incremental")
    body = "".join(t for kind, t in sections if kind == "sql").strip()
    pre_blocks = [t for kind, t in sections if kind == "block" and t.lstrip().startswith("pre_operations")]
    pre: list[str] = []
    for block in pre_blocks:
        inner = block[block.index("{") + 1 : block.rindex("}")]
        # ``${when(incremental(), `stmt`)}`` yields a statement only on incremental runs.
        rendered = _resolve(inner, target, True).strip()
        if _resolve(inner, target, False).strip() != rendered:
            pre.extend(s.strip() for s in rendered.split(";") if s.strip())
        elif rendered:
            raise IncrementalError("pre_operations that run on every run are not simulated")
    return IncrementalModel(
        target=target,
        full_sql=_resolve(body, target, False).strip().rstrip(";"),
        incremental_sql=_resolve(body, target, True).strip().rstrip(";"),
        unique_key=_config_list(config, "uniqueKey"),
        pre_operations=tuple(pre),
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

_DUCK_TYPES = {
    "INT64": "BIGINT",
    "INTEGER": "BIGINT",
    "FLOAT64": "DOUBLE",
    "NUMERIC": "DECIMAL(18,6)",
    "STRING": "VARCHAR",
    "BOOL": "BOOLEAN",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "DATETIME": "TIMESTAMP",
}

_EPOCH = dt.datetime(2024, 1, 1)


def _connect():
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise IncrementalError("duckdb is required; install kumosql[execution]") from exc
    return duckdb.connect(database=":memory:")


def _to_duckdb(sql: str, clock: dt.datetime, read: str = "bigquery") -> str:
    """Model SQL (BigQuery) or source DML (DuckDB/ANSI) to DuckDB, clock pinned."""

    try:
        tree = sqlglot.parse_one(sql, read=read)
    except sqlglot.errors.SqlglotError as exc:
        raise IncrementalError(f"cannot parse: {exc}") from exc
    literal = f"{clock:%Y-%m-%d %H:%M:%S}"

    def pin(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table) and read != "duckdb" and (node.args.get("db") or node.args.get("catalog")):
            # tables are local DuckDB tables named by their last part
            return exp.Table(this=node.this, alias=node.args.get("alias"))
        if isinstance(node, (exp.CurrentTimestamp, exp.CurrentDatetime)):
            return exp.cast(exp.Literal.string(literal), "timestamp")
        if isinstance(node, exp.CurrentDate):
            return exp.cast(exp.Literal.string(literal[:10]), "date")
        return node

    tree = tree.transform(pin)
    if read == "bigquery":
        from .bigquery_on_duckdb import faithful

        try:
            tree = faithful(tree)
        except sqlglot.errors.SqlglotError as exc:
            raise IncrementalError(f"cannot run as BigQuery does: {exc}") from exc
    return tree.sql(dialect="duckdb")


def _norm(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_norm(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _norm(v)) for k, v in value.items()))
    if isinstance(value, float):
        return round(value, 9)
    if hasattr(value, "normalize") and hasattr(value, "as_tuple"):
        return float(value)
    return value


def _sort_key(row: tuple) -> tuple:
    return tuple((v is None, type(v).__name__, repr(v)) for v in row)


@dataclass
class BatchResult:
    index: int
    status: str  # agree | diverge | error
    only_incremental: tuple[tuple, ...] = ()
    only_full: tuple[tuple, ...] = ()
    error: str = ""


class Simulation:
    """Replay source changes and compare incremental output with full recompute."""

    def __init__(
        self,
        model: IncrementalModel,
        sources: dict[str, SourceTable],
        initial: Iterable[str] = (),
    ):
        self.model = model
        self.sources = sources
        self.con = _connect()
        if model.dialect == "bigquery":
            from .bigquery_on_duckdb import configure

            configure(self.con)
        self.clock = dt.datetime(2030, 1, 1)
        self.runs = 0
        self.failed: str | None = None
        for name, table in sources.items():
            cols = ", ".join(
                f'"{c}" {_DUCK_TYPES.get(t.upper(), t)}' + (f" DEFAULT {table.defaults[c]}" if c in table.defaults else "")
                for c, t in table.columns.items()
            )
            if table.defaults:
                self.con.execute("CREATE SEQUENCE IF NOT EXISTS _load_seq")
            self.con.execute(f'CREATE TABLE "{name}" ({cols})')
        for statement in initial:
            self._dml(statement)

    # -- statements -------------------------------------------------------

    def _dml(self, statement: str) -> None:
        try:
            self.con.execute(_to_duckdb(statement, self.clock, "duckdb"))
        except Exception as exc:
            raise IncrementalError(f"source statement failed: {exc}") from exc

    def _full_query(self) -> str:
        return _to_duckdb(self.model.full_sql, self.clock, self.model.dialect)

    # -- the Dataform run cycle ------------------------------------------

    def run_incremental(self) -> None:
        """First run: full build. Later runs: the Dataform incremental statement."""

        m = self.model
        self.clock += dt.timedelta(hours=1)
        if self.runs == 0:
            self.con.execute(f'CREATE OR REPLACE TABLE "{m.target}" AS {self._full_query()}')
            self.runs += 1
            return
        self.runs += 1
        for statement in m.pre_operations:
            self.con.execute(_to_duckdb(statement, self.clock, m.dialect))
        self.con.execute(f"CREATE OR REPLACE TEMP TABLE __incr AS {_to_duckdb(m.incremental_sql, self.clock, m.dialect)}")
        columns = [r[0] for r in self.con.execute(f'SELECT column_name FROM information_schema.columns WHERE table_name = \'{m.target}\' ORDER BY ordinal_position').fetchall()]
        if m.unique_key:
            on = " AND ".join(f't."{k}" = s."{k}"' for k in m.unique_key)
            # BigQuery MERGE raises when one target row matches several source rows.
            dup = self.con.execute(
                f'SELECT count(*) FROM (SELECT t.rowid FROM "{m.target}" t JOIN __incr s ON {on} GROUP BY t.rowid HAVING count(*) > 1)'
            ).fetchone()[0]
            if dup:
                raise MergeConflict("MERGE: a target row matches more than one source row")
            self.con.execute(f'DELETE FROM "{m.target}" t USING __incr s WHERE {on}')
        collist = ", ".join(f'"{c}"' for c in columns)
        self.con.execute(f'INSERT INTO "{m.target}" ({collist}) SELECT {collist} FROM __incr')

    def full_rows(self) -> tuple[list[str], list[tuple]]:
        cur = self.con.execute(self._full_query())
        return [d[0] for d in cur.description], [tuple(map(_norm, r)) for r in cur.fetchall()]

    def target_rows(self) -> tuple[list[str], list[tuple]]:
        cur = self.con.execute(f'SELECT * FROM "{self.model.target}"')
        return [d[0] for d in cur.description], [tuple(map(_norm, r)) for r in cur.fetchall()]

    def compare(self, index: int) -> BatchResult:
        try:
            cols_f, full = self.full_rows()
            cols_t, got = self.target_rows()
        except Exception as exc:
            raise IncrementalError(f"cannot compare in DuckDB: {exc}") from exc
        keep = [i for i, c in enumerate(cols_f) if c.lower() not in {x.lower() for x in self.model.ignore_columns}]
        keep_t = [i for i, c in enumerate(cols_t) if c.lower() not in {x.lower() for x in self.model.ignore_columns}]
        if len(keep) != len(keep_t):
            return BatchResult(index, "diverge", error="column counts differ")
        full = [tuple(r[i] for i in keep) for r in full]
        got = [tuple(r[i] for i in keep_t) for r in got]
        a, b = Counter(got), Counter(full)
        extra, missing = a - b, b - a
        if extra or missing:
            return BatchResult(
                index,
                "diverge",
                tuple(sorted(extra.elements(), key=_sort_key))[:5],
                tuple(sorted(missing.elements(), key=_sort_key))[:5],
            )
        return BatchResult(index, "agree")

    # -- batches ----------------------------------------------------------

    def step(self, statements: Iterable[str], index: int) -> BatchResult:
        """Apply one batch of source DML, run the incremental table, compare."""

        for statement in statements:
            self._dml(statement)
        try:
            self.run_incremental()
        except MergeConflict as exc:
            return BatchResult(index, "error", error=str(exc))
        except IncrementalError:
            raise
        except Exception as exc:
            raise IncrementalError(f"cannot run the model in DuckDB: {exc}") from exc
        return self.compare(index)


def replay(
    model: IncrementalModel,
    sources: dict[str, SourceTable],
    initial: Iterable[str],
    batches: Iterable[Iterable[str]],
) -> list[BatchResult]:
    """Run every batch; the first diverging or failing batch ends the replay."""

    sim = Simulation(model, sources, initial)
    results = [sim.step((), 0)]
    if results[0].status != "agree":
        return results
    for i, batch in enumerate(batches, start=1):
        result = sim.step(batch, i)
        results.append(result)
        if result.status != "agree":
            break
    return results


def first_divergence(results: list[BatchResult]) -> BatchResult | None:
    return next((r for r in results if r.status != "agree"), None)


# ---------------------------------------------------------------------------
# Contracts and counterexample search
# ---------------------------------------------------------------------------

#: Kinds of source change a contract can allow.
CHANGE_KINDS = (
    "insert_new",  # new rows, event time after everything seen
    "insert_late",  # rows whose event time is before the newest seen
    "insert_boundary",  # rows whose event time equals the newest seen
    "duplicate",  # exact re-delivery of an existing row
    "update",  # change non-key columns of an existing row
    "update_touch",  # change a row and move its event time to the newest
    "delete",  # remove an existing row
    "null_key",  # a new row whose key column is NULL
    "empty",  # a run with no source change
)


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, dt.datetime):
        return f"TIMESTAMP '{value:%Y-%m-%d %H:%M:%S}'"
    if isinstance(value, dt.date):
        return f"DATE '{value:%Y-%m-%d}'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return repr(value)


def _random_value(rng: random.Random, sql_type: str) -> Any:
    t = sql_type.upper()
    if t in ("INT64", "INTEGER"):
        return rng.randint(0, 3)
    if t in ("FLOAT64", "NUMERIC"):
        return float(rng.randint(0, 3))
    if t == "STRING":
        return rng.choice("abc")
    if t == "BOOL":
        return rng.random() < 0.5
    if t == "DATE":
        return (_EPOCH + dt.timedelta(days=rng.randint(0, 3))).date()
    return _EPOCH + dt.timedelta(hours=rng.randint(0, 3))


def _where(table: SourceTable, row: dict[str, Any]) -> str:
    return " AND ".join(
        f'"{c}" IS NULL' if v is None else f'"{c}" = {_literal(v)}' for c, v in row.items()
    )


def _insert(name: str, table: SourceTable, row: dict[str, Any]) -> str:
    cols = ", ".join(f'"{c}"' for c in row)
    return f'INSERT INTO "{name}" ({cols}) VALUES ({", ".join(_literal(v) for v in row.values())})'


class _Generator:
    """Random batches under a contract, tracking the source tables it writes."""

    def __init__(self, sources: dict[str, SourceTable], kinds: frozenset[str], rng: random.Random, tables: tuple[str, ...] | None = None):
        self.sources = sources
        self.tables = tuple(sorted(tables if tables else sources))
        self.kinds = kinds
        self.rng = rng
        self.rows: dict[str, list[dict[str, Any]]] = {n: [] for n in sources}
        self.next_key = {name: 0 for name in sources}
        self.next_hour = 0

    def _fresh(self, name: str, hour: int | None) -> dict[str, Any]:
        table = self.sources[name]
        row: dict[str, Any] = {}
        for column, sql_type in table.columns.items():
            if column in table.key:
                row[column] = self.next_key[name]
                self.next_key[name] += 1
            elif column == table.time_column:
                row[column] = _EPOCH + dt.timedelta(hours=self.next_hour if hour is None else hour)
            else:
                row[column] = _random_value(self.rng, sql_type)
        return row

    def _newest(self, name: str) -> int | None:
        tc = self.sources[name].time_column
        hours = [int((r[tc] - _EPOCH).total_seconds() // 3600) for r in self.rows[name] if tc and r.get(tc)]
        return max(hours) if hours else None

    def batch(self) -> list[str]:
        statements: list[str] = []
        for _ in range(self.rng.randint(1, 2)):
            op = self.rng.choice(sorted(self.kinds))
            if op == "empty":
                continue
            name = self.rng.choice(self.tables)
            table, rows = self.sources[name], self.rows[name]
            newest = self._newest(name)
            if op == "insert_new":
                self.next_hour = (newest + 1) if newest is not None else 0
                row = self._fresh(name, None)
            elif op == "null_key":
                self.next_hour = (newest + 1) if newest is not None else 0
                row = self._fresh(name, None)
                for column in table.key:
                    row[column] = None
            elif op == "insert_boundary":
                if newest is None:
                    continue
                row = self._fresh(name, newest)
            elif op == "insert_late":
                if not newest:
                    continue
                row = self._fresh(name, self.rng.randint(0, newest - 1))
            elif op == "duplicate":
                if not rows:
                    continue
                row = dict(self.rng.choice(rows))
            elif op in ("update", "update_touch"):
                if not rows:
                    continue
                old = self.rng.choice(rows)
                new = dict(old)
                for column, sql_type in table.columns.items():
                    if column in table.key or column == table.time_column:
                        continue
                    new[column] = _random_value(self.rng, sql_type)
                if op == "update_touch" and table.time_column:
                    new[table.time_column] = _EPOCH + dt.timedelta(hours=(newest or 0) + 1)
                if new == old:
                    continue
                assignments = ", ".join(f'"{c}" = {_literal(new[c])}' for c in new if new[c] != old[c])
                statements.append(f'UPDATE "{name}" SET {assignments} WHERE {_where(table, old)}')
                for i, r in enumerate(rows):
                    if r == old:
                        rows[i] = new
                continue
            elif op == "delete":
                if not rows:
                    continue
                old = self.rng.choice(rows)
                statements.append(f'DELETE FROM "{name}" WHERE {_where(table, old)}')
                rows[:] = [r for r in rows if r != old]
                continue
            else:  # pragma: no cover
                continue
            statements.append(_insert(name, table, row))
            rows.append(row)
        return statements


@dataclass(frozen=True)
class Counterexample:
    """A replayable source-change sequence on which the model diverges."""

    initial: tuple[str, ...]
    batches: tuple[tuple[str, ...], ...]
    batch_index: int
    status: str
    detail: str

    @property
    def size(self) -> int:
        return len(self.initial) + sum(len(b) for b in self.batches)


def _diverges(model, sources, initial, batches) -> BatchResult | None:
    try:
        return first_divergence(replay(model, sources, initial, batches))
    except IncrementalError:
        return None


def minimize(model, sources, initial, batches) -> tuple[list[str], list[list[str]]]:
    """Greedy shrink: drop whole batches, then single statements, while it still diverges."""

    initial, batches = list(initial), [list(b) for b in batches]

    def still(i, b):
        return _diverges(model, sources, i, b) is not None

    changed = True
    while changed:
        changed = False
        for i in range(len(batches) - 1, -1, -1):
            trial = batches[:i] + batches[i + 1 :]
            if still(initial, trial):
                batches, changed = trial, True
        for i in range(len(initial) - 1, -1, -1):
            trial = initial[:i] + initial[i + 1 :]
            if still(trial, batches):
                initial, changed = trial, True
        for bi in range(len(batches)):
            for si in range(len(batches[bi]) - 1, -1, -1):
                trial = [list(b) for b in batches]
                del trial[bi][si]
                if still(initial, trial):
                    batches, changed = trial, True
                    break
    # trailing batches after the first divergence are never needed
    result = _diverges(model, sources, initial, batches)
    if result is not None:
        batches = batches[: result.index]
    return initial, batches


def search_divergence(
    model: IncrementalModel,
    sources: dict[str, SourceTable],
    kinds: Iterable[str],
    *,
    seeds: int = 60,
    batches: int = 4,
    seed: int = 0,
    tables: tuple[str, ...] | None = None,
    time_limit: float | None = None,
) -> Counterexample | None:
    """Look for a source-change sequence allowed by ``kinds`` that makes the model diverge."""

    kinds = frozenset(kinds)
    unknown = kinds - set(CHANGE_KINDS)
    if unknown:
        raise IncrementalError(f"unknown change kinds: {sorted(unknown)}")
    deadline = None if time_limit is None else time.monotonic() + time_limit
    for s in range(seeds):
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError("counterexample search ran out of time")
        rng = random.Random(seed * 100003 + s)
        gen = _Generator(sources, kinds, rng, tables)
        initial: list[str] = []
        for name in sorted(sources):
            for _ in range(rng.randint(0, 2)):
                gen.next_hour = rng.randint(0, 2)
                row = gen._fresh(name, None)
                initial.append(_insert(name, sources[name], row))
                gen.rows[name].append(row)
        plan = [gen.batch() for _ in range(batches)]
        try:
            result = first_divergence(replay(model, sources, initial, plan))
        except IncrementalError:
            continue
        if result is not None:
            small_i, small_b = minimize(model, sources, initial, plan)
            final = _diverges(model, sources, small_i, small_b)
            if final is None:  # pragma: no cover - minimisation keeps divergence
                continue
            detail = final.error or f"incremental has {len(final.only_incremental)} extra and {len(final.only_full)} missing rows"
            return Counterexample(tuple(small_i), tuple(tuple(b) for b in small_b), final.index, final.status, detail)
    return None


# ---------------------------------------------------------------------------
# Static proof rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    outcome: str  # safe | diverges | unknown | unsupported
    rule: str
    detail: str = ""
    counterexample: Counterexample | None = None


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    return [node]


def _watermark_predicate(conjunct: exp.Expression, target: str, source_column: str, target_column: str) -> str | None:
    """``ts >[=] [TIMESTAMP_SUB(]COALESCE((SELECT MAX(col) FROM self), <old date>)[, ...)]``.

    Returns ``">"`` or ``">="``. A lookback (``TIMESTAMP_SUB``) only lowers the
    boundary, so it reads a superset of the rows the plain watermark would.
    """

    if not isinstance(conjunct, (exp.GT, exp.GTE)):
        return None
    left, right = conjunct.left, conjunct.right
    if not (isinstance(left, exp.Column) and left.name.lower() == source_column.lower()):
        return None
    lookback = False
    if isinstance(right, (exp.TimestampSub, exp.DatetimeSub)):
        right, lookback = right.this, True
    # An empty table gives MAX = NULL and ``ts > NULL`` reads nothing, so only a
    # COALESCE to a date before any event keeps the first rows from being lost.
    if not isinstance(right, exp.Coalesce):
        return None
    year = re.search(r"(\d{4})-\d{2}-\d{2}", right.expressions[0].sql()) if right.expressions else None
    if not year or int(year.group(1)) > 2000:
        return None
    right = right.this
    if isinstance(right, exp.Paren):
        right = right.this
    if isinstance(right, exp.Subquery):
        right = right.this
    if not isinstance(right, exp.Select) or len(right.expressions) != 1:
        return None
    agg = right.expressions[0]
    if not (isinstance(agg, exp.Max) and isinstance(agg.this, exp.Column) and agg.this.name.lower() == target_column.lower()):
        return None
    source = right.args.get("from_") or right.args.get("from")
    table = source.this if source is not None else None
    if not (isinstance(table, exp.Table) and table.name.lower() == target.lower()) or right.args.get("where"):
        return None
    return ">=" if isinstance(conjunct, exp.GTE) or lookback else ">"


def _row_wise(select: exp.Select, ignore: frozenset[str] = frozenset()) -> bool:
    """A single-table select-project-filter with no state across rows.

    ``CURRENT_TIMESTAMP`` is allowed only as the value of an ignored audit column.
    """

    if not isinstance(select, exp.Select) or select.args.get("with_") or select.args.get("with"):
        return False
    if any(select.args.get(k) for k in ("joins", "group", "having", "qualify", "distinct", "limit", "offset", "order", "windows", "laterals")):
        return False
    source = select.args.get("from_") or select.args.get("from")
    if source is None or not isinstance(source.this, exp.Table):
        return False
    clock = (exp.CurrentTimestamp, exp.CurrentDate, exp.CurrentDatetime)
    banned = (exp.AggFunc, exp.Window, exp.Subquery, exp.Select, exp.Rand, exp.Unnest) + clock
    for node in select.walk():
        if node is select:
            continue
        if isinstance(node, clock):
            audit = isinstance(node.parent, exp.Alias) and node.parent.alias.lower() in ignore and node.parent.parent is select
            if not audit:
                return False
        elif isinstance(node, banned):
            return False
    return True


def _strip(select: exp.Select, drop: exp.Expression) -> exp.Expression:
    copy = select.copy()
    where = copy.args.get("where")
    kept = [c for c in _conjuncts(where.this if where else None) if c.sql() != drop.sql()]
    if kept:
        condition = kept[0]
        for c in kept[1:]:
            condition = exp.And(this=condition, expression=c)
        copy.set("where", exp.Where(this=condition))
    else:
        copy.set("where", None)
    return copy


def _projected_as(select: exp.Select, column: str) -> str | None:
    """Output name under which ``column`` is selected unchanged, or None."""

    for e in select.expressions:
        if isinstance(e, exp.Star):
            return column
        if isinstance(e, exp.Column) and e.name.lower() == column.lower():
            return e.name
        if isinstance(e, exp.Alias) and isinstance(e.this, exp.Column) and e.this.name.lower() == column.lower():
            return e.alias
    return None


def prove_watermark(
    model: IncrementalModel, sources: dict[str, SourceTable], kinds: frozenset[str]
) -> Verdict | None:
    """Proof rules for a watermark over a row-wise query.

    The incremental query must be the full query plus one conjunct
    ``ts >[=] [TIMESTAMP_SUB(] COALESCE((SELECT MAX(ts) FROM self), <old date>) [...)]``,
    the full query a single-table select-project-filter, and ``ts`` projected
    unchanged. The COALESCE default is assumed to precede every event time, and
    ``CURRENT_TIMESTAMP`` may appear only as an ignored audit column. Then:

    * **R1, append.** No ``uniqueKey``, strict ``>``, only ``insert_new`` (and
      empty runs). Each new row's time exceeds every earlier one, so a run
      appends exactly the full query's output on the new rows.
    * **R2, merge.** ``uniqueKey`` equal to the source's declared key (projected
      unchanged, never NULL, unique), and only ``insert_new``, ``update_touch``
      and empty runs with ``>``, or also ``insert_boundary`` with ``>=`` (or a
      lookback). Every changed row has a time at or after the table's maximum,
      so it is re-read, and the merge replaces the row with its current output.
      Re-read unchanged rows merge to themselves; the key is unique in the
      source, so no merge has two source rows for one target row.
      ``update_touch`` needs a query without ``WHERE``: an update that makes a
      row fail the filter would leave its old version in the table.
    """

    if model.pre_operations:
        return None
    try:
        full = sqlglot.parse_one(model.full_sql, read=model.dialect)
        incremental = sqlglot.parse_one(model.incremental_sql, read=model.dialect)
    except sqlglot.errors.SqlglotError:
        return None
    ignore = frozenset(c.lower() for c in model.ignore_columns)
    if not _row_wise(full, ignore) or not isinstance(incremental, exp.Select):
        return None
    source = full.args.get("from_") or full.args.get("from")
    table = sources.get(source.this.name) if source is not None else None
    if table is None or table.time_column is None:
        return None
    tc = table.time_column
    target_tc = _projected_as(full, tc)
    if target_tc is None:
        return None
    where = incremental.args.get("where")
    marks = [(c, _watermark_predicate(c, model.target, tc, target_tc)) for c in _conjuncts(where.this if where else None)]
    marks = [(c, op) for c, op in marks if op]
    if len(marks) != 1:
        return None
    conjunct, op = marks[0]
    if _strip(incremental, conjunct).sql() != _strip(full, exp.Null()).sql():
        return None
    if not model.unique_key:
        if op == ">" and kinds <= {"insert_new", "empty"}:
            return Verdict("safe", "R1 append-only strict watermark", "row-wise query, strict watermark on the table's own time column, in-order inserts only")
        return None
    if set(model.unique_key) != set(table.key) or any(_projected_as(full, k) != k for k in table.key):
        return None
    allowed = {"insert_new", "update_touch", "empty"} | ({"insert_boundary"} if op == ">=" else set())
    if "update_touch" in kinds and full.args.get("where"):
        return None  # an update can move a row out of the filter, and a merge never deletes it
    if kinds <= allowed:
        return Verdict("safe", "R2 merge on a watermark", "row-wise query, merge on the source's unique key, every change at or after the table's newest time")
    return None


def check_incremental(
    model: IncrementalModel,
    sources: dict[str, SourceTable],
    kinds: Iterable[str],
    *,
    seeds: int = 60,
    batches: int = 4,
    tables: tuple[str, ...] | None = None,
    time_limit: float | None = 30.0,
) -> Verdict:
    """Decide whether the model equals its full refresh under every change in ``kinds``.

    ``tables`` limits which source tables the changes may touch (default: all).
    """

    kinds = frozenset(kinds)
    try:
        proof = prove_watermark(model, sources, kinds)
        if proof is None:
            from .incremental_rules import prove_more

            proof = prove_more(model, sources, kinds, tables)
        if proof is not None:
            return proof
        found = search_divergence(model, sources, kinds, seeds=seeds, batches=batches, tables=tables, time_limit=time_limit)
    except TimeoutError as exc:
        return Verdict("timeout", "counterexample search", str(exc))
    except IncrementalError as exc:
        return Verdict("unsupported", "simulator", str(exc))
    if found is not None:
        return Verdict("diverges", "counterexample search", found.detail, found)
    return Verdict("unknown", "counterexample search", f"no divergence in {seeds} random sequences; no proof rule applies")
