"""Find a database on which two queries differ, using the targeted suite.

One entry point for the equivalence evals: given the queries (in the dialect they
were written in), the declared tables, keys, NOT NULL columns and foreign keys,
``find_targeted_difference`` runs both queries over the databases of
:func:`kumosql.targeted_data.database_suite` built around each of them, and
returns the first database where the results differ, after confirming that the
difference repeats. The databases respect the declared facts: NOT NULL, keys and
foreign keys (a child value with no parent is pointed at a parent row, set to
NULL where allowed, or its row dropped).

``engine="duckdb"`` runs SQL written in ``dialect`` (BigQuery by default; ``settings`` are DuckDB ``SET`` statements) through the shared
:class:`~kumosql.result_equivalence.DatasetRunner`; ``engine="sqlite"`` runs the
SQL as written in SQLite, for evals whose labels come from SQLite semantics.
A refutation can be shrunk with :func:`kumosql.minimize.minimize_failure` and
replayed from JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import sqlite3
import threading
import time
from typing import Any, Mapping, Sequence

from .result_equivalence import (
    DataRules,
    DatasetRunner,
    ExecutionError,
    QueryOutput,
    QueryTimeout,
    SyntheticDataset,
    SyntheticTable,
    compare_outputs,
)
from .targeted_data import database_suite, random_datasets

_SQLITE_TYPES = {
    "INT64": "INTEGER",
    "FLOAT64": "REAL",
    "NUMERIC": "REAL",
    "STRING": "TEXT",
    "BOOL": "INTEGER",
    "DATE": "TEXT",
    "TIMESTAMP": "TEXT",
}

ForeignKey = tuple  # (child table, child column, parent table, parent column), lower-case


@dataclass(frozen=True)
class Refutation:
    """A database on which the two queries differ, with the label of the scenario that found it."""

    label: str
    dataset: SyntheticDataset
    left: QueryOutput
    right: QueryOutput

    @property
    def rows(self) -> int:
        return sum(len(t.rows) for t in self.dataset.tables.values())


class SqliteRunner:
    """The ``DatasetRunner`` interface over SQLite: the SQL runs exactly as written."""

    def __init__(self, schema: Any = None):
        self._connection = sqlite3.connect(":memory:")
        self._loaded: SyntheticDataset | None = None
        self._created: set[str] = set()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SqliteRunner":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @staticmethod
    def _value(value: Any) -> Any:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, datetime):
            return value.isoformat(sep=" ")
        if isinstance(value, date):
            return value.isoformat()
        return value

    def load(self, dataset: SyntheticDataset) -> None:
        if dataset is self._loaded:
            return
        for name, table in dataset.tables.items():
            if name not in self._created:
                columns = ", ".join(f'"{c}" {_SQLITE_TYPES[t]}' for c, t in table.columns)
                self._connection.execute(f'CREATE TABLE "{name}" ({columns})')
                self._created.add(name)
            self._connection.execute(f'DELETE FROM "{name}"')
            if table.rows:
                marks = ", ".join("?" * len(table.columns))
                self._connection.executemany(
                    f'INSERT INTO "{name}" VALUES ({marks})',
                    [tuple(self._value(v) for v in row) for row in table.rows],
                )
        self._loaded = dataset

    def run(self, sql: str, dataset: SyntheticDataset, *, timeout: float | None = None) -> QueryOutput:
        self.load(dataset)
        deadline = None if timeout is None else time.monotonic() + timeout
        if deadline is not None:
            self._connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        try:
            cursor = self._connection.execute(sql.strip().rstrip(";"))
            columns = tuple(c[0] for c in cursor.description)
            rows = tuple(tuple(row) for row in cursor.fetchall())
        except sqlite3.OperationalError as exc:
            if deadline is not None and time.monotonic() > deadline:
                raise QueryTimeout(f"query ran longer than {timeout} s") from exc
            raise ExecutionError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise ExecutionError(str(exc)) from exc
        finally:
            self._connection.set_progress_handler(None, 0)
        return QueryOutput(columns=columns, rows=rows)


def repair_foreign_keys(
    dataset: SyntheticDataset,
    foreign_keys: Sequence[ForeignKey],
    rules: Mapping[str, DataRules] | None = None,
) -> SyntheticDataset:
    """Make every non-NULL child value of a foreign key a value that exists in the parent.

    A value with no parent is replaced by the first parent value; with no parent
    rows it becomes NULL, or the row is dropped when the column is NOT NULL.
    """

    if not foreign_keys:
        return dataset
    tables = {k: [list(r) for r in t.rows] for k, t in dataset.tables.items()}
    index = {k: {c.lower(): i for i, (c, _) in enumerate(t.columns)} for k, t in dataset.tables.items()}
    for child, child_column, parent, parent_column in foreign_keys:
        if child not in tables or parent not in tables:
            continue
        ci, pi = index[child][child_column], index[parent][parent_column]
        parent_values = [r[pi] for r in tables[parent] if r[pi] is not None]
        allowed = set(parent_values)
        required = child_column in (rules[child].not_null if rules and child in rules else frozenset())
        kept = []
        for row in tables[child]:
            if row[ci] is not None and row[ci] not in allowed:
                if parent_values:
                    row[ci] = parent_values[0]
                elif required:
                    continue
                else:
                    row[ci] = None
            kept.append(row)
        tables[child] = kept
    return SyntheticDataset(
        dataset.seed,
        {k: SyntheticTable(t.columns, tuple(tuple(r) for r in tables[k])) for k, t in dataset.tables.items()},
    )


def _confirmed(runner, left: str, right: str, dataset: SyntheticDataset, compare: Mapping[str, Any]) -> bool:
    try:
        a1, a2 = runner.run(left, dataset), runner.run(left, dataset)
        b1, b2 = runner.run(right, dataset), runner.run(right, dataset)
    except ExecutionError:
        return False
    return compare_outputs(a1, a2, **compare)[0] and compare_outputs(b1, b2, **compare)[0]


def find_targeted_difference(
    left: str,
    right: str,
    schema: Mapping[str, Mapping[str, str]],
    rules: Mapping[str, DataRules] | None = None,
    *,
    foreign_keys: Sequence[ForeignKey] = (),
    engine: str = "duckdb",
    dialect: str = "bigquery",
    random_seeds=range(1, 5),
    ordered: bool = False,
    timeout: float = 5.0,
    budget: float = 60.0,
    settings: Sequence[str] = (),
) -> Refutation | None:
    """The first database on which ``left`` and ``right`` differ, or ``None``.

    ``schema`` maps a table to its columns and BigQuery types (``INT64``,
    ``STRING``...). Databases come from the suite built around ``left`` and the
    suite built around ``right``, then a few random ones. A database where either
    query errors is skipped; a difference that does not repeat is not reported.
    ``ordered`` compares the rows in order (queries with ``ORDER BY ... LIMIT``).
    """

    compare = {"check_column_names": False, "ignore_row_order": not ordered}
    started = time.monotonic()
    runner_class = SqliteRunner if engine == "sqlite" else DatasetRunner
    seen: set[int] = set()

    def datasets():
        for sql in (left, right):
            try:
                suite = database_suite(sql, schema, rules, dialect=dialect, random_seeds=())
            except Exception:
                suite = []
            for labeled in suite:
                yield labeled.label, labeled.dataset
        for labeled in random_datasets(schema, rules, random_seeds):
            yield labeled.label, labeled.dataset

    runner_args = (schema, dialect, settings) if engine != "sqlite" else (schema,)
    with runner_class(*runner_args) as runner:
        for label, dataset in datasets():
            if time.monotonic() - started > budget:
                return None
            dataset = repair_foreign_keys(dataset, foreign_keys, rules)
            key = hash(tuple((k, t.rows) for k, t in sorted(dataset.tables.items())))
            if key in seen:
                continue
            seen.add(key)
            try:
                a = runner.run(left, dataset, timeout=timeout)
                b = runner.run(right, dataset, timeout=timeout)
            except ExecutionError:
                continue
            if compare_outputs(a, b, **compare)[0]:
                continue
            if _confirmed(runner, left, right, dataset, compare):
                return Refutation(label, dataset, a, b)
    return None


def infer_schema(
    queries: Sequence[str],
    known: Mapping[str, Sequence[str]] | None = None,
    types: Mapping[str, Mapping[str, str]] | None = None,
    dialect: str = "bigquery",
) -> dict[str, dict[str, str]]:
    """Tables and typed columns the queries read, for pasted queries with no declared schema.

    Columns come from ``known`` where the table is declared, otherwise from the
    column references in the queries. A column is STRING or FLOAT64 when it is
    compared with such a literal, otherwise INT64.
    """

    import sqlglot
    from sqlglot import exp

    known = {k.lower(): list(v) for k, v in (known or {}).items()}
    types = {k.lower(): {c.lower(): t for c, t in v.items()} for k, v in (types or {}).items()}
    schema: dict[str, dict[str, str]] = {}
    for sql in queries:
        tree = sqlglot.parse_one(sql, read=dialect)
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        aliases: dict[str, str] = {}
        tables: list[str] = []
        for table in tree.find_all(exp.Table):
            name = table.name.lower()
            if not name or name in ctes:
                continue
            aliases[(table.alias or table.name).lower()] = name
            tables.append(name)
            schema.setdefault(name, {})
        for column in tree.find_all(exp.Column):
            owner = aliases.get(column.table.lower()) if column.table else (tables[0] if len(set(tables)) == 1 else None)
            if owner is None:
                continue
            guess = "INT64"
            parent = column.parent
            if isinstance(parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like)):
                other = parent.expression if parent.this is column else parent.this
                if isinstance(other, exp.Literal):
                    guess = "STRING" if other.is_string else ("FLOAT64" if "." in other.name else "INT64")
            elif isinstance(parent, exp.Like):
                guess = "STRING"
            name = column.name.lower()
            declared = types.get(owner, {}).get(name)
            if declared is not None:
                guess = declared
            current = schema[owner].get(name)
            if current is None or (current == "INT64" and guess != "INT64"):
                schema[owner][name] = guess
    for name, columns in list(schema.items()):
        for extra in known.get(name, ()):
            columns.setdefault(extra.lower(), types.get(name, {}).get(extra.lower(), "INT64"))
        if not columns:
            columns["id"] = "INT64"
    return schema


def counterexample_from_search(
    left: str,
    right: str,
    known: Mapping[str, Sequence[str]] | None = None,
    rules: Mapping[str, DataRules] | None = None,
    types: Mapping[str, Mapping[str, str]] | None = None,
    *,
    budget: float = 15.0,
) -> dict | None:
    """A minimal (fewest rows) database on which two pasted queries differ, as JSON, or ``None``."""

    from .minimize import minimize_failure

    try:
        schema = infer_schema([left, right], known, types)
        found = find_targeted_difference(left, right, schema, rules, budget=budget)
        if found is None:
            return None
        small = minimize_failure(left, right, schema, found.dataset, rules, time_limit=budget)
    except Exception:  # noqa: BLE001 - no witness is the same as not searching
        return None
    with DatasetRunner(small.schema) as runner:
        a = runner.run(small.left_sql, small.dataset)
        b = runner.run(small.right_sql, small.dataset)
    return {
        "tables": {
            name: [dict(zip((c for c, _ in t.columns), row)) for row in t.rows]
            for name, t in small.dataset.tables.items()
        },
        "left_rows": [list(r) for r in a.rows],
        "right_rows": [list(r) for r in b.rows],
    }
