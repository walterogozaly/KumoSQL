"""Proven rewrites of the engine-suite evals (``tools/engine_suites.py``), as engine cases.

The eval sends every query of another engine's test suite through KumoSQL's canonical rule order (and wrapped or padded
variants of it, the "amplified" rows), executes the query and the rewrite once on the suite's own database in DuckDB, and
counts the rewrite verified when the rows match. The adapters keep the rewrites whose verification is ``proven`` and run
the same two DuckDB texts (the original after the eval's BigQuery round trip, the rewrite after ``sqlglot.transpile`` from
BigQuery) on thousands of random databases over the tables the suite's setup creates.

* ``engine-duckdb-slt-plain`` / ``-amplified``, ``engine-sqlite-slt-plain`` / ``-amplified``: the SQLLogicTest files.
  The setup statements before the query run natively in a fresh DuckDB (as the eval runs them) and the catalog it ends
  with, read from ``information_schema``, gives the tables and column types; keys and NOT NULL are not read (the proof
  assumes none). A table with a column type the engine cannot generate (BLOB, LIST, STRUCT, UUID, INTERVAL, HUGEINT...)
  makes the pair ``unrunnable``.
* ``engine-sqlglot-fixtures-plain`` / ``-amplified``: SQLGlot's identity and optimizer fixtures, over the synthetic tables
  the eval builds (BIGINT and VARCHAR columns from the fixture's schema) or the TPC data files' own columns.

An item is one (query, variant) pair; the eval's file selection (``--stride``, ``--max-queries``) is applied as in each
results file's command. The suites are fetched by the eval's own ``fetch_suite`` (``KUMOSQL_SUITES_DIR``). The lists are
long (tens of thousands of rewrites), so a run takes ``--every N``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sys

import sqlglot

TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
for _path in (str(TOOLS), str(ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from recheck import dialect_rewrites as dr  # noqa: E402
from recheck import new_evals_b  # noqa: E402
from recheck.engine import Case, Column, Table  # noqa: E402

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

Adapter = dr.Adapter

_KINDS = {
    "TINYINT": ("int", "TINYINT"), "SMALLINT": ("int", "SMALLINT"), "INTEGER": ("int", "INTEGER"), "BIGINT": ("int", "BIGINT"),
    "UTINYINT": ("int", "UTINYINT"), "USMALLINT": ("int", "USMALLINT"), "UINTEGER": ("int", "UINTEGER"), "UBIGINT": ("int", "UBIGINT"),
    "DOUBLE": ("float", "DOUBLE"), "FLOAT": ("float", "FLOAT"), "REAL": ("float", "FLOAT"), "VARCHAR": ("text", "VARCHAR"),
    "BOOLEAN": ("bool", "BOOLEAN"), "DATE": ("date", "DATE"), "TIMESTAMP": ("timestamp", "TIMESTAMP"), "TIME": ("time", "TIME"),
}


def tables_of(connection) -> dict[str, Table] | None:
    """The base tables of a DuckDB connection as engine tables, or None when a column has a type the engine cannot draw."""

    rows = connection.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = 'main' "
        "AND table_name IN (SELECT table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE') "
        "ORDER BY table_name, ordinal_position"
    ).fetchall()
    out: dict[str, Table] = {}
    for table, column, kind in rows:
        upper = kind.upper()
        if upper.startswith("DECIMAL"):
            mapped = ("decimal", upper.replace(" ", ""))
        elif upper in _KINDS:
            mapped = _KINDS[upper]
        else:
            return None
        out.setdefault(table, Table(table, [])).columns.append(Column(column, mapped[0], sql_type=mapped[1]))
    return out


def _unrunnable(name: str, pair: str, left: str, right: str, held_out: bool, meta: dict) -> Case:
    """A case the engine reports as unrunnable (its schema cannot be created); the reason is in ``meta``."""

    return Case(name, pair, "SELECT 1", "SELECT 1", {"x": Table("x", [Column("a", "int", sql_type="NO SUCH TYPE")])},
                held_out=held_out, source=(left, right), dialect="bigquery", meta=meta)


def rewrite_pair(record_sql: str, dialect: str | None, variant: str) -> tuple[str, str, str] | None:
    """``(control DuckDB text, rewrite DuckDB text, rewrite BigQuery text)`` for one variant, when the canonical rule order
    changes it and the verification is proven; None otherwise (``engine_suites.evaluate_query`` and ``_treat``)."""

    import engine_suites as es
    from kumosql.rewrite import VerificationStatus, apply_rules, canonical_rule_order

    tree = es._single_query(record_sql, dialect or "")
    if tree is None or es.NONDETERMINISTIC.search(record_sql):
        return None
    try:
        bigquery_sql = tree.sql(dialect="bigquery")
        control = sqlglot.transpile(bigquery_sql, read="bigquery", write="duckdb")[0]
    except Exception:  # noqa: BLE001
        return None
    if variant == "plain":
        text = bigquery_sql
    else:
        text = dict(es._variants(tree)).get(variant)
        if text is None:
            return None
    try:
        result = apply_rules(canonical_rule_order(), text)
    except Exception:  # noqa: BLE001
        return None
    try:
        if sqlglot.parse_one(result.sql, read="bigquery") == sqlglot.parse_one(text, read="bigquery"):
            return None
    except Exception:  # noqa: BLE001
        pass
    if result.verification.status != VerificationStatus.PROVEN:
        return None
    try:
        return control, sqlglot.transpile(result.sql, read="bigquery", write="duckdb")[0], result.sql
    except Exception:  # noqa: BLE001
        return None


VARIANTS = ("plain", "wrap-subquery", "wrap-cte", "unused-cte", "trivial-predicate")


class EngineSuite(Adapter):
    """One of the six engine-suite results files: a suite, and the plain or the amplified variants."""

    def __init__(self, name: str, suite: str, amplified: bool, stride: int = 1, max_queries: int | None = None):
        self.name = name
        self.suite = suite
        self.variants = VARIANTS[1:] if amplified else VARIANTS[:1]
        self.stride = stride
        self.max_queries = max_queries

    def _root(self) -> Path:
        import engine_suites as es

        return es.fetch_suite(self.suite)

    def items(self) -> list[dict]:
        import engine_suites as es

        root = self._root()
        out = []
        for relative in es.collect_files(self.suite, root, None, self.stride):
            held = es._held_out(relative)
            path = root / relative
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            if es.SUITES[self.suite].get("kind") == "sqlglot":
                records = [(line, sql) for _, sql, _, line in es.parse_fixture(text, path.name == "identity.sql")]
                taken = records[: self.max_queries] if self.max_queries else records
            else:
                parsed, skipped = es.parse_slt(text)
                if skipped:
                    continue
                queries = [r for r in parsed if r.kind == "query" and r.sql and not r.skip]
                taken = [(r.line, r.sql) for r in (queries[: self.max_queries] if self.max_queries else queries)]
            for line, _ in taken:
                for variant in self.variants:
                    out.append({"pair": f"{relative}:{line}#{variant}", "file": relative, "line": line, "variant": variant, "held_out": held})
        return out

    def case(self, item: dict) -> Case | None:
        import engine_suites as es

        new_evals_b._install()
        root = self._root()
        path = root / item["file"]
        text = path.read_text(encoding="utf-8")
        if es.SUITES[self.suite].get("kind") == "sqlglot":
            return self._sqlglot_case(es, item, path, text)
        return self._slt_case(es, item, text)

    def _slt_case(self, es, item: dict, text: str) -> Case | None:
        records, skipped = es.parse_slt(text)
        if skipped:
            return None
        connection = es._connect()
        try:
            target = None
            for record in records:
                if record.skip:
                    continue
                if record.kind == "statement":
                    try:
                        es._run(connection, record.sql)
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                if record.line == item["line"] and record.sql:
                    target = record
                    break
            if target is None:
                return None
            pair = rewrite_pair(target.sql, "duckdb", item["variant"])
            if pair is None or not self._runs(es, connection, pair[0]):
                return None
            tables = tables_of(connection)
        finally:
            connection.close()
        return self._build(item, target.sql, pair, tables)

    def _sqlglot_case(self, es, item: dict, path: Path, text: str) -> Case | None:
        identity = path.name == "identity.sql"
        has_data = any(path.parent.glob("*.csv.gz"))
        target = next(((meta, sql, expected, line) for meta, sql, expected, line in es.parse_fixture(text, identity) if line == item["line"]), None)
        if target is None:
            return None
        meta, sql, _expected, _line = target
        dialect = meta.get("dialect") or ("duckdb" if has_data else "")
        pair = rewrite_pair(sql, dialect or None, item["variant"])
        if pair is None:
            return None
        if has_data:
            connection = es._tpc_connection(path.parent)
        else:
            schema = es.DEFAULT_SCHEMA
            if meta.get("schema"):
                try:
                    schema = json.loads(meta["schema"])
                except Exception:  # noqa: BLE001
                    schema = es.DEFAULT_SCHEMA
            connection = es._synthetic_connection(schema)
        try:
            if not self._runs(es, connection, pair[0]):
                return None
            tables = tables_of(connection)
        finally:
            connection.close()
        return self._build(item, sql, pair, tables)

    @staticmethod
    def _runs(es, connection, control: str) -> bool:
        """The eval counts a query whose original does not run on the suite's database unsupported, not rewritten."""

        try:
            es._run(connection, control)
        except Exception:  # noqa: BLE001
            return False
        return True

    def _build(self, item: dict, sql: str, pair: tuple[str, str, str], tables: dict[str, Table] | None) -> Case:
        control, treated_duck, treated = pair
        meta = {"variant": item["variant"], "file": item["file"]}
        if not tables:
            meta["unrunnable"] = "the suite's tables have a column type the engine cannot generate" if tables is None else "the query reads no table of the suite"
            if tables is None:
                return _unrunnable(self.name, item["pair"], sql, treated, item["held_out"], meta)
        return Case(self.name, item["pair"], control, treated_duck, tables or {}, held_out=item["held_out"], source=(sql, treated),
                    dialect="bigquery", meta=meta)


ADAPTERS = {
    a.name: a
    for a in [
        EngineSuite("engine-duckdb-slt-plain", "duckdb-slt", False),
        EngineSuite("engine-duckdb-slt-amplified", "duckdb-slt", True),
        EngineSuite("engine-sqlite-slt-plain", "sqlite-slt", False, stride=4, max_queries=40),
        EngineSuite("engine-sqlite-slt-amplified", "sqlite-slt", True, stride=4, max_queries=40),
        EngineSuite("engine-sqlglot-fixtures-plain", "sqlglot-fixtures", False),
        EngineSuite("engine-sqlglot-fixtures-amplified", "sqlglot-fixtures", True),
    ]
}
