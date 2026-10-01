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
import logging
import sqlite3
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
    return sqlglot.transpile(sql, read="mysql", write=dialect)[0]


def random_database(tables: dict[str, Table], rng: random.Random):
    """A SQLite database with small value domains so joins and ties are common."""

    db = sqlite3.connect(":memory:")
    for table in tables.values():
        columns = ", ".join(f'"{c.name}"' for c in table.columns)
        db.execute(f'CREATE TABLE "{table.name}" ({columns})')
        keys = set()
        for _ in range(rng.choice([0, 0, 1, 2, 3, 4])):
            row = []
            for column in table.columns:
                base = column.type.split("(")[0]
                if base in {"VARCHAR", "CHAR", "TEXT"}:
                    domain = ["a", "b", "c"]
                else:
                    domain = [0, 1, 2, 3]
                value = rng.choice(domain)
                if not column.not_null and rng.random() < 0.25:
                    value = None
                row.append(value)
            if table.primary_key:
                key = tuple(row[[c.name for c in table.columns].index(k)] for k in table.primary_key)
                if key in keys:
                    continue
                keys.add(key)
            marks = ", ".join("?" * len(row))
            db.execute(f'INSERT INTO "{table.name}" VALUES ({marks})', row)
    return db


def differ(left: str, right: str, tables: dict[str, Table], trials: int = 60, seed: int = 11):
    """A database on which the queries differ as bags, else ``None``; ``False`` if SQLite rejects them."""

    rng = random.Random(seed)
    try:
        left_sqlite, right_sqlite = to_dialect(left, "sqlite"), to_dialect(right, "sqlite")
    except sqlglot.errors.SqlglotError:
        return False
    for _ in range(trials):
        db = random_database(tables, rng)
        try:
            a = Counter(db.execute(left_sqlite).fetchall())
            b = Counter(db.execute(right_sqlite).fetchall())
        except sqlite3.Error:
            return False
        if a != b:
            return (left_sqlite, right_sqlite, a, b)
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
        counter = differ(left, right, tables, trials)
        if counter is False:
            result.unchecked += 1
        elif counter is not None:
            result.wrong.append((index, counter))
    result.seconds = time.time() - start
    return result


def default_prove(left: str, right: str, tables: dict[str, Table]) -> bool:
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    schema = {t.name: [c.name for c in t.columns] for t in tables.values()}
    return prove_equivalent_algebraic(
        to_dialect(left, "bigquery"), to_dialect(right, "bigquery"), schema=schema
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
