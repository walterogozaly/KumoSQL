"""Run SQLSolver's published equivalence benchmarks through KumoSQL's provers.

Each benchmark file holds pairs of queries on consecutive lines (SQLSolver's
authors state each pair is equivalent). For every pair this reports whether the
prover proved it, and re-checks every proof by running both queries on random
SQLite databases that respect the schema's NOT NULL and key constraints.

    python tools/sqlsolver_bench.py            # all suites
    python tools/sqlsolver_bench.py calcite    # one suite

The data in tests/fixtures/sqlsolver comes from https://github.com/SJTU-IPADS/SQLSolver
(Apache-2.0, see LICENSE there).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import random
import re
import logging
import sys
import time

import sqlglot
from sqlglot import exp

logging.getLogger("sqlglot").setLevel(logging.ERROR)

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sqlsolver"
SUITES = {
    "calcite": ("calcite_pairs.txt", "calcite.schema.sql"),
    "spark": ("spark_pairs.txt", "calcite.schema.sql"),
    "tpch": ("tpch_pairs.txt", "tpch.schema.sql"),
    "tpcc": ("tpcc_pairs.txt", "tpcc.schema.sql"),
}


@dataclass
class Column:
    name: str
    type: str
    not_null: bool = False


@dataclass
class Table:
    name: str
    columns: list[Column]
    primary_key: tuple[str, ...] = ()
    unique: list[tuple[str, ...]] = field(default_factory=list)


def load_schema(path: Path) -> dict[str, Table]:
    tables = {}
    for statement in sqlglot.parse(path.read_text(encoding="utf-8"), read="mysql"):
        if not isinstance(statement, exp.Create):
            continue
        schema = statement.this
        name = schema.this.name.lower()
        table = Table(name, [])
        for item in schema.expressions:
            if isinstance(item, exp.ColumnDef):
                kinds = [k.args.get("kind") for k in item.args.get("constraints") or []]
                constraint_sql = " ".join(k.sql().upper() for k in item.args.get("constraints") or [])
                column = Column(item.name.lower(), item.args["kind"].sql(dialect="mysql").upper())
                column.not_null = "NOT NULL" in constraint_sql or "PRIMARY KEY" in constraint_sql
                if "PRIMARY KEY" in constraint_sql:
                    table.primary_key = (column.name,)
                table.columns.append(column)
            elif isinstance(item, exp.PrimaryKey):
                table.primary_key = tuple(e.name.lower() for e in item.expressions)
                for column in table.columns:
                    if column.name in table.primary_key:
                        column.not_null = True
            elif isinstance(item, exp.UniqueColumnConstraint):
                table.unique.append(tuple(e.name.lower() for e in item.this.expressions))
        tables[name] = table
    return tables


def load_pairs(path: Path) -> list[tuple[str, str]]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return list(zip(lines[0::2], lines[1::2]))


def to_dialect(sql: str, dialect: str) -> str:
    # Spark writes date('1994-01-01 +08'); the engines under test read the date part.
    sql = re.sub(r"date\(\s*'(\d{4}-\d{2}-\d{2})\s*[+-]\d{2}(?::?\d{2})?'\s*\)", r"date('\1')", sql, flags=re.I)
    return sqlglot.transpile(sql, read="mysql", write=dialect)[0]


def _duck_type(column: Column) -> str:
    base = column.type.split("(")[0]
    if base in {"VARCHAR", "CHAR", "TEXT"}:
        return "VARCHAR"
    if base in {"DECIMAL", "DOUBLE", "FLOAT", "NUMERIC"}:
        return "DOUBLE"
    if base in {"DATE", "TIMESTAMP", "DATETIME"}:
        return "DATE"
    return "BIGINT"


# Around the dates the TPC-H pairs filter on, so range predicates split the rows.
DATES = ["1993-12-31", "1994-01-01", "1994-09-01", "1994-12-15", "1995-03-21", "1996-06-30", "1997-01-01"]


def new_database(tables: dict[str, Table]):
    """An empty DuckDB database holding the schema."""

    import duckdb

    db = duckdb.connect(":memory:")
    for table in tables.values():
        columns = ", ".join(f'"{c.name}" {_duck_type(c)}' for c in table.columns)
        db.execute(f'CREATE TABLE "{table.name}" ({columns})')
    return db


def random_rows(table: Table, rng: random.Random) -> list[list]:
    """A few rows with small value domains so joins and ties are common."""

    rows, keys = [], set()
    for _ in range(rng.choice([0, 0, 1, 2, 3, 4])):
        row = []
        for column in table.columns:
            kind = _duck_type(column)
            domain = {"VARCHAR": ["a", "b", "c"], "DATE": DATES}.get(kind, [0, 1, 2, 3])
            value = rng.choice(domain)
            if not column.not_null and rng.random() < 0.25:
                value = None
            row.append(value)
        key_sets = ([table.primary_key] if table.primary_key else []) + list(table.unique)
        clash = False
        for index, key in enumerate(key_sets):
            value = (index, tuple(row[[c.name for c in table.columns].index(k)] for k in key))
            if None not in value[1] and value in keys:
                clash = True
        if clash:
            continue
        for index, key in enumerate(key_sets):
            keys.add((index, tuple(row[[c.name for c in table.columns].index(k)] for k in key)))
        rows.append(row)
    return rows


def referenced_tables(*queries: str) -> set[str]:
    names = set()
    for query in queries:
        for table in sqlglot.parse_one(query, read="mysql").find_all(exp.Table):
            names.add(table.name.lower())
    return names


def differ(left: str, right: str, tables: dict[str, Table], db, trials: int = 60, seed: int = 11):
    """A database on which the queries differ as bags, else ``None``; ``False`` if DuckDB rejects them."""

    import duckdb

    rng = random.Random(seed)
    try:
        left_sql, right_sql = to_dialect(left, "duckdb"), to_dialect(right, "duckdb")
        used = [tables[n] for n in sorted(referenced_tables(left, right)) if n in tables]
    except sqlglot.errors.SqlglotError:
        return False
    for _ in range(trials):
        try:
            for table in used:
                db.execute(f'DELETE FROM "{table.name}"')
                rows = random_rows(table, rng)
                if rows:
                    marks = ", ".join("?" * len(table.columns))
                    db.executemany(f'INSERT INTO "{table.name}" VALUES ({marks})', rows)
            a = Counter(db.execute(left_sql).fetchall())
            b = Counter(db.execute(right_sql).fetchall())
        except duckdb.Error:
            return False
        if a != b:
            return (left_sql, right_sql, a, b)
    return None


@dataclass
class SuiteResult:
    name: str
    total: int = 0
    proved: int = 0
    wrong: list = field(default_factory=list)
    unchecked: int = 0
    seconds: float = 0.0
    unproved: list = field(default_factory=list)


def run_suite(name: str, prove, limit: int | None = None, trials: int = 60) -> SuiteResult:
    pairs_file, schema_file = SUITES[name]
    tables = load_schema(FIXTURES / schema_file)
    pairs = load_pairs(FIXTURES / pairs_file)[:limit]
    result = SuiteResult(name, total=len(pairs))
    start = time.time()
    db = new_database(tables)
    for index, (left, right) in enumerate(pairs):
        try:
            proof = prove(left, right, tables)
        except Exception as error:  # a crash is a failure to prove, never a proof
            proof = False
            result.unproved.append((index, f"crash: {type(error).__name__}: {error}"))
            continue
        if not proof:
            result.unproved.append((index, "not proven"))
            continue
        result.proved += 1
        counter = differ(left, right, tables, db, trials)
        if counter is False:
            result.unchecked += 1
        elif counter is not None:
            result.wrong.append((index, counter))
    result.seconds = time.time() - start
    return result


def default_prove(left: str, right: str, tables: dict[str, Table]) -> bool:
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    from kumosql.smt_equivalence import TableConstraints

    schema = {t.name: [c.name for c in t.columns] for t in tables.values()}
    constraints = {
        t.name: TableConstraints(
            not_null=frozenset(c.name for c in t.columns if c.not_null),
            keys=tuple(k for k in ([t.primary_key] if t.primary_key else []) + list(t.unique)),
        )
        for t in tables.values()
    }
    return prove_equivalent_algebraic(
        left, right, schema=schema, constraints=constraints, compare_names=False, dialect="mysql", exact_arithmetic=True
    ).proven


def main(argv: list[str] | None = None) -> int:
    names = (argv if argv is not None else sys.argv[1:]) or list(SUITES)
    print(f"{'suite':8} {'pairs':>6} {'proved':>7} {'unknown':>8} {'wrong':>6} {'unchecked':>10} {'sec':>6}")
    bad = 0
    for name in names:
        r = run_suite(name, default_prove)
        print(f"{r.name:8} {r.total:6} {r.proved:7} {r.total - r.proved:8} {len(r.wrong):6} {r.unchecked:10} {r.seconds:6.1f}")
        bad += len(r.wrong)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
