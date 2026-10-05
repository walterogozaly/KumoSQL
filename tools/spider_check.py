"""Search for a counterexample on databases KumoSQL builds from a Spider schema (``tools/spider_data.py``).

Spider's SQLite databases are not downloadable here, so a database is generated from ``tables.json``: the
declared tables and types, the listed primary keys (unique, never NULL) and the foreign keys (every non-NULL
reference points at an existing row; a column a foreign key points at is unique). Two queries differ on such a
database when SQLite returns different results for them, compared as Spider does: as lists when the first
query ends in ``ORDER BY``, as bags otherwise. The search is ``tools/llm_sql_solver_bench.py``'s (random
databases, then the targeted suite, then the z3 bounded check, each replayed in SQLite) with two more guards:

* an order-dependent comparison is only made on a database where neither query's ``ORDER BY`` has ties, so a
  tie the join order decides can never count as a difference;
* a database a search proposes is checked against the schema rules above and skipped when it breaks one.

A counterexample can be shrunk row by row to a small database for reporting.
"""

from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path

from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm_sql_solver_bench as solver
import spider_data

CAUSE_TRIALS = 300


def case(schema: spider_data.Schema, sql1: str = "", sql2: str = "", index: int = 0) -> solver.Case:
    """The LLM-SQL-Solver harness's case shape, whose SQLite database search this reuses."""

    return solver.Case("spider", index, schema.database, sql1, sql2, "equivalent", schema.tables, schema.keys, schema.foreign)


def valid(schema: spider_data.Schema, database: dict[str, list[list]]) -> bool:
    """Listed keys are unique and not NULL, referenced columns unique, and every non-NULL reference is in its parent."""

    for table, key in schema.keys.items():
        positions = [list(schema.tables[table]).index(c) for c in key]
        values = [tuple(row[p] for p in positions) for row in database.get(table, [])]
        if any(None in v for v in values) or len(values) != len(set(values)):
            return False
    for table, columns in schema.unique.items():
        for column in columns:
            position = list(schema.tables[table]).index(column)
            values = [row[position] for row in database.get(table, []) if row[position] is not None]
            if len(values) != len(set(values)):
                return False
    for child, column, parent, parent_column in schema.foreign:
        parents = {row[list(schema.tables[parent]).index(parent_column)] for row in database.get(parent, [])}
        position = list(schema.tables[child]).index(column)
        if any(row[position] is not None and row[position] not in parents for row in database.get(child, [])):
            return False
    return True


def differs(schema: spider_data.Schema, sql1: str, sql2: str, **options) -> str:
    """"differs", "agree" or "error" on schema-valid SQLite databases (see the module docstring)."""

    return solver.differs_on_random_databases(
        case(schema, sql1, sql2), sql1, sql2, tie_safe=True, unique=schema.unique, valid=lambda database: valid(schema, database), **options,
    )


def refute(schema: spider_data.Schema, sql1: str, sql2: str, witness: dict | None = None) -> str:
    """"differs" (random), "targeted", "bounded", "agree" or "error"; the database goes into ``witness``."""

    result = differs(schema, sql1, sql2, witness=witness)
    if result != "agree" or solver.ordered(sql1) or solver.has_any_limit(sql1, sql2):
        return result  # the targeted and bounded searches compare bags, without the storage-order check
    for how, search in (("targeted", _targeted), ("bounded", _bounded)):
        try:
            database = search(schema, sql1, sql2)
        except Exception:  # a search that fails finds nothing
            database = None
        if database is not None and valid(schema, database) and differs(schema, sql1, sql2, databases=[database]) == "differs":
            if witness is not None:
                witness.update(database)
            return how
    return "agree"


def _rows(schema: spider_data.Schema, tables: dict) -> dict[str, list[list]]:
    """A found database ({table: (column names, rows)}) as rows in the schema's column order."""

    out = {}
    for name, (columns, rows) in tables.items():
        table = name.lower().split(".")[-1].strip("`\"")
        if table not in schema.tables:
            continue
        positions = [c.lower() for c in columns]
        out[table] = [[row[positions.index(c)] if c in positions else None for c in schema.tables[table]] for row in rows]
    return out


def _unique_sets(schema: spider_data.Schema, table: str) -> tuple[tuple[str, ...], ...]:
    return tuple(k for k in ((schema.keys.get(table) or ()), *((c,) for c in schema.unique.get(table, ()))) if k)


def _bq_type(kind: str) -> str:
    import sqliq_bench

    return "FLOAT64" if any(w in kind for w in ("REAL", "FLOA", "DOUB", "DEC")) else sqliq_bench._BQ_TYPES[sqliq_bench._kind(kind)]


def _targeted(schema: spider_data.Schema, sql1: str, sql2: str) -> dict | None:
    from kumosql.refute import find_targeted_difference
    from kumosql.result_equivalence import DataRules

    types = {t: {c: _bq_type(k) for c, k in cols.items()} for t, cols in schema.tables.items()}
    rules = {t: DataRules(frozenset(schema.keys.get(t, ())), _unique_sets(schema, t)) for t in schema.tables if _unique_sets(schema, t)}
    found = find_targeted_difference(
        sql1, sql2, types, rules, foreign_keys=schema.foreign, engine="sqlite", dialect="sqlite", ordered=False, budget=20.0,
    )
    if found is None:
        return None
    return _rows(schema, {name: ([c for c, _ in table.columns], table.rows) for name, table in found.dataset.tables.items()})


def _bounded(schema: spider_data.Schema, sql1: str, sql2: str) -> dict | None:
    import sqliq_bench
    from kumosql import bounded_equivalence as be

    tables = {}
    for table, columns in schema.tables.items():
        key = set(schema.keys.get(table, ()))
        cols = [be.BColumn(c, _bq_type(k), c in key) for c, k in columns.items()]
        tables[table] = be.BTable(table, cols, list(_unique_sets(schema, table)))
    for child, child_column, parent, parent_column in schema.foreign:
        tables[child].foreign_keys.append(((child_column,), parent, (parent_column,)))
    bounded = be.BoundedSchema(tables)
    result = be.check_bounded(
        sql1, sql2, bounded, rows=sqliq_bench.BOUNDED_ROWS, dialect="sqlite", budget_s=30, timeout_ms=5000,
        replay=be.SQLiteReplay(bounded, sql1, sql2),
    )
    if result.status is not be.BoundedStatus.DIFFERENT or not result.counterexample:
        return None
    return _rows(schema, {name: ([c.name for c in tables[name.lower()].columns] if name.lower() in tables else [], rows) for name, rows in result.counterexample.items()})


def shrink(schema: spider_data.Schema, sql1: str, sql2: str, database: dict[str, list[list]]) -> dict[str, list[list]]:
    """Drop rows one at a time while the database stays valid and the two results still differ."""

    current = {t: [list(r) for r in rows] for t, rows in database.items()}
    progress = True
    while progress:
        progress = False
        for table in list(current):
            for index in reversed(range(len(current[table]))):
                trial = {t: (rows[:index] + rows[index + 1:] if t == table else rows) for t, rows in current.items()}
                if valid(schema, trial) and differs(schema, sql1, sql2, databases=[trial]) == "differs":
                    current, progress = trial, True
    return {t: rows for t, rows in current.items() if rows}


# -- explaining a dispute -------------------------------------------------------------


def pool(
    schema: spider_data.Schema, sql1: str, sql2: str, *, nulls: bool = True, nonempty: bool = False, count: int = CAUSE_TRIALS, seed: int = 11,
) -> list[dict]:
    """Random schema-valid databases for explaining a dispute.

    Without ``nulls`` no cell is NULL (rows that would need one are left out). With ``nonempty`` every table has a
    row and ``sql1`` returns at least one row.
    """

    import sqlite3

    import sqliq_bench

    pair = sqliq_bench.Pair(0, sql1, sql2, schema.tables, schema.keys, "no", schema.foreign)
    domains = sqliq_bench.make_domains(pair, *sqliq_bench.mentioned_values(sql1, sql2))
    rng = random.Random(seed)
    out: list[dict] = []
    connection = sqlite3.connect(":memory:")
    try:
        for table, columns in schema.tables.items():
            connection.execute(f'CREATE TABLE "{table}" ({", ".join(f"{chr(34)}{c}{chr(34)} {k}" for c, k in columns.items())})')
        for _ in range(count * (20 if nonempty else 1)):
            if len(out) >= count:
                break
            made: dict[str, list[list]] = {}
            for table in sqliq_bench.table_order(pair):
                rows = sqliq_bench.random_rows(pair, table, domains, rng, made, null_rate=None if nulls else 0.0, unique=schema.unique.get(table, ()))
                made[table] = rows if nulls else [r for r in rows if None not in r]  # a reference into an empty parent table is NULL
            if nonempty:
                if not all(made[t] for t in schema.tables):
                    continue
                for table, columns in schema.tables.items():
                    connection.execute(f'DELETE FROM "{table}"')
                    connection.executemany(f'INSERT INTO "{table}" VALUES ({", ".join("?" * len(columns))})', made[table])
                try:
                    if not sqliq_bench.run_query(connection, sql1):
                        continue
                except sqlite3.Error:
                    continue
            out.append(made)
    finally:
        connection.close()
    return out


def without_nulls(schema: spider_data.Schema, database: dict[str, list[list]]) -> dict[str, list[list]] | None:
    """The database with every NULL replaced, if the result is valid.

    A NULL becomes a value no other cell holds; a NULL reference points at a row of its parent table (a new
    row of fresh values when the parent is empty).
    """

    fresh = itertools.count(1)
    parents = {(child, column): (parent, parent_column) for child, column, parent, parent_column in schema.foreign}
    out = {t: [list(r) for r in database.get(t, [])] for t in schema.tables}

    def new_value(table: str, column: str):
        return 90_000 + next(fresh) if schema.tables[table][column] == "INTEGER" else f"fresh{next(fresh)}"

    def referenced(table: str, column: str, depth: int):
        position = list(schema.tables[table]).index(column)
        for row in out[table]:
            if row[position] is not None:
                return row[position]
        if depth > 4:
            return None
        row = [referenced(*parents[(table, c)], depth + 1) if (table, c) in parents else new_value(table, c) for c in schema.tables[table]]
        out[table].append(row)
        return row[position]

    for table, rows in out.items():
        columns = list(schema.tables[table])
        for row in rows:
            for position, column in enumerate(columns):
                if row[position] is None:
                    row[position] = referenced(*parents[(table, column)], 0) if (table, column) in parents else new_value(table, column)
    out = {t: rows for t, rows in out.items() if rows}
    if any(None in row for rows in out.values() for row in rows):
        return None
    return out if valid(schema, out) else None


def column_orders(sql: str) -> list[str]:
    """The query with its output columns in every order (TestSuiteEval compares results up to column order)."""

    tree = solver._tree(sql)
    top = solver._top(tree)
    if not isinstance(top, exp.Select) or not 1 < len(top.expressions) <= 4 or any(
        isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in top.expressions
    ):
        return [sql]
    out = []
    for order in itertools.permutations(top.expressions):
        copy = tree.copy()
        solver._top(copy).set("expressions", [e.copy() for e in order])
        out.append(copy.sql(dialect="sqlite"))
    return out
