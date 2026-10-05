"""A small database that shows a query's result changing with the order its rows are stored in.

:mod:`kumosql.tie_determinism` says a window, ``LIMIT`` or aggregate *may* depend on how tied rows
are broken. ``find_tie_witness`` shows it: a database of a few rows, and two physical orders of those
rows, on which DuckDB (one thread, BigQuery SQL through :mod:`kumosql.bigquery_on_duckdb`) returns two
different bags. With ``SET threads=1`` DuckDB breaks ties in windows, ``LIMIT`` and ``ARRAY_AGG`` by
storage order, which is what makes a physical-order witness replay.

The search tries the databases of :func:`kumosql.targeted_data.database_suite` built around the query,
plus variants of them in which the rows of a table agree on every column but one (the shape a tie
takes: equal sort keys, different payload). A database where the query fails is skipped. On the first
database where one of the storage orders of :func:`kumosql.refute._orders` changes the result, rows are
dropped greedily while the result still depends on the order, which usually leaves two tied rows. The
declared facts always hold (``refute.legal``): NOT NULL columns, unique keys and foreign keys, so a
witness never contradicts a fact the analysis rested on. Both orders are then re-run twice, and again
with DuckDB's optimizer off (:func:`kumosql.duckdb_load.run_unoptimized`); a difference that does not
repeat identically, or that the unoptimized run does not show, is dropped, so a witness never rests on
a random function or an engine bug.

The result is JSON (``tables``, the row ``orders``, the two ``results``) and :func:`replay` rebuilds
the database from it and checks all of this again, so a stored witness can be re-verified later.

A witness shows that the query *can* return different rows on some database that keeps the declared
facts; no witness does not show the query is deterministic. Which of a query's tie sites the witness
exercises is not recorded: it belongs to the query as a whole.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import time
from typing import Any, Iterator, Mapping, Sequence

from .duckdb_load import run_unoptimized
from .refute import _order_stable, _orders, legal, repair_foreign_keys
from .result_equivalence import (
    DataRules,
    DatasetRunner,
    ExecutionError,
    QueryOutput,
    SyntheticDataset,
    SyntheticTable,
    _bigquery_rows,
    _normalize_type,
    compare_outputs,
)
from .targeted_data import database_suite

SETTINGS = ("SET threads=1",)
_COMPARE = {"check_column_names": False, "ignore_row_order": True}


def find_tie_witness(
    sql: str,
    schema: Mapping[str, Mapping[str, str]],
    rules: Mapping[str, DataRules] | None = None,
    *,
    foreign_keys: Sequence[tuple] = (),
    dialect: str = "bigquery",
    budget: float = 20.0,
    timeout: float = 5.0,
) -> dict | None:
    """A tie witness for ``sql`` as JSON, or ``None`` if none was found within ``budget`` seconds.

    ``schema`` maps each table to its columns and BigQuery types. ``rules`` are the declared NOT NULL
    columns and keys (lower-case names) and ``foreign_keys`` the ``(child, child_columns, parent,
    parent_columns)`` facts; every database tried keeps them.
    """

    started = time.monotonic()
    rules = rules or {}
    single_fks = _single_column_keys(foreign_keys)
    try:
        suite = database_suite(sql, schema, rules, dialect=dialect, random_seeds=range(1, 5))
    except Exception:  # noqa: BLE001 - a query the generators cannot read gets no witness
        return None
    try:
        with DatasetRunner(schema, dialect, SETTINGS) as runner:
            seen: set[int] = set()
            for dataset in _candidates([labeled.dataset for labeled in suite], rules):
                if time.monotonic() - started > budget:
                    return None
                dataset = repair_foreign_keys(dataset, single_fks, rules)
                if not legal(dataset, rules, foreign_keys) or not any(len(t.rows) >= 2 for t in dataset.tables.values()):
                    continue
                key = hash(tuple((k, t.rows) for k, t in sorted(dataset.tables.items())))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    if _order_stable(runner, sql, dataset, _COMPARE):
                        continue
                except ExecutionError:
                    continue
                small = _shrink(runner, sql, dataset, rules, foreign_keys, started + budget)
                witness = _confirmed(runner, sql, small, schema, rules, foreign_keys, dialect)
                if witness is not None:
                    return witness
    except ExecutionError:
        return None
    return None


def _single_column_keys(foreign_keys: Sequence[tuple]) -> tuple:
    return tuple(
        (child, cols[0], parent, parent_cols[0])
        for child, cols, parent, parent_cols in foreign_keys
        if len(cols) == 1 and len(parent_cols) == 1
    )


def _candidates(datasets: Sequence[SyntheticDataset], rules: Mapping[str, DataRules]) -> Iterator[SyntheticDataset]:
    """The suite's databases, then variants whose rows agree on every non-key column but one."""

    yield from datasets
    for dataset in datasets:
        if not any(len(t.rows) >= 2 for t in dataset.tables.values()):
            continue
        names = list(dict.fromkeys(c.lower() for t in dataset.tables.values() for c, _ in t.columns))
        for free in names:
            yield SyntheticDataset(dataset.seed, {k: _tied(k, t, rules.get(k.lower()), free) for k, t in dataset.tables.items()})


def _tied(name: str, table: SyntheticTable, rules: DataRules | None, free: str) -> SyntheticTable:
    """``table`` with every non-key column but ``free`` copied from its first row."""

    if len(table.rows) < 2:
        return table
    names = [c.lower() for c, _ in table.columns]
    pinned = {c for key in (rules.keys if rules else ()) for c in key} | {free}
    first = table.rows[0]
    rows = tuple(tuple(v if names[i] in pinned else first[i] for i, v in enumerate(row)) for row in table.rows)
    return SyntheticTable(table.columns, rows)


def _shrink(
    runner: DatasetRunner,
    sql: str,
    dataset: SyntheticDataset,
    rules: Mapping[str, DataRules],
    foreign_keys: Sequence[tuple],
    deadline: float,
) -> SyntheticDataset:
    """Drop rows one at a time while the result still depends on the storage order."""

    tables = {k: list(t.rows) for k, t in dataset.tables.items()}

    def build() -> SyntheticDataset:
        return SyntheticDataset(dataset.seed, {k: SyntheticTable(dataset.tables[k].columns, tuple(rows)) for k, rows in tables.items()})

    changed = True
    while changed and time.monotonic() < deadline:
        changed = False
        for name in tables:
            index = 0
            while index < len(tables[name]) and time.monotonic() < deadline:
                row = tables[name].pop(index)
                trial = build()
                try:
                    keeps = legal(trial, rules, foreign_keys) and not _order_stable(runner, sql, trial, _COMPARE)
                except ExecutionError:
                    keeps = False
                if keeps:
                    changed = True
                else:
                    tables[name].insert(index, row)
                    index += 1
    return build()


def _permutation(base: Sequence[tuple], other: Sequence[tuple]) -> list[int]:
    """Indices into ``base`` that list its rows in the order of ``other`` (``other`` reorders ``base``)."""

    used: set[int] = set()
    order = []
    for row in other:
        index = next(i for i, candidate in enumerate(base) if i not in used and candidate == row)
        used.add(index)
        order.append(index)
    return order


def _differing_order(runner: DatasetRunner, sql: str, dataset: SyntheticDataset) -> tuple[SyntheticDataset, QueryOutput, QueryOutput] | None:
    first = runner.run(sql, dataset)
    for other in _orders(dataset):
        output = runner.run(sql, other)
        if not compare_outputs(first, output, **_COMPARE)[0]:
            return other, first, output
    return None


def _unoptimized(runner: DatasetRunner, sql: str, dataset: SyntheticDataset, columns: tuple[str, ...]) -> QueryOutput:
    """``sql`` over ``dataset`` with DuckDB's optimizer off, as the runner's own output type."""

    text = runner.prepare(sql)
    runner.load(dataset)
    rows = run_unoptimized(runner._connection, text)[0]  # noqa: SLF001 - the connection that holds the tables
    return QueryOutput(columns=columns, rows=_bigquery_rows(tuple(tuple(r) for r in rows), runner.dialect))


def _agree(a: QueryOutput, b: QueryOutput) -> bool:
    return compare_outputs(a, b, **_COMPARE)[0]


def _confirmed(
    runner: DatasetRunner,
    sql: str,
    dataset: SyntheticDataset,
    schema: Mapping[str, Mapping[str, str]],
    rules: Mapping[str, DataRules],
    foreign_keys: Sequence[tuple],
    dialect: str,
) -> dict | None:
    """The witness for ``dataset`` if both storage orders repeat and show the same difference unoptimized."""

    try:
        found = _differing_order(runner, sql, dataset)
        if found is None:
            return None
        other, first, second = found
        columns = first.columns
        for data, want in ((dataset, first), (other, second)):
            if not (_agree(runner.run(sql, data), want) and _agree(_unoptimized(runner, sql, data, columns), want)):
                return None
        if _agree(_unoptimized(runner, sql, dataset, columns), _unoptimized(runner, sql, other, columns)):
            return None
    except ExecutionError:
        return None
    return _to_json(sql, dialect, schema, rules, foreign_keys, dataset, other, first, second)


def _to_json(sql, dialect, schema, rules, foreign_keys, dataset, other, first, second) -> dict:
    names = list(dataset.tables)
    return {
        "sql": sql,
        "dialect": dialect,
        "schema": {name: {column: type_ for column, type_ in columns.items()} for name, columns in schema.items()},
        "rules": {
            name: {"not_null": sorted(rule.not_null), "keys": [list(key) for key in rule.keys]}
            for name, rule in rules.items()
        },
        "foreign_keys": [[child, list(cols), parent, list(pcols)] for child, cols, parent, pcols in foreign_keys],
        "tables": {
            name: [dict(zip((c for c, _ in dataset.tables[name].columns), _encode(list(row)))) for row in dataset.tables[name].rows]
            for name in names
        },
        "orders": {
            "first": {name: list(range(len(dataset.tables[name].rows))) for name in names},
            "second": {name: _permutation(dataset.tables[name].rows, other.tables[name].rows) for name in names},
        },
        "results": {
            "first": [_encode(list(row)) for row in first.rows],
            "second": [_encode(list(row)) for row in second.rows],
        },
    }


def _encode(value: Any) -> Any:
    """``value`` as JSON: dates and decimals as text, containers recursively."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _decode(value: Any, type_: str) -> Any:
    if value is None:
        return None
    kind = _normalize_type(type_)
    if kind == "DATE":
        return date.fromisoformat(value)
    if kind == "TIMESTAMP":
        return datetime.fromisoformat(value)
    if kind == "NUMERIC":
        return Decimal(value)
    return value


def _dataset(witness: Mapping[str, Any], which: str) -> SyntheticDataset:
    tables = {}
    for name, rows in witness["tables"].items():
        columns = tuple((c, _normalize_type(t)) for c, t in witness["schema"][name].items())
        decoded = [tuple(_decode(row[c], t) for c, t in columns) for row in rows]
        tables[name] = SyntheticTable(columns, tuple(decoded[i] for i in witness["orders"][which][name]))
    return SyntheticDataset(0, tables)


def replay(witness: Mapping[str, Any]) -> bool:
    """Whether ``witness`` (as returned by :func:`find_tie_witness`, possibly after a JSON round trip) still holds.

    The database keeps the declared facts, the query returns the recorded rows in each of the two
    orders, with the optimizer on and off, and the two results differ.
    """

    try:
        rules = {
            name: DataRules(frozenset(rule["not_null"]), tuple(tuple(k) for k in rule["keys"]))
            for name, rule in witness["rules"].items()
        }
        foreign_keys = [(child, tuple(cols), parent, tuple(pcols)) for child, cols, parent, pcols in witness["foreign_keys"]]
        first, second = _dataset(witness, "first"), _dataset(witness, "second")
        if not (legal(first, rules, foreign_keys) and legal(second, rules, foreign_keys)):
            return False
        sql = witness["sql"]
        with DatasetRunner(witness["schema"], witness.get("dialect", "bigquery"), SETTINGS) as runner:
            outputs = []
            for dataset, recorded in ((first, witness["results"]["first"]), (second, witness["results"]["second"])):
                want = sorted(repr(row) for row in recorded)
                run = runner.run(sql, dataset)
                if sorted(repr(_encode(list(row))) for row in run.rows) != want:
                    return False
                again = _unoptimized(runner, sql, dataset, run.columns)
                if sorted(repr(_encode(list(row))) for row in again.rows) != want:
                    return False
                outputs.append(run)
            return not compare_outputs(outputs[0], outputs[1], **_COMPARE)[0]
    except (ExecutionError, KeyError, ValueError, TypeError):
        return False
