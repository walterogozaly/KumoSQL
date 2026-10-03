"""Synthesize a database on which two queries differ, for pairs the provers leave ``not_proven``.

The SMT prover returns a counterexample only when its own model yields one (inner joins, simple
aggregates), and :mod:`kumosql.executed_refutation` runs only BigQuery queries inside a strict
allow-list. Every other pair that differs used to end as ``not_proven``: outer joins, NOT IN with
NULLs, set operations of different shapes, windows, decorrelated subqueries, and differences that
need many rows (``HAVING COUNT(*) > 1000``). This module tries, cheapest first and within one time
limit:

1. **search**: corner-case, targeted and random databases built around both queries
   (:mod:`kumosql.targeted_data`), including narrow ones where joins match, groups share keys and
   rows repeat;
2. **bounded**: :mod:`kumosql.bounded_equivalence`'s z3 encoding over every database of one to
   three symbolic rows per table, the model replayed rather than trusted;
3. **multiplicity**: :mod:`kumosql.refute_bounded`, a few distinct symbolic rows each with a
   symbolic copy count, so a difference that needs a thousand rows is found from two or three.

Every candidate database is judged by :class:`kumosql.refutation_replay.Judge`: it must keep the
declared NOT NULL columns, keys and foreign keys, both queries must run on DuckDB (BigQuery SQL
through :mod:`kumosql.bigquery_on_duckdb`'s guards), the bags must differ with DuckDB's optimizer
on and off and whatever order the rows are stored in. The first database the judge confirms is
shrunk row by row while it still separates the queries and returned; finding nothing proves
nothing. Types must be declared for every column a query reads (no guessing that an untyped
column is an integer).

``KUMOSQL_SYNTHESIS=0`` turns the synthesis off (the evals' baseline runs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import random
import re
import time
from typing import Any, Iterator, Mapping, Sequence

import sqlglot
from sqlglot import exp

from .refutation_replay import Judge, Verdict, bag

ASSUMPTION = "the counterexample was found by running both queries on DuckDB with the declared column types"

_UNSIGNED = re.compile(r"^U(TINY|SMALL|BIG|HUGE)?INT(EGER)?\d*$|^UINT\d+$")


@dataclass
class Synthesis:
    """A confirmed database (positional rows per table, in the schema's column order) and both results."""

    data: dict[str, list[tuple]]
    left_rows: list[tuple]
    right_rows: list[tuple]
    method: str  # search, bounded or multiplicity
    seconds: float
    tried: int = 0  # candidate databases judged
    notes: list[str] = field(default_factory=list)

    @property
    def rows(self) -> int:
        return sum(len(r) for r in self.data.values())


def enabled() -> bool:
    return os.environ.get("KUMOSQL_SYNTHESIS", "1") != "0"


# --- schema -----------------------------------------------------------------------------------------


def tables_read(sql: str, dialect: str) -> set[str]:
    tree = sqlglot.parse_one(sql, read=dialect)
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    names = set()
    for table in tree.find_all(exp.Table):
        full = ".".join(p for p in (table.catalog, table.db, table.name) if p).lower()
        if full and not (not table.db and full in ctes) and not isinstance(table.this, exp.Func):
            names.add(full)
    return names


def typed_schema(
    read: set[str],
    schema: Mapping[str, Sequence[str]],
    types: Mapping[str, Mapping[str, str]],
    constraints: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, str]] | None:
    """``{table: {column: declared type}}`` for every table read, and every table a foreign key of one of
    them points to (a database must hold the parent rows), or ``None`` when a table or a column type is missing."""

    by_name = {k.lower(): k for k in schema}
    by_last: dict[str, list[str]] = {}
    for k in schema:
        by_last.setdefault(k.split(".")[-1].lower(), []).append(k)
    types_lower = {k.lower(): {c.lower(): t for c, t in v.items()} for k, v in types.items()}
    by_constraint = {k.lower(): v for k, v in (constraints or {}).items()}
    wanted = sorted(read)
    for name in wanted:  # grows while foreign keys name parents
        declared = by_constraint.get(name) or by_constraint.get((by_name.get(name) or name).lower())
        for _, parent, _ in getattr(declared, "foreign_keys", ()) or ():
            if parent.lower() not in wanted:
                wanted.append(parent.lower())
    typed: dict[str, dict[str, str]] = {}
    for name in wanted:
        key = by_name.get(name) or (by_last[name][0] if len(by_last.get(name, ())) == 1 else None)
        if key is None:
            return None
        declared = types_lower.get(key.lower())
        if declared is None:
            return None
        columns = {}
        for column in schema[key]:
            if column.lower() not in declared:
                return None
            columns[column] = declared[column.lower()]
        typed[key] = columns
    return typed


def _generation_type(declared: str, dialect: str) -> str | None:
    """The synthetic-data type (``INT64``, ``STRING``...) values of a declared type are drawn from."""

    from .bounded_equivalence import _base_type, kind_of

    base = _base_type(declared)
    if _UNSIGNED.match(base) or base == "HUGEINT":
        return "INT64"
    kind = kind_of(declared)
    if kind == "int":
        return "INT64"
    if kind == "real":
        exact = base in ("NUMERIC", "DECIMAL", "DEC", "NUMBER") and dialect not in ("mysql",)
        return "NUMERIC" if exact else "FLOAT64"
    return {"str": "STRING", "bool": "BOOL", "date": "DATE", "datetime": "TIMESTAMP"}.get(kind)


# --- candidate databases ----------------------------------------------------------------------------


class _Space:
    """The tables, their generation types and rules, and how to turn a synthetic dataset into judge rows."""

    def __init__(self, typed, constraints, dialect):
        from .result_equivalence import DataRules

        self.typed = typed
        self.dialect = dialect
        self.generation = {}
        self.unsigned = {}
        for table, columns in typed.items():
            out = {}
            for column, declared in columns.items():
                gen = _generation_type(declared, dialect)
                if gen is None:
                    raise ValueError(f"no synthetic values for {declared}")
                out[column] = gen
            self.generation[table] = out
            self.unsigned[table] = [bool(_UNSIGNED.match(declared.split("(")[0].strip().upper())) for declared in columns.values()]
        by_lower = {k.lower(): v for k, v in (constraints or {}).items()}
        self.rules = {}
        self.foreign_keys = []
        for table in typed:
            declared = by_lower.get(table.lower())
            if declared is None:
                continue
            keys = tuple(tuple(c.lower() for c in k) for k in declared.keys)
            not_null = frozenset(c.lower() for c in declared.not_null) | {c for k in keys for c in k}
            self.rules[table.lower()] = DataRules(not_null=frozenset(not_null), keys=keys)
            for cols, parent, pcols in declared.foreign_keys:
                self.foreign_keys.append((table, tuple(cols), parent, tuple(pcols)))

    def rows_of(self, dataset) -> dict[str, list[tuple]]:
        by_lower = {k.lower(): t for k, t in dataset.tables.items()}
        out = {}
        for table, columns in self.typed.items():
            synthetic = by_lower.get(table.lower())
            if synthetic is None:
                out[table] = []
                continue
            index = {c.lower(): i for i, (c, _) in enumerate(synthetic.columns)}
            rows = [tuple(row[index[c.lower()]] for c in columns) for row in synthetic.rows]
            unsigned = self.unsigned[table]
            if any(unsigned):
                rows = [r for r in rows if not any(u and v is not None and v < 0 for u, v in zip(unsigned, r))]
            out[table] = rows
        return self._repair(out)

    def _repair(self, data: dict[str, list[tuple]]) -> dict[str, list[tuple]]:
        """Point a child value with no parent at an existing parent value, else NULL, else drop the row."""

        if not self.foreign_keys:
            return data
        data = {t: [list(r) for r in rows] for t, rows in data.items()}
        index = {t: {c.lower(): i for i, c in enumerate(cols)} for t, cols in self.typed.items()}
        by_lower = {t.lower(): t for t in self.typed}
        for child, cols, parent, pcols in self.foreign_keys:
            child, parent = by_lower.get(child.lower()), by_lower.get(parent.lower())
            if child is None or parent is None:
                continue
            ci = [index[child][c.lower()] for c in cols]
            pi = [index[parent][c.lower()] for c in pcols]
            present = [tuple(r[i] for i in pi) for r in data[parent] if all(r[i] is not None for i in pi)]
            required = self.rules.get(child.lower())
            kept = []
            for row in data[child]:
                values = tuple(row[i] for i in ci)
                if None in values or values in present:
                    kept.append(row)
                elif present:
                    for i, v in zip(ci, present[0]):
                        row[i] = v
                    kept.append(row)
                elif not required or not any(c.lower() in required.not_null for c in cols):
                    for i in ci:
                        row[i] = None
                    kept.append(row)
            data[child] = kept
        return {t: [tuple(r) for r in rows] for t, rows in data.items()}


def _candidates(left: str, right: str, space: _Space) -> Iterator[dict[str, list[tuple]]]:
    from .result_equivalence import generate_synthetic_dataset, query_constants
    from .targeted_data import edge_datasets, targeted_datasets

    schema = space.generation
    rules = space.rules
    for labeled in edge_datasets(schema, rules):
        yield space.rows_of(labeled.dataset)
    for sql in (left, right):
        try:
            suite = targeted_datasets(sql, schema, rules, dialect=space.dialect, random_count=6)
        except Exception:  # noqa: BLE001 - a query the targeting cannot read still gets the other databases
            suite = []
        for labeled in suite:
            yield space.rows_of(labeled.dataset)
    try:
        extras = query_constants(left, right)
    except Exception:  # noqa: BLE001
        extras = {}
    yield from (space.rows_of(d) for d in _narrow(schema, rules, extras))
    for seed in range(1, 13):
        yield space.rows_of(generate_synthetic_dataset(schema, seed=seed, rows_per_table=5, null_rate=0.2, rules=rules, extra_values=extras))


def _narrow(schema, rules, extras, count: int = 80):
    """Two to four rows per table over a few values per column (the queries' integers and their
    neighbours), often one value for a whole column: groups that share a key, join partners and
    duplicates are common, which is where HAVING, COUNT(col) and join rewrites part ways. Declared
    NOT NULL columns get no NULL and key columns are made distinct, so few rows are thrown away."""

    from .result_equivalence import _DOMAINS, SyntheticDataset, SyntheticTable, respect_rules

    ints = sorted({v + d for v in extras.get("INT64", ()) for d in (-1, 0, 1)} | {0, 1, 2})[:9]
    rng = random.Random(17)
    for index in range(count):
        tables = {}
        for key, columns in schema.items():
            cols = tuple(columns.items())
            table_rules = rules.get(key.lower())
            required = (table_rules.not_null if table_rules else frozenset()) | {c for k in (table_rules.keys if table_rules else ()) for c in k}
            pools = []
            for _, col_type in cols:
                pool = ints if col_type == "INT64" else list(dict.fromkeys(list(extras.get(col_type, ()))[:2] + list(_DOMAINS[col_type][:3])))
                pools.append([rng.choice(pool)] if rng.random() < 0.4 else pool)
            rows = [
                [None if (name.lower() not in required and rng.random() < 0.25) else rng.choice(pool) for (name, _), pool in zip(cols, pools)]
                for _ in range(0 if rng.random() < 0.15 else rng.randint(1, 4))
            ]
            if rows and rng.random() < 0.3:
                rows.append(list(rng.choice(rows)))
            for key_columns in (table_rules.keys if table_rules else ()):
                positions = [i for i, (name, _) in enumerate(cols) if name.lower() in key_columns]
                seen = set()
                for row in rows:
                    tries = 0
                    while tuple(row[i] for i in positions) in seen and tries < 20:
                        i = positions[tries % len(positions)]
                        full = list(_DOMAINS[cols[i][1]]) + (ints if cols[i][1] == "INT64" else [])
                        row[i] = rng.choice(full)
                        tries += 1
                    seen.add(tuple(row[i] for i in positions))
            rows = respect_rules(cols, [tuple(r) for r in rows], table_rules)
            tables[key] = SyntheticTable(cols, tuple(rows))
        yield SyntheticDataset(1000 + index, tables)


# --- the stages -------------------------------------------------------------------------------------


def _search(judge: Judge, left: str, right: str, space: _Space, deadline: float, limit: int = 240) -> tuple[dict | None, int]:
    seen: set = set()
    tried = 0
    for data in _candidates(left, right, space):
        if time.monotonic() > deadline or tried >= limit:
            break
        key = tuple(sorted((t, tuple(r)) for t, r in data.items()))
        if key in seen:
            continue
        seen.add(key)
        tried += 1
        if judge.verdict(data) is Verdict.DIFFERS:
            return data, tried
    return None, tried


def _bounded(judge: Judge, left: str, right: str, typed, constraints, dialect: str, deadline: float) -> dict | None:
    from . import bounded_equivalence as be

    if be.z3 is None:
        return None
    schema = be.schema_from_prover({t: list(c) for t, c in typed.items()}, constraints, typed)
    found: dict = {}

    def replay(data) -> bool:
        if judge.verdict(data) is Verdict.DIFFERS:
            found["data"] = data
            return True
        return False

    remaining = deadline - time.monotonic()
    if remaining <= 0.2:
        return None
    try:
        result = be._check_bounded(
            left, right, schema, rows=3, dialect=dialect, timeout_ms=int(remaining * 1000), budget_s=remaining, replay=replay,
        )
    except Exception:  # noqa: BLE001 - the encoding refusing a query is no evidence
        return None
    if result.status is be.BoundedStatus.DIFFERENT:
        return found.get("data") or result.counterexample
    return None


def _multiplicity(judge: Judge, left: str, right: str, typed, constraints, dialect: str, deadline: float) -> dict | None:
    try:
        from .refute_bounded import find_counterexample
    except ImportError:  # pragma: no cover
        return None
    return find_counterexample(left, right, typed, constraints, dialect=dialect, judge=judge, deadline=deadline)


def shrink(judge: Judge, data: dict[str, list[tuple]], deadline: float) -> dict[str, list[tuple]]:
    """Drop rows, whole tables first, while the judge still confirms the difference."""

    rows = {t: list(r) for t, r in data.items()}

    def ok(trial) -> bool:
        return judge.verdict(trial) is Verdict.DIFFERS

    for table in sorted(rows):
        if rows[table] and time.monotonic() < deadline and len(rows[table]) <= 64:
            trial = {**rows, table: []}
            if ok(trial):
                rows = trial
    changed = True
    while changed and time.monotonic() < deadline:
        changed = False
        for table in sorted(rows):
            if len(rows[table]) > 64:
                continue  # a large-count witness keeps its copies (the multiplicity stage chose them)
            for i in range(len(rows[table])):
                trial = {**rows, table: rows[table][:i] + rows[table][i + 1 :]}
                if ok(trial):
                    rows = trial
                    changed = True
                    break
            if changed:
                break
    return rows


def synthesize(
    left_sql: str,
    right_sql: str,
    *,
    schema: Mapping[str, Sequence[str]] | None,
    types: Mapping[str, Mapping[str, str]] | None,
    constraints: Mapping[str, Any] | None = None,
    dialect: str = "bigquery",
    time_limit: float = 6.0,
    stages: Sequence[str] = ("search", "bounded", "multiplicity"),
) -> Synthesis | None:
    """A confirmed database on which the two queries return different bags, or ``None``."""

    if not enabled():
        return None
    began = time.monotonic()
    deadline = began + time_limit
    try:
        read = tables_read(left_sql, dialect) | tables_read(right_sql, dialect)
    except (sqlglot.errors.SqlglotError, RecursionError):
        return None
    typed = typed_schema(read, schema or {}, types or {}, constraints) if read else {}
    if typed is None:
        return None
    try:
        import duckdb  # noqa: F401
    except ImportError:
        return None
    constraints = constraints or {}
    by_lower = {k.lower(): v for k, v in constraints.items()}
    keys = {t: [list(k) for k in by_lower[t.lower()].keys] for t in typed if t.lower() in by_lower}
    not_null = {t: sorted(by_lower[t.lower()].not_null) for t in typed if t.lower() in by_lower}
    fks = [(t, list(c), p, list(pc)) for t in typed if t.lower() in by_lower for c, p, pc in by_lower[t.lower()].foreign_keys]
    try:
        judge = Judge(left_sql, right_sql, typed or {"kumo_dual": {"x": "INT64" if dialect == "bigquery" else "INTEGER"}}, dialect=dialect, keys=keys, not_null=not_null, foreign_keys=fks)
    except Exception:  # noqa: BLE001 - no engine, no search
        return None
    with judge:
        if judge.problem is not None:
            return None
        found, method, tried = None, None, 0
        if not typed:
            # queries that read no table: one evaluation decides
            if judge.verdict({"kumo_dual": []}) is Verdict.DIFFERS:
                found, method = {"kumo_dual": []}, "search"
        else:
            try:
                space = _Space(typed, constraints, dialect)
            except ValueError:
                space = None
            for stage in stages:
                if found is not None or time.monotonic() > deadline:
                    break
                if stage == "search" and space is not None:
                    found, tried = _search(judge, left_sql, right_sql, space, began + time_limit * 0.4)
                elif stage == "bounded":
                    found = _bounded(judge, left_sql, right_sql, typed, constraints, dialect, began + time_limit * 0.75)
                elif stage == "multiplicity":
                    found = _multiplicity(judge, left_sql, right_sql, typed, constraints, dialect, deadline)
                method = stage
        if found is None:
            return None
        small = shrink(judge, found, deadline + min(2.0, time_limit / 3))
        if judge.verdict(small) is not Verdict.DIFFERS:
            small = found
        try:
            a, b = judge.outputs(small)
        except judge.duckdb.Error:
            return None
        data = {t: r for t, r in small.items() if t != "kumo_dual"}
        return Synthesis(data, sorted(a, key=repr), sorted(b, key=repr), method or "search", time.monotonic() - began, tried)


def _export(value: Any) -> Any:
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def as_counterexample(found: Synthesis, schema: Mapping[str, Sequence[str]], types: Mapping[str, Mapping[str, str]]):
    """``found`` as the prover's :class:`~kumosql.smt_equivalence.Counterexample` (rows as ``{column: value}``)."""

    from .smt_equivalence import Counterexample

    by_lower = {k.lower(): k for k in schema}
    tables = {}
    for table, rows in found.data.items():
        columns = list(schema[by_lower.get(table.lower(), table)]) if table.lower() in by_lower else list(types.get(table, {}))
        tables[table] = [{c: _export(v) for c, v in zip(columns, row)} for row in rows]
    return Counterexample(
        tables=tables,
        left_rows=[tuple(_export(v) for v in r) for r in found.left_rows],
        right_rows=[tuple(_export(v) for v in r) for r in found.right_rows],
    )


# --- the prover hook --------------------------------------------------------------------------------


def refute_unproven(left_sql: str, right_sql: str, result, **kwargs):
    """``result`` (``not_proven``) turned into ``not_equivalent`` when a confirmed counterexample is found."""

    from .smt_equivalence import SmtEquivalenceResult, SmtStatus

    found = synthesize(
        left_sql, right_sql, schema=kwargs.get("schema"), types=kwargs.get("types"), constraints=kwargs.get("constraints"),
        dialect=kwargs.get("dialect", "bigquery"), time_limit=SYNTHESIS_SECONDS,
    )
    if found is None:
        return result
    return SmtEquivalenceResult(
        SmtStatus.NOT_EQUIVALENT,
        f"the queries return different rows on the attached database ({found.rows} rows, found by {found.method} and replayed)",
        counterexample=as_counterexample(found, kwargs.get("schema") or {}, kwargs.get("types") or {}),
        assumptions=(ASSUMPTION,),
    )


def check_solver_counterexample(left_sql: str, right_sql: str, result, **kwargs):
    """The solver's ``not_equivalent`` ``result``, kept unless its database replays to the same rows.

    The SMT model is a claim about the solver's encoding of the queries; replaying it on DuckDB is
    the check. A database on which both queries demonstrably return the same bag (legal, both run,
    equal bags) is no counterexample, so the result becomes ``not_proven`` (and the synthesis gets
    its turn). A replay that cannot run (undeclared types, a construct with no faithful DuckDB
    reading) leaves the result as it was.
    """

    from .refutation_replay import positional
    from .smt_equivalence import SmtEquivalenceResult, SmtStatus

    counterexample = result.counterexample
    if counterexample is None or not enabled():
        return result
    dialect = kwargs.get("dialect", "bigquery")
    try:
        read = tables_read(left_sql, dialect) | tables_read(right_sql, dialect)
    except (sqlglot.errors.SqlglotError, RecursionError):
        return result
    typed = typed_schema(read, kwargs.get("schema") or {}, kwargs.get("types") or {}, kwargs.get("constraints")) if read else None
    if not typed:
        return result
    by_lower = {k.lower(): v for k, v in (kwargs.get("constraints") or {}).items()}
    try:
        with Judge(
            left_sql, right_sql, typed, dialect=dialect,
            keys={t: [list(k) for k in by_lower[t.lower()].keys] for t in typed if t.lower() in by_lower},
            not_null={t: sorted(by_lower[t.lower()].not_null) for t in typed if t.lower() in by_lower},
        ) as judge:
            verdict = judge.verdict(positional(counterexample.tables, typed))
    except Exception:  # noqa: BLE001 - no replay leaves the solver's answer alone
        return result
    if verdict is not Verdict.SAME:
        return result
    return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "the solver's counterexample returns the same rows for both queries when run")


SYNTHESIS_SECONDS = 6.0


__all__ = [
    "ASSUMPTION", "Synthesis", "as_counterexample", "check_solver_counterexample", "enabled", "refute_unproven", "shrink",
    "synthesize", "typed_schema",
]
