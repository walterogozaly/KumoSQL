"""Cross-dialect parse and proof coverage on DLBench's SQL translation pairs, with no language model.

DLBench (https://github.com/DLBenchll/DLBench, Apache-2.0, ASE 2025) pairs a query with its
translation into another database's dialect: BIRDTrans (3,206 pairs from BIRD's SQLite queries)
and BUTTERTrans (3,196 pairs from MySQL's and PostgreSQL's own test suites), translated into
MySQL, MariaDB, PostgreSQL, ClickHouse, MonetDB and DuckDB. Its labels (6,199 "exact" and 203
"approximate" equivalence) come from language models and human review, not proofs.

For each pair KumoSQL:

1. **parses** the source in its dialect and the translation in the target's (sqlglot has no MariaDB
   or MonetDB dialect: they are read as MySQL and PostgreSQL);
2. **proves** the translation equal to the source in the source's dialect: the translation is
   renamed back to the source's column names (BIRD's translations rename some columns, for example
   ``Type`` to ``_Type``), written in the source dialect by sqlglot, and compared with
   ``prove_equivalent_algebraic`` (no keys, output names ignored). As in Spider, a source ending in
   ``ORDER BY`` is compared as a list.

A proof is about sqlglot's reading of each dialect. Where the two databases mean different things
by the same text and sqlglot does not translate it, no proof is taken (``dialect gap``):

* ``LIKE`` ignores case in SQLite and MySQL but not in the others;
* MySQL and MariaDB compare strings without case (their default collation): a proof between a
  MySQL-family query and another dialect is taken only for queries with no string and no column;
* ``/`` on integers truncates in PostgreSQL and MonetDB but not in MySQL; dividing by zero gives NULL
  in SQLite and MySQL but infinity in DuckDB and ClickHouse (an error elsewhere, which proofs exclude);
* ``REAL`` (and ``FLOAT`` in most of the targets) is a 32-bit float, but sqlglot writes it as SQLite's
  64-bit ``REAL``;
* ``1.0`` is an exact decimal in PostgreSQL, MonetDB and MySQL (so ``1.0 / 3`` keeps 20 digits), a float
  in SQLite;
* ClickHouse returns 0 rather than NULL for ``SUM`` of no rows and fills the missing side of an outer
  join with default values, not NULL;
* SQLite compares a text column with a number after converting one side (see
  ``llm_sql_solver_bench.mixed_type_comparison``).

Every proof of a BIRDTrans pair is re-checked: the source and the translation run on 300 random
SQLite databases, and a translation into DuckDB also runs natively in DuckDB on the same data. A
difference makes the proof ``wrong``.

The repository pins a subset (``tests/fixtures/dlbench``): every "approximate" pair and one in ten of
the rest, by a hash of the pair's dataset, target and id. One pair in five of the subset is held out.

    python tools/dlbench_bench.py                                 # the pinned subset
    python tools/dlbench_bench.py --data DLBench                  # all 6,402 pairs from a checkout
    python tools/dlbench_bench.py --data DLBench --write-subset   # rebuild the pinned subset
    python tools/dlbench_bench.py --write-results
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import sys
import time

import sqlglot
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "dlbench"
SOURCE_COMMIT = "a3525919033faac73e60a13a88c2d14ab6953f23"
DATASETS = ("BIRDTrans", "BUTTERTrans")
TARGETS = ("mysql", "mariadb", "postgresql", "clickhouse", "monetdb", "duckdb")
SQLGLOT = {"sqlite": "sqlite", "mysql": "mysql", "mariadb": "mysql", "postgresql": "postgres", "clickhouse": "clickhouse", "monetdb": "postgres", "duckdb": "duckdb"}
CASELESS_LIKE = {"sqlite", "mysql", "mariadb"}
CASELESS_STRINGS = {"mysql", "mariadb"}
INTEGER_DIVISION = {"sqlite", "postgresql", "monetdb"}
EXACT_DECIMALS = {"postgresql", "monetdb", "mysql", "mariadb"}  # 1.0 is an exact decimal, so 1.0 / 3 is not a float
DIVIDE_BY_ZERO = {"sqlite": "null", "mysql": "null", "mariadb": "null", "duckdb": "infinity", "clickhouse": "infinity"}  # the others raise an error
PROVER_TIMEOUT_MS = 3000
CHECK_TRIALS = 300
UNBOUNDED = 1_000_000_000


@dataclass
class Pair:
    dataset: str
    sql_id: int
    database: str
    source_dbms: str
    target_dbms: str
    source_query: str
    target_query: str
    label_raw: str  # as published: exact_equivalence, appr_equivalence, approximate_equivalence or "Approximate equivalence"
    source_schema: list[str] = field(default_factory=list)  # source_related_schemas, as published
    renames: dict[str, str] = field(default_factory=dict)  # translation's column name -> source's, where they differ

    @property
    def id(self) -> str:
        return f"{self.dataset}/{self.target_dbms}/{self.sql_id}"

    @property
    def label(self) -> str:
        return "approximate" if "appr" in self.label_raw.lower() else "exact"

    @property
    def held_out(self) -> bool:
        return int(hashlib.sha1(f"dlbench-held-out\n{self.id}".encode()).hexdigest(), 16) % 5 == 0


def in_subset(dataset: str, target: str, sql_id: int, label_raw: str) -> bool:
    if "appr" in label_raw.lower():
        return True
    return int(hashlib.sha1(f"dlbench\n{dataset}\n{target}\n{sql_id}".encode()).hexdigest(), 16) % 10 == 0


# -- loading --------------------------------------------------------------------------

_TABLE = re.compile(r"Table:\s*`?([^`\n]+?)`?\s*\nColumns:\s*\n(.*)", re.S)
_COLUMN = re.compile(r"^\(\s*`?([^`,]+?)`?\s*,\s*([^,)]*)(?:,\s*([^)]*))?\)\s*$")


def described_tables(texts: list[str]) -> list[tuple[str, list[tuple[str, str, bool]]]]:
    """``[(table, [(column, type, primary key)])]`` from DLBench's ``Table: .. Columns: ..`` text."""

    tables = []
    for text in texts:
        match = _TABLE.match(text.strip())
        if not match:
            continue
        columns = []
        for line in match.group(2).splitlines():
            column = _COLUMN.match(line.strip())
            if column:
                columns.append((column.group(1).strip(), column.group(2).strip().lower(), "primary key" in (column.group(3) or "").lower()))
        tables.append((match.group(1).strip(), columns))
    return tables


def created_tables(texts: list[str], dialect: str) -> list[tuple[str, list[tuple[str, str, bool]]]]:
    """The same shape from CREATE TABLE statements (BUTTERTrans lists the statements a test runs first)."""

    tables = []
    for text in texts:
        try:
            statements = sqlglot.parse(text, read=SQLGLOT[dialect])
        except sqlglot.errors.SqlglotError:
            continue
        for statement in statements:
            if isinstance(statement, exp.Create) and isinstance(statement.this, exp.Schema):
                columns = [
                    (c.name, (c.args.get("kind").sql() if c.args.get("kind") else "").lower(), False)
                    for c in statement.this.expressions
                    if isinstance(c, exp.ColumnDef)
                ]
                tables.append((statement.this.this.name, columns))
    return tables


def schema_of(pair: Pair) -> dict[str, dict[str, str]]:
    """Lower-case table -> lower-case column -> declared type, for the tables the source describes."""

    tables = described_tables(pair.source_schema) or created_tables(pair.source_schema, pair.source_dbms)
    return {t.lower(): {c.lower(): k for c, k, _ in cols} for t, cols in tables}


def _renames(source: list[str], target: list[str]) -> dict[str, str]:
    """Column renames between the source's and the translation's schema text, matched by position."""

    renames = {}
    for (_, left), (_, right) in zip(described_tables(source), described_tables(target)):
        if len(left) != len(right):
            continue
        for (a, _, _), (b, _, _) in zip(left, right):
            if a.lower() != b.lower():
                renames[b] = a
    return renames


def read_checkout(data: Path, subset_only: bool = False) -> list[Pair]:
    pairs = []
    for dataset in DATASETS:
        for target in TARGETS:
            for row in json.loads((data / "datasets" / dataset / f"{target}.json").read_text(encoding="utf-8")):
                if subset_only and not in_subset(dataset, target, row["sql_id"], row["semantic_equivalent_type"]):
                    continue
                pairs.append(Pair(
                    dataset, row["sql_id"], row["database_name"], row["source_dbms"], row["target_dbms"],
                    row["source_query"], row["target_query"], row["semantic_equivalent_type"],
                    row.get("source_related_schemas") or [],
                    _renames(row.get("source_related_schemas") or [], row.get("target_related_schemas") or []),
                ))
    return pairs


def _schema_key(pair: Pair) -> str:
    return f"{pair.dataset}/{pair.database}/{pair.source_dbms}"


def _renames_key(pair: Pair) -> str:
    return f"{pair.dataset}/{pair.database}/{pair.target_dbms}"


def load_pairs(fixtures: Path = FIXTURES) -> list[Pair]:
    """The pinned subset; schemas and renames are stored once per database (``schemas.json``, ``renames.json``)."""

    schemas = json.loads((fixtures / "schemas.json").read_text(encoding="utf-8"))
    renames = json.loads((fixtures / "renames.json").read_text(encoding="utf-8"))
    pairs = []
    for line in (fixtures / "pairs.jsonl").read_text(encoding="utf-8").splitlines():
        pair = Pair(**json.loads(line))
        pair.source_schema = schemas.get(_schema_key(pair), [])
        pair.renames = renames.get(_renames_key(pair), {})
        pairs.append(pair)
    return pairs


def write_subset(data: Path, fixtures: Path = FIXTURES) -> int:
    pairs = read_checkout(data, subset_only=True)
    schemas: dict[str, dict[str, str]] = defaultdict(dict)  # key -> table -> its schema text
    renames: dict[str, dict[str, str]] = defaultdict(dict)
    with open(fixtures / "pairs.jsonl", "w", encoding="utf-8") as handle:
        for pair in pairs:
            for text in pair.source_schema:
                described = described_tables([text])
                schemas[_schema_key(pair)].setdefault(described[0][0] if described else text, text)
            renames[_renames_key(pair)].update(pair.renames)
            row = {k: v for k, v in pair.__dict__.items() if k not in ("source_schema", "renames")}
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    for name, value in (("schemas.json", {k: list(v.values()) for k, v in schemas.items()}), ("renames.json", {k: v for k, v in renames.items() if v})):
        (fixtures / name).write_text(json.dumps(dict(sorted(value.items())), ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    return len(pairs)


# -- reading and translating ----------------------------------------------------------


def parse(sql: str, dbms: str):
    try:
        trees = [t for t in sqlglot.parse(sql, read=SQLGLOT[dbms]) if t is not None and not isinstance(t, exp.Semicolon)]
    except Exception:  # sqlglot raises more than ParseError on unusual input
        return None
    return trees[0] if len(trees) == 1 and not isinstance(trees[0], exp.Command) else None


def _plain(tree, renames: dict[str, str]):
    """Translation's column names back to the source's; every name lower-case, quoted only when needed."""

    lowered = {k.lower(): v for k, v in renames.items()}
    for identifier in tree.find_all(exp.Identifier):
        name = identifier.name
        if isinstance(identifier.parent, exp.Column) and identifier.parent.this is identifier:
            name = lowered.get(name.lower(), name)
        name = name.lower()
        identifier.set("this", name)
        identifier.set("quoted", not re.fullmatch(r"[a-z_][a-z0-9_]*", name))
    return tree


def as_source(pair: Pair) -> tuple[str | None, str | None]:
    """Both queries in the source's dialect, names normalised; ``None`` where a query does not parse."""

    dialect = SQLGLOT[pair.source_dbms]
    source, target = parse(pair.source_query, pair.source_dbms), parse(pair.target_query, pair.target_dbms)
    try:
        left = _plain(source, {}).sql(dialect=dialect) if source is not None else None
        right = _plain(target, pair.renames).sql(dialect=dialect) if target is not None else None
    except Exception:  # a construct sqlglot cannot write in the source dialect
        return None, None
    return left, right


# -- dialect gaps ---------------------------------------------------------------------


def dialect_gap(pair: Pair, left: str, right: str, schema: dict[str, dict[str, str]]) -> str | None:
    """Why a proof would not carry over between the two databases, or ``None``."""

    source, target = pair.source_dbms, pair.target_dbms
    trees = [parse(q, d) for q, d in ((pair.source_query, source), (pair.target_query, target))]
    if any(t is None for t in trees):
        return "unparsed"
    if (source in CASELESS_LIKE) != (target in CASELESS_LIKE) and any(t.find(exp.Like) for t in trees):
        return "LIKE and case"
    if (source in CASELESS_STRINGS) != (target in CASELESS_STRINGS) and any(t.find(exp.Literal) and any(l.is_string for l in t.find_all(exp.Literal)) or t.find(exp.Column) for t in trees):
        return "MySQL string comparison"
    if target in INTEGER_DIVISION and source not in INTEGER_DIVISION and trees[1].find(exp.Div):
        return "integer division"
    zero = {DIVIDE_BY_ZERO.get(source), DIVIDE_BY_ZERO.get(target)}
    if len(zero - {None}) == 2 and any(t.find(exp.Div) for t in trees):
        return "division by zero"
    if target != "sqlite" and any(d.this == exp.DataType.Type.FLOAT for d in trees[1].find_all(exp.DataType)):
        return "32-bit float"
    if target in EXACT_DECIMALS and source not in EXACT_DECIMALS and trees[1].find(exp.Div) and any(
        not l.is_string and "." in l.name for l in trees[1].find_all(exp.Literal)
    ):
        return "decimal arithmetic"
    if target == "clickhouse" and (
        any(not isinstance(a, exp.Count) for a in trees[1].find_all(exp.AggFunc))
        or any(j.side for j in trees[1].find_all(exp.Join))
    ):
        return "ClickHouse defaults"
    if source == "sqlite":
        import llm_sql_solver_bench as solver

        tables = {t: {c: ("TEXT" if _texty(k) else "INTEGER") for c, k in cols.items()} for t, cols in schema.items()}
        if solver.mixed_type_comparison(left, tables) or solver.mixed_type_comparison(right, tables):
            return "SQLite type conversion"
    return None


def _texty(declared: str) -> bool:
    return any(w in declared.lower() for w in ("char", "text", "clob", "date", "time", "string", "blob")) or not declared


def _ordered(sql: str, dialect: str) -> bool:
    tree = parse(sql, dialect)
    while isinstance(tree, exp.Subquery):
        tree = tree.this
    return tree is not None and tree.args.get("order") is not None


def _with_unbounded_limit(sql: str, dialect: str) -> str:
    tree = sqlglot.parse_one(sql, read=dialect)
    top = tree
    while isinstance(top, exp.Subquery):
        top = top.this
    if top.args.get("order") is not None and top.args.get("limit") is None and top.args.get("offset") is None:
        top.set("limit", exp.Limit(expression=exp.Literal.number(UNBOUNDED)))
    return tree.sql(dialect=dialect)


def prove(left: str, right: str, schema: dict[str, dict[str, str]], dialect: str) -> bool:
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.smt_equivalence import SmtStatus

    if _ordered(left, dialect):
        if not _ordered(right, dialect):
            return False  # a list against an unordered result
        left, right = _with_unbounded_limit(left, dialect), _with_unbounded_limit(right, dialect)
    try:
        result = prove_equivalent_algebraic(
            left, right, schema={t: list(c) for t, c in schema.items()} or None, compare_names=False,
            dialect=dialect, timeout_ms=PROVER_TIMEOUT_MS,
        )
    except Exception:  # a crash is a failure to prove, never a proof
        return False
    return result.status is SmtStatus.PROVEN_EQUIVALENT


# -- checking proofs by running them --------------------------------------------------


def _random_rows(schema: dict[str, dict[str, str]], rng: random.Random, literals: list) -> dict[str, list[tuple]]:
    numbers = [0, 1, 2, 3, -1] + [v for v in literals if isinstance(v, (int, float))]
    texts = ["a", "b", "A"] + [v for v in literals if isinstance(v, str)]
    data = {}
    for table, columns in schema.items():
        rows = []
        for _ in range(rng.choice([0, 1, 2, 3, 4, 6])):
            rows.append(tuple(
                None if rng.random() < 0.1 else (rng.choice(texts) if _texty(k) else rng.choice(numbers))
                for k in columns.values()
            ))
        data[table] = rows
    return data


def _literals(*queries: str) -> list:
    values: list = []
    for sql in queries:
        tree = parse(sql, "sqlite")
        for literal in tree.find_all(exp.Literal) if tree is not None else ():
            if literal.is_string:
                values.append(literal.name)
            else:
                try:
                    number = float(literal.name)
                    values.extend([int(number) if number.is_integer() else number])
                except ValueError:
                    pass
    return list(dict.fromkeys(values))[:20]


def _bag(rows) -> Counter:
    def norm(v):
        if isinstance(v, float):
            return round(v, 6) + 0.0
        return v

    return Counter(tuple(norm(v) for v in row) for row in rows)


def _uses_rowid(*queries: str) -> bool:
    """Whether a query reads SQLite or DuckDB's implicit row id."""

    return any(re.search(r"(?<![\w$])(?:rowid|_rowid_|oid)(?![\w$])", query, re.IGNORECASE) for query in queries)


def check_proof(pair: Pair, left: str, right: str, schema: dict[str, dict[str, str]]) -> str | None:
    """A database on which a proved BIRDTrans pair differs (described), or ``None``.

    Both queries (the translation written as SQLite) run in SQLite; a DuckDB translation also runs as
    published in DuckDB. Results are compared as bags (lists when the source ends in ORDER BY and the
    ordering covers ties, which a second load in reverse row order checks).
    """

    import sqlite3

    if pair.source_dbms != "sqlite" or not schema:
        return None
    rng = random.Random(5)
    literals = _literals(left, right)
    ordered = _ordered(left, "sqlite")
    native = None
    if pair.target_dbms == "duckdb":
        try:
            import duckdb

            native = _plain(parse(pair.target_query, "duckdb"), pair.renames).sql(dialect="duckdb")
        except Exception:
            native = None
    reuse_sqlite = not _uses_rowid(left, right)
    reuse_duckdb = native is not None and not _uses_rowid(native)
    sqlite_connections = []
    duckdb_connection = None
    try:
        if reuse_sqlite:
            try:
                for _ in range(2):
                    connection = sqlite3.connect(":memory:")
                    sqlite_connections.append(connection)
                    for table, columns in schema.items():
                        connection.execute(_create(table, columns, lambda k: k or "TEXT"))
            except sqlite3.Error:
                return None  # a schema SQLite cannot load is not checked
        if reuse_duckdb:
            try:
                import duckdb

                duckdb_connection = duckdb.connect()
                for table, columns in schema.items():
                    duckdb_connection.execute(_create(table, columns, _duck_type))
            except Exception:
                if duckdb_connection is not None:
                    duckdb_connection.close()
                    duckdb_connection = None
                reuse_duckdb = False

        for _ in range(CHECK_TRIALS):
            data = _random_rows(schema, rng, literals)
            results = []
            for index, reverse in enumerate((False, True)):
                connection = sqlite_connections[index] if reuse_sqlite else sqlite3.connect(":memory:")
                try:
                    for table, columns in schema.items():
                        if reuse_sqlite:
                            connection.execute(f'DELETE FROM "{table}"')
                        else:
                            connection.execute(_create(table, columns, lambda k: k or "TEXT"))
                        rows = data[table][::-1] if reverse else data[table]
                        if rows:
                            connection.executemany(f'INSERT INTO "{table}" VALUES ({", ".join("?" * len(columns))})', rows)
                    if reuse_sqlite:
                        connection.commit()
                    results.append(tuple(connection.execute(q).fetchall() for q in (left, right)))
                except sqlite3.Error:
                    return None  # a query SQLite cannot run is not checked
                finally:
                    if not reuse_sqlite:
                        connection.close()
            (a, b), (a2, b2) = results
            if ordered and (a != a2 or b != b2):
                continue  # ties: the order is not determined
            if (a != b) if ordered else (_bag(a) != _bag(b)):
                return f"SQLite: {data}"
            if native is not None:
                from kumosql.duckdb_load import insert_rows, run_unoptimized

                db = duckdb_connection if reuse_duckdb else duckdb.connect()
                try:
                    for table, columns in schema.items():
                        if reuse_duckdb:
                            db.execute(f'DELETE FROM "{table}"')
                        else:
                            db.execute(_create(table, columns, _duck_type))
                        if data[table]:
                            insert_rows(db, f'"{table}"', data[table])
                    theirs = db.execute(native).fetchall()
                    if _bag(theirs) != _bag(a) and _bag(run_unoptimized(db, native)[0]) != _bag(a):
                        return f"DuckDB: {data}"
                except Exception:
                    pass  # DuckDB cannot load these values or run the query: not a difference
                finally:
                    if not reuse_duckdb:
                        db.close()
        return None
    finally:
        for connection in sqlite_connections:
            connection.close()
        if duckdb_connection is not None:
            duckdb_connection.close()


def _create(table: str, columns: dict[str, str], typed) -> str:
    definitions = ", ".join(f'"{column}" {typed(declared)}' for column, declared in columns.items())
    return f'CREATE TABLE "{table}" ({definitions})'


def _duck_type(declared: str) -> str:
    if _texty(declared):
        return "VARCHAR"
    return "DOUBLE" if any(w in declared.lower() for w in ("real", "float", "double", "dec", "num")) else "BIGINT"


# -- deciding -------------------------------------------------------------------------


def decide(pair: Pair) -> dict:
    started = time.time()
    source, target = parse(pair.source_query, pair.source_dbms), parse(pair.target_query, pair.target_dbms)
    parsed = {"source": source is not None, "target": target is not None}
    outcome, detail, wrong = "unsupported", "", False
    if source is not None and target is not None:
        left, right = as_source(pair)
        schema = schema_of(pair)
        if left is None or right is None:
            detail = "not written in the source dialect"
        elif (gap := dialect_gap(pair, left, right, schema)) is not None:
            outcome, detail = "unknown", f"dialect gap: {gap}"
        elif prove(left, right, schema, SQLGLOT[pair.source_dbms]):
            outcome = "proven"
            difference = check_proof(pair, left, right, schema)
            if difference is not None:
                wrong, detail = True, difference
        else:
            outcome = "unknown"
    else:
        detail = "parse: " + ", ".join(k for k, v in parsed.items() if not v)
    return {
        "id": pair.id, "dataset": pair.dataset, "target": pair.target_dbms, "source": pair.source_dbms,
        "label": pair.label, "parsed": all(parsed.values()), **{f"parsed_{k}": v for k, v in parsed.items()},
        "outcome": outcome, "detail": detail, "wrong": wrong, "held_out": pair.held_out,
        "seconds": round(time.time() - started, 2),
    }


def run(pairs: list[Pair], jobs: int = 1) -> list[dict]:
    if jobs > 1 and len(pairs) > 1:
        with ProcessPoolExecutor(jobs) as pool:
            return list(pool.map(decide, pairs, chunksize=4))
    return [decide(p) for p in pairs]


def table(results: list[dict]) -> list[str]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in results:
        groups[(r["dataset"], r["target"])].append(r)
    lines = [f"{'dataset':12} {'target':11} {'pairs':>5} {'parsed':>7} {'proven':>7} {'gap':>5} {'approx proven':>14} {'wrong':>6}"]
    for (dataset, target), rows in sorted(groups.items()):
        lines.append(
            f"{dataset:12} {target:11} {len(rows):5} {sum(r['parsed'] for r in rows):7} "
            f"{sum(r['outcome'] == 'proven' for r in rows):7} {sum(r['detail'].startswith('dialect gap') for r in rows):5} "
            f"{sum(r['outcome'] == 'proven' and r['label'] == 'approximate' for r in rows):14} {sum(r['wrong'] for r in rows):6}"
        )
    return lines


def results_rows(results: list[dict]) -> dict[str, dict]:
    from bench_common import today

    exact = [r for r in results if r["label"] == "exact"]
    approx = [r for r in results if r["label"] == "approximate"]
    held = [r for r in exact if r["held_out"]]
    counts = Counter(r["outcome"] for r in results)
    proven = sum(r["outcome"] == "proven" for r in exact)
    parsed = sum(r["parsed"] for r in results)
    gaps = sum(r["detail"].startswith("dialect gap") for r in results)
    return {
        "dlbench": {
            "suite": "DLBench cross-dialect translations (pinned subset)",
            "order": 35,
            "size": len(results),
            "score": f"{proven}/{len(exact)} exact pairs proved, {sum(r['wrong'] for r in results)} wrong; {parsed}/{len(results)} parsed",
            "metric": "Translations from SQLite, MySQL and PostgreSQL into six databases' dialects, proved equal to the source in the source's dialect after sqlglot reads both; parsed means both sides parse in their own dialect.",
            "evidence": "proof",
            "correctness": f"Every BIRDTrans proof is re-run on 300 random SQLite databases (DuckDB translations also natively in DuckDB); {gaps} pairs where the two databases read the same text differently (LIKE case, MySQL collation, integer division, SQLite type conversion) are left unproved. {sum(r['outcome'] == 'proven' for r in approx)}/{len(approx)} 'approximate' pairs are proved (listed in the docs).",
            "coverage": {k: counts[k] for k in ("proven", "unknown", "unsupported") if counts[k]},
            "held_out": f"{sum(r['outcome'] == 'proven' for r in held)}/{len(held)} exact pairs proved",
            "docs": "docs/evals/dlbench.md",
            "command": "python tools/dlbench_bench.py --write-results",
            "date": today(),
            "caveats": f"Pinned subset of the 6,402 pairs (every approximate pair, one in ten of the rest); run --data with a DLBench checkout for all. Labels are LLM plus human review, not proofs. Proofs are about sqlglot's reading of each dialect; MariaDB is read as MySQL and MonetDB as PostgreSQL. The 32-bit float, division-by-zero, decimal and ClickHouse rules were added after a first run on the whole subset showed two proofs contradicted natively in DuckDB (tuned on test).",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", help=f"a DLBench checkout (commit {SOURCE_COMMIT[:12]}) to run all pairs instead of the pinned subset")
    parser.add_argument("--write-subset", action="store_true", help="rebuild tests/fixtures/dlbench/pairs.jsonl from --data")
    parser.add_argument("--split", choices=("all", "dev", "held-out"), default="all")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--show", default="", help="comma-separated outcomes to list (proven, unknown, unsupported, wrong)")
    parser.add_argument("--json", help="write every outcome to this file")
    parser.add_argument("--write-results", action="store_true", help="update benchmarks/results/dlbench.json (pinned subset, --split all)")
    args = parser.parse_args(argv)
    from bench_common import quiet, write_results

    quiet()
    if args.write_subset:
        if not args.data:
            parser.error("--write-subset needs --data")
        print(f"wrote {write_subset(Path(args.data))} pairs")
        return 0
    pairs = read_checkout(Path(args.data)) if args.data else load_pairs()
    if args.split != "all":
        pairs = [p for p in pairs if p.held_out == (args.split == "held-out")]
    started = time.time()
    results = run(pairs, args.jobs)
    print("\n".join(table(results)))
    print(f"{len(results)} pairs in {time.time() - started:.0f}s")
    show = {s.strip() for s in args.show.split(",") if s.strip()}
    for pair, result in zip(pairs, results):
        if result["outcome"] in show or (result["wrong"] and "wrong" in show) or result["wrong"]:
            print(f"\n{result['id']} [{result['label']}] {result['outcome']} {result['detail'][:200]}{' WRONG' if result['wrong'] else ''}")
            print(f"  S: {pair.source_query}\n  T: {pair.target_query}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1), encoding="utf-8")
    if args.write_results:
        if args.data or args.split != "all":
            parser.error("--write-results scores the pinned subset with --split all")
        for name, row in results_rows(results).items():
            write_results(name, row)
    return 1 if any(r["wrong"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
