"""Table-minimization cases: load them, run pipelines on DuckDB, compare protected outputs, prove them.

A case (format in ``docs/evals/table-minimization.md``) is a pipeline of tables, each one SELECT over
source tables and other tables named by bare name, a list of protected tables, a reference minimized
pipeline and traps (tempting simplifications that change a protected output, each with a witness
database). This module is shared by the case generator (``tools/make_minimization_cases.py``) and the
harness (``tools/minimization_bench.py``):

* ``random_databases`` / ``targeted_databases``: source databases that respect declared keys and NOT
  NULL columns, with values drawn from small domains plus every literal the queries mention.
* ``Engine``: every pipeline ("world") as DuckDB views over one set of source tables, run with the
  optimizer off (as ``kumosql.duckdb_load.run_unoptimized`` does), so an optimizer bug cannot hide or
  invent a difference.
* ``compare``: protected outputs as (column names, bag of rows) on each database.
* ``prove``: KumoSQL's ``prove_models`` on each protected table, original against new pipeline.
"""

from __future__ import annotations

from collections import Counter
import contextlib
from datetime import date, datetime
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import random
import re
import sys
import tempfile
import time
from typing import Iterable, Mapping

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

CASES_DIR = ROOT / "benchmarks" / "table_minimization"
HELD_OUT_SHARE = 5  # one case in five, chosen by a hash of its id
DUCK_TYPES = {"INT64": "BIGINT", "STRING": "VARCHAR", "FLOAT64": "DOUBLE", "BOOL": "BOOLEAN", "DATE": "DATE",
              "TIMESTAMP": "TIMESTAMP", "NUMERIC": "DECIMAL(38, 9)"}
BASE_DOMAINS = {
    "INT64": [-1, 0, 1, 2, 3, 4, 5],
    "STRING": ["a", "b", ""],
    "FLOAT64": [-1.5, 0.0, 0.5, 2.0, 3.25],
    "BOOL": [True, False],
    "DATE": [date(2024, 1, 1), date(2024, 1, 2), date(2024, 2, 29)],
    "TIMESTAMP": [datetime(2024, 1, 1), datetime(2024, 1, 1, 23, 59, 59), datetime(2024, 3, 1, 12)],
    "NUMERIC": [Decimal("-1.5"), Decimal("0"), Decimal("0.25"), Decimal("2"), Decimal("10.125")],
}
ROW_COUNTS = (0, 1, 2, 3, 4, 5, 6, 8)
NULL_RATE = 0.15
PROJECT, DATASET, RAW = "proj", "an", "raw"


def held_out_split(case_id: str) -> str:
    """The fixed split of a case id: one in ``HELD_OUT_SHARE`` is held out."""

    digest = int(hashlib.sha256(case_id.encode("utf-8")).hexdigest(), 16)
    return "held_out" if digest % HELD_OUT_SHARE == 0 else "dev"


# ------------------------------------------------------------------ loading


def load_cases(paths: Iterable[Path] | None = None, split: str | None = None) -> list[dict]:
    """Every case in ``benchmarks/table_minimization/*.jsonl`` (or ``paths``), optionally one split."""

    files = sorted(CASES_DIR.glob("*.jsonl")) if paths is None else [Path(p) for p in paths]
    cases = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                case = json.loads(line)
                if split is None or case["split"] == split:
                    cases.append(case)
    return cases


def case_input(case: Mapping) -> dict:
    """What a minimizer sees: no reference, no traps, no verification."""

    return {key: case[key] for key in ("id", "dialect", "sources", "tables", "protected")}


# ------------------------------------------------------------------ SQL plumbing


def _parse(sql: str, dialect: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read=dialect)


def _cte_names(tree: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}


def _bare_tables(tree: exp.Expression):
    """Table references that name a source or pipeline table (bare names, not CTEs)."""

    ctes = _cte_names(tree)
    for table in tree.find_all(exp.Table):
        if table.args.get("db") or table.args.get("catalog") or not table.name:
            continue
        name = table.name.lower()
        if name not in ctes:
            yield table, name


def reads(sql: str, dialect: str = "bigquery") -> set[str]:
    return {name for _, name in _bare_tables(_parse(sql, dialect))}


def topological_order(tables: Mapping[str, str], sources: Iterable[str], dialect: str = "bigquery") -> list[str]:
    """Tables in dependency order. Raises ``ValueError`` on an unknown name or a cycle."""

    known = set(tables)
    source_names = {s.lower() for s in sources}
    deps = {}
    for name, sql in tables.items():
        used = reads(sql, dialect)
        unknown = used - known - source_names
        if unknown:
            raise ValueError(f"{name} reads unknown tables: {sorted(unknown)}")
        deps[name] = used & known
    order, done, visiting = [], set(), set()

    def visit(name: str) -> None:
        if name in done:
            return
        if name in visiting:
            raise ValueError(f"cycle through {name}")
        visiting.add(name)
        for dep in sorted(deps[name]):
            visit(dep)
        visiting.discard(name)
        done.add(name)
        order.append(name)

    for name in sorted(tables):
        visit(name)
    return order


def upstream(tables: Mapping[str, str], name: str, dialect: str = "bigquery") -> set[str]:
    """``name`` and every pipeline table it reads, directly or not."""

    seen, todo = set(), [name]
    while todo:
        current = todo.pop()
        if current in seen or current not in tables:
            continue
        seen.add(current)
        todo.extend(reads(tables[current], dialect))
    return seen


def unchanged(original: Mapping[str, str], new: Mapping[str, str], name: str, dialect: str = "bigquery") -> bool:
    """Whether ``name`` and everything it reads are the same SQL in both pipelines."""

    if name not in new:
        return False
    closure = upstream(new, name, dialect)
    return closure == upstream(original, name, dialect) and all(
        _normal(new[t]) == _normal(original.get(t, "")) for t in closure)


def _normal(sql: str) -> str:
    return " ".join(sql.split())


def literals(sqls: Iterable[str], dialect: str = "bigquery") -> tuple[set, set]:
    """Integer and string literals in the queries (integers with their neighbours)."""

    ints, strings = set(), set()
    for sql in sqls:
        for lit in _parse(sql, dialect).find_all(exp.Literal):
            if lit.is_string:
                strings.add(lit.this)
            else:
                try:
                    value = int(lit.this)
                except ValueError:
                    continue
                ints.update((value - 1, value, value + 1))
    return ints, strings


# ------------------------------------------------------------------ databases


def domains(sources: Mapping[str, Mapping], sqls: Iterable[str], dialect: str = "bigquery") -> dict:
    """Value pool per ``(source, column)``: the column's ``values``, the type's base pool and the literals."""

    ints, strings = literals(sqls, dialect)
    pools = {}
    for name, spec in sources.items():
        hints = spec.get("values", {})
        for column, kind in spec["columns"].items():
            pool = [*hints.get(column, []), *BASE_DOMAINS[kind]]  # base values stand for "anything else"
            if kind == "INT64":
                pool += sorted(ints)
            elif kind == "STRING":
                pool += sorted(strings)
            pools[(name, column)] = list(dict.fromkeys(_typed(v, kind) for v in pool))
    return pools


def _typed(value, kind: str):
    if value is None:
        return None
    if kind == "DATE" and isinstance(value, str):
        return date.fromisoformat(value)
    if kind == "TIMESTAMP" and isinstance(value, str):
        return datetime.fromisoformat(value)
    if kind == "NUMERIC":
        return Decimal(str(value))
    if kind == "FLOAT64":
        return float(value)
    return value


def _required(spec: Mapping) -> set[str]:
    return set(spec.get("key", [])) | set(spec.get("not_null", []))


def random_database(sources: Mapping[str, Mapping], pools: Mapping, rng: random.Random) -> dict[str, list[tuple]]:
    db = {}
    for name, spec in sources.items():
        columns = list(spec["columns"])
        key = [columns.index(c) for c in spec.get("key", [])]
        required = _required(spec)
        target = rng.choice(ROW_COUNTS)
        rows, seen = [], set()
        for _ in range(target * 4):
            if len(rows) >= target:
                break
            row = tuple(
                None if c not in required and rng.random() < NULL_RATE else rng.choice(pools[(name, c)])
                for c in columns)
            if key:
                k = tuple(row[i] for i in key)
                if k in seen:
                    continue
                seen.add(k)
            rows.append(row)
        if rows and not key and rng.random() < 0.3:
            rows.append(rng.choice(rows))  # duplicates
        db[name] = rows
    return db


def targeted_databases(sources: Mapping[str, Mapping], pools: Mapping) -> list[dict[str, list[tuple]]]:
    """Empty sources, one all-NULL row per source, and duplicated rows."""

    empty = {name: [] for name in sources}
    nulls, dupes = {}, {}
    for name, spec in sources.items():
        columns = list(spec["columns"])
        required = _required(spec)
        nulls[name] = [tuple(pools[(name, c)][0] if c in required else None for c in columns)]
        row = tuple(pools[(name, c)][min(1, len(pools[(name, c)]) - 1)] for c in columns)
        dupes[name] = [row] if spec.get("key") else [row, row]
    return [empty, nulls, dupes]


def check_databases(case: Mapping, worlds: Iterable[Mapping[str, str]], count: int, seed: int | None = None) -> list[dict]:
    """The targeted databases, ``count`` random ones, the case's ``data`` rows and every trap witness."""

    sources = case["sources"]
    sqls = [sql for world in worlds for sql in world.values()]
    pools = domains(sources, sqls, case.get("dialect", "bigquery"))
    rng = random.Random(seed if seed is not None else case["id"])
    dbs = targeted_databases(sources, pools) + [random_database(sources, pools, rng) for _ in range(count)]
    if case.get("data"):
        dbs.append(witness_rows(sources, case["data"]))
    for trap in case.get("traps", []):
        dbs.append(witness_rows(sources, trap["witness"]))
    return dbs


def witness_rows(sources: Mapping[str, Mapping], witness: Mapping[str, list]) -> dict[str, list[tuple]]:
    """A stored witness (JSON lists) as source rows; sources it leaves out are empty."""

    out = {}
    for name, spec in sources.items():
        kinds = list(spec["columns"].values())
        out[name] = [tuple(_typed(v, k) for v, k in zip(row, kinds)) for row in witness.get(name, [])]
    return out


def witness_json(db: Mapping[str, list[tuple]]) -> dict[str, list[list]]:
    def plain(value):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return str(value) if isinstance(value, Decimal) else value

    return {name: [[plain(v) for v in row] for row in rows] for name, rows in db.items() if rows}


# ------------------------------------------------------------------ DuckDB


def _norm(value):
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, Decimal):
        return round(float(value), 9)
    return value


class WorldError(Exception):
    pass


class Engine:
    """Each world (a pipeline) as DuckDB views over one set of source tables, optimizer off."""

    def __init__(self, sources: Mapping[str, Mapping], worlds: Mapping[str, Mapping[str, str]], dialect: str = "bigquery"):
        import duckdb

        self.sources = sources
        self.con = duckdb.connect(":memory:")
        self.con.execute("PRAGMA disable_optimizer")  # as kumosql.duckdb_load.run_unoptimized
        self.errors: dict[str, str] = {}
        self.worlds = {label: dict(tables) for label, tables in worlds.items()}
        self._loaded: dict[str, str] = {}  # source -> repr of the rows it holds
        self._prepared: dict[str, str] = {}  # view -> prepared statement ("" when it could not be prepared)
        for name, spec in sources.items():
            columns = ", ".join(f'"{c}" {DUCK_TYPES[t]}' for c, t in spec["columns"].items())
            self.con.execute(f'CREATE TABLE "{name}" ({columns})')
        for label, tables in self.worlds.items():
            try:
                for name in topological_order(tables, sources, dialect):
                    tree = _parse(tables[name], dialect)
                    for table, ref in list(_bare_tables(tree)):
                        if ref in tables:
                            if not table.alias:  # qualified columns keep naming the table
                                table.set("alias", exp.TableAlias(this=exp.to_identifier(table.name)))
                            table.set("this", exp.to_identifier(f"{label}__{ref}", quoted=True))
                    self.con.execute(f'CREATE VIEW "{label}__{name}" AS {tree.sql(dialect="duckdb")}')
            except Exception as error:  # a world that cannot be built: every read of it is an error
                self.errors[label] = f"{type(error).__name__}: {error}"[:300]

    def load(self, db: Mapping[str, list[tuple]]) -> None:
        from kumosql.duckdb_load import insert_rows

        for name in self.sources:
            rows = db.get(name, [])
            held = repr(rows)  # repr tells 1 from 1.0, True and -0.0, which compare equal
            if self._loaded.get(name) == held:
                continue  # the table already holds exactly these rows, in this order
            self._loaded.pop(name, None)
            self.con.execute(f'DELETE FROM "{name}"')
            insert_rows(self.con, f'"{name}"', rows)
            self._loaded[name] = held

    def output(self, label: str, name: str) -> tuple[tuple[str, ...], Counter]:
        if label in self.errors:
            raise WorldError(self.errors[label])
        if name not in self.worlds[label]:
            raise WorldError(f"{name} is missing")
        try:
            cursor, rows = self._select(f"{label}__{name}")
        except Exception as error:
            raise WorldError(f"{name}: {type(error).__name__}: {error}"[:300]) from error
        columns = tuple(d[0].lower() for d in cursor.description)
        return columns, Counter(tuple(_norm(v) for v in row) for row in rows)

    def _select(self, view: str):
        """``(cursor, rows)`` of ``SELECT * FROM view``, through a statement prepared on first use.

        With the optimizer off the plan never depends on the rows loaded, so the prepared statement returns what
        the query would; preparing skips parsing and binding the views on every database. Anything that fails
        prepared runs as the plain query, so its error reads as it always did.
        """

        statement = self._prepared.get(view)
        if statement is None:
            statement = f"kumo_select_{len(self._prepared)}"
            try:
                self.con.execute(f'PREPARE {statement} AS SELECT * FROM "{view}"')
            except Exception:
                statement = ""
            self._prepared[view] = statement
        if statement:
            try:
                cursor = self.con.execute(f"EXECUTE {statement}")
                return cursor, cursor.fetchall()
            except Exception:
                pass
        cursor = self.con.execute(f'SELECT * FROM "{view}"')
        return cursor, cursor.fetchall()

    def close(self) -> None:
        self.con.close()


ORDER_SENSITIVE = re.compile(r"\bOVER\b|\bLIMIT\b|ARRAY_AGG|STRING_AGG|ANY_VALUE|\bFIRST\b|\bLAST\b", re.IGNORECASE)


def order_sensitive(tables: Mapping[str, str]) -> bool:
    """Whether a pipeline may depend on row order (windows, LIMIT, ordered or arbitrary aggregates)."""

    return any(ORDER_SENSITIVE.search(sql) for sql in tables.values())


def reversed_rows(db: Mapping[str, list[tuple]]) -> dict[str, list[tuple]]:
    return {name: list(reversed(rows)) for name, rows in db.items()}


def compare(engine: Engine, dbs: Iterable[Mapping], base: str, others: Iterable[str], names: Iterable[str],
            stable_only: bool = False) -> dict:
    """For each other world, the first difference from ``base`` on a protected table, or ``None``.

    A difference is ``{"table", "database", "reason"}``; an error in the other world counts as one. An
    error in ``base`` raises. With ``stable_only``, a database counts for a protected table only when
    ``base`` gives that table the same output with every source's rows reversed: where the original
    itself depends on row order (ties in a window's ORDER BY), no answer can be held to one order.
    """

    names = list(names)
    found = {label: None for label in others}
    for db in dbs:
        if all(found.values()):
            break
        checked = names
        if stable_only:
            engine.load(reversed_rows(db))
            flipped = {name: engine.output(base, name) for name in names}
        engine.load(db)
        expected = {name: engine.output(base, name) for name in names}
        if stable_only:
            checked = [name for name in names if flipped[name] == expected[name]]
        for label in found:
            if found[label]:
                continue
            for name in checked:
                try:
                    got = engine.output(label, name)
                except WorldError as error:
                    found[label] = {"table": name, "database": db, "reason": str(error)}
                    break
                if got != expected[name]:
                    reason = "column names differ" if got[0] != expected[name][0] else "rows differ"
                    found[label] = {"table": name, "database": db, "reason": reason}
                    break
    return found


def differs_on(engine: Engine, db: Mapping, base: str, other: str, names: Iterable[str]) -> list[str]:
    """Protected tables on which ``other`` differs from ``base`` on ``db`` (errors count)."""

    engine.load(db)
    out = []
    for name in names:
        try:
            if engine.output(other, name) != engine.output(base, name):
                out.append(name)
        except WorldError:
            out.append(name)
    return out


def shrink(engine: Engine, db: Mapping[str, list[tuple]], base: str, other: str, names: list[str]) -> dict:
    """Drop rows from ``db`` while ``other`` still differs from ``base`` on some protected table."""

    db = {name: list(rows) for name, rows in db.items()}
    progress = True
    while progress:
        progress = False
        for table in list(db):
            for index in range(len(db[table]) - 1, -1, -1):
                trial = {**db, table: db[table][:index] + db[table][index + 1:]}
                if differs_on(engine, trial, base, other, names):
                    db = trial
                    progress = True
    return db


# ------------------------------------------------------------------ proofs


def _sqlx(sql: str, tables: set[str], suffix: str, dialect: str) -> str:
    tree = _parse(sql, dialect)
    for table, name in list(_bare_tables(tree)):
        marker = f"zzref_{name}{suffix}_zz" if name in tables else f"zzsrc_{name}_zz"
        if not table.alias:  # qualified columns keep naming the table
            table.set("alias", exp.TableAlias(this=exp.to_identifier(table.name)))
        table.set("this", exp.to_identifier(marker))
    text = tree.sql(dialect="bigquery")
    text = re.sub(r"zzref_([a-z0-9_]+?)_zz", lambda m: '${ref("%s")}' % m.group(1), text)
    return re.sub(r"zzsrc_([a-z0-9_]+?)_zz", lambda m: '${ref("%s", "%s")}' % (RAW, m.group(1)), text)


def write_project(root: Path, sources: Mapping[str, Mapping], worlds: Mapping[str, Mapping[str, str]], dialect: str = "bigquery") -> None:
    """A Dataform project: sources declared in ``raw``, each world's tables with its suffix (``""`` first)."""

    definitions = root / "definitions"
    definitions.mkdir(parents=True, exist_ok=True)
    (root / "workflow_settings.yaml").write_text(f"defaultProject: {PROJECT}\ndefaultDataset: {DATASET}\n", encoding="utf-8")
    for name in sources:
        (definitions / f"src_{name}.sqlx").write_text(
            f'config {{ type: "declaration", schema: "{RAW}", name: "{name}" }}\n', encoding="utf-8")
    for suffix, tables in worlds.items():
        for name, sql in tables.items():
            (definitions / f"{name}{suffix}.sqlx").write_text(
                'config { type: "table" }\n' + _sqlx(sql, set(tables), suffix, dialect) + "\n", encoding="utf-8")


def prover_schema(pipeline, sources: Mapping[str, Mapping]):
    """Source columns, keys and NOT NULL columns, plus each model's columns, for the prover."""

    from kumosql.prover_schema import _Builder, _select_names

    builder = _Builder()
    for name, spec in sources.items():
        builder.add(f"{PROJECT}.{RAW}.{name}", list(spec["columns"]), sorted(_required(spec)),
                    [spec["key"]] if spec.get("key") else [], source="case",
                    types=dict(spec["columns"]))
    for key, model in pipeline.models.items():
        if model.is_query and "${" not in model.sql:
            builder.add(model.target.key or key, _select_names(model.sql))
    return builder.build()


def load_project(sources: Mapping[str, Mapping], worlds: Mapping[str, Mapping[str, str]], dialect: str = "bigquery"):
    """``(pipeline, schema)`` for the worlds, loaded from a temporary Dataform project."""

    from kumosql import load_sqlx_project

    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
        root = Path(tmp)
        write_project(root, sources, worlds, dialect)
        pipeline = load_sqlx_project(root)
    for name, spec in sources.items():
        pipeline.source_schema[f"{PROJECT}.{RAW}.{name}"] = {c: t for c, t in spec["columns"].items()}
    return pipeline, prover_schema(pipeline, sources)


def prove(sources: Mapping[str, Mapping], original: Mapping[str, str], new: Mapping[str, str], names: Iterable[str],
          dialect: str = "bigquery", timeout_ms: int = 5000) -> dict[str, dict]:
    """Per protected table: ``{"status": "same" | "proved" | "unknown", "reason"}``.

    ``same`` means the table and everything it reads are unchanged SQL, so nothing needs proving.
    """

    from kumosql.pipeline_equivalence import prove_models

    out = {}
    todo = []
    for name in names:
        if unchanged(original, new, name, dialect):
            out[name] = {"status": "same", "reason": "unchanged"}
        elif name not in new:
            out[name] = {"status": "unknown", "reason": "missing"}
        else:
            todo.append(name)
    if not todo:
        return out
    try:
        pipeline, schema = load_project(sources, {"": original, "__after": new}, dialect)
    except Exception as error:
        return {**out, **{n: {"status": "unknown", "reason": f"could not load: {error}"[:200]} for n in todo}}
    for name in todo:
        started = time.time()
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                result = prove_models(pipeline, f"{PROJECT}.{DATASET}.{name}", f"{PROJECT}.{DATASET}.{name}__after",
                                      declared=[], schema=schema, timeout_ms=timeout_ms)
            status = "proved" if result.proven else "unknown"
            reason = "" if result.proven else (result.reason or "")[:200]
        except Exception as error:  # the prover failing is an unknown, never a proof
            status, reason = "unknown", f"{type(error).__name__}: {error}"[:200]
        out[name] = {"status": status, "reason": reason, "seconds": round(time.time() - started, 2)}
    return out
