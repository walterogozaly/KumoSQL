"""IBM FIBEN for the sample-database eval: the run-time download, the load and the query adaptation.

FIBEN (https://github.com/IBM/fiben-benchmark, Apache-2.0) is a financial data mart of 152 tables
(companies, officers, accounts, securities, holdings, transactions and the XBRL financial reports of
public companies) with an 80 MB ``data.zip`` of Db2 ``DEL`` files (400 MB of CSV) and 300 natural
language questions with 237 distinct SQL targets, 170 of them nested.

The DDL (``FIBEN.sql``), the query file and the licence are small and Apache-2.0: they are committed
unchanged in ``tests/fixtures/sample_databases/fiben/upstream/``. The data is too big to commit: it is
downloaded on first use from the pinned commit into a cache folder (``KUMOSQL_BENCH_DATA``, default
``~/.cache/kumosql-bench``), checked against the pinned SHA-256 of the archive and of every CSV file,
and tests that need it skip when it cannot be fetched (:class:`Unavailable`).

This module holds what is specific to FIBEN and needs nothing from ``sample_db_bench``; the ``Adapter``
subclass that plugs it into the harness is ``FIBEN`` in ``tools/sample_db_bench.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.request
import zipfile

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

ROOT = Path(__file__).resolve().parent.parent
FOLDER = ROOT / "tests" / "fixtures" / "sample_databases" / "fiben"

REPO = "IBM/fiben-benchmark"
COMMIT = "96eaffc60cc824b8f545cd29adffc2a170e08d04"
BASE = f"https://raw.githubusercontent.com/{REPO}/{COMMIT}/"
ARCHIVE = "data.zip"
ARCHIVE_BYTES = 80_216_305
ARCHIVE_SHA256 = "17b4ef1ad5051f73ce01a2237a22bbedd8179de7ac4b6c345b14c8e98e9f7a9e"
CACHE = (
    Path(os.environ.get("KUMOSQL_BENCH_DATA", Path.home() / ".cache" / "kumosql-bench"))
    / "fiben"
    / COMMIT[:12]
)
#: the pinned upstream files committed unchanged (small, Apache-2.0)
COMMITTED = ("FIBEN.sql", "FIBEN_Queries.json", "tablelist.txt", "LICENSE")
SCHEMA = "FIBEN"  # the Db2 schema the upstream queries qualify every table with


class Unavailable(OSError):
    """The data cannot be fetched (no network) or does not match the pinned version."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest() -> dict:
    """``{table: {"bytes", "sha256", "rows"}}`` of the 152 CSV files in ``data.zip`` (pinned, committed)."""

    return json.loads((FOLDER / "data_manifest.json").read_text(encoding="utf-8"))[
        "files"
    ]


# ---------------------------------------------------------------- the run-time download


def _download() -> Path:
    path = CACHE / ARCHIVE
    if path.exists() and path.stat().st_size == ARCHIVE_BYTES:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(BASE + ARCHIVE, timeout=120) as response:
            with tmp.open("wb") as out:
                for block in iter(lambda: response.read(1 << 20), b""):
                    digest.update(block)
                    out.write(block)
    except OSError as error:  # URLError, timeouts and resets are all OSErrors
        tmp.unlink(missing_ok=True)
        raise Unavailable(f"cannot download {BASE + ARCHIVE}: {error}") from error
    if digest.hexdigest() != ARCHIVE_SHA256:
        tmp.unlink(missing_ok=True)
        raise Unavailable(
            f"{ARCHIVE} has SHA-256 {digest.hexdigest()}, not the pinned {ARCHIVE_SHA256} (git LFS pointer or a changed file?)"
        )
    tmp.replace(path)  # atomic, so parallel runs never read half a file
    return path


def data_dir() -> Path:
    """The folder with the 152 CSV files, downloaded and extracted once; every file is checked against the manifest."""

    out = CACHE / "data"
    files = manifest()
    if not all(
        (out / f"{t}.csv").exists()
        and (out / f"{t}.csv").stat().st_size == f["bytes"]
        for t, f in files.items()
    ):
        archive = _download()
        out.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zf:
            for table, spec in files.items():
                target = out / f"{table}.csv"
                if target.exists() and target.stat().st_size == spec["bytes"]:
                    continue
                tmp = target.with_suffix(f".{os.getpid()}.part")
                with zf.open(f"data/{table}.csv") as source, tmp.open("wb") as sink:
                    for block in iter(lambda: source.read(1 << 20), b""):
                        sink.write(block)
                tmp.replace(target)
    for table, spec in files.items():
        got = sha256_file(out / f"{table}.csv")
        if got != spec["sha256"]:
            raise Unavailable(
                f"{table}.csv has SHA-256 {got}, not the pinned {spec['sha256']}; delete {out} to download again"
            )
    return out


def available() -> bool:
    """Can the data be had (cached, or downloadable)?"""

    try:
        data_dir()
    except OSError:
        return False
    return True


# ---------------------------------------------------------------- the adapted schema


BIGQUERY_TYPES = {
    "BIGINT": "INT64",
    "INTEGER": "INT64",
    "VARCHAR": "STRING",
    "CHARACTER": "STRING",
    "DOUBLE PRECISION": "FLOAT64",
    "REAL": "FLOAT64",
    "TIMESTAMP": "DATETIME",
    "DATE": "DATE",
}


def bigquery_type(db2_type: str) -> str:
    return BIGQUERY_TYPES[re.sub(r"\(.*\)", "", db2_type).strip().upper()]


def adapted_schema_text(ddl: str, read_ddl) -> str:
    """The BigQuery DDL for FIBEN's upstream DDL, as committed in ``adapted/schema.sql`` (a test regenerates it).

    Tables in dependency order (a foreign key's parent first) so the script also runs on BigQuery; primary and
    foreign keys declared ``NOT ENFORCED``; every column keeps its name and order.
    """

    tables = read_ddl(ddl)
    parents = {n: {p for _, p, _ in t.foreign_keys if p != n} for n, t in tables.items()}
    order: list[str] = []
    while len(order) < len(tables):
        ready = sorted(
            n for n in tables if n not in order and parents[n] <= set(order)
        )
        if not ready:  # a foreign key cycle: keep the rest in file order
            ready = [n for n in tables if n not in order]
        order += ready
    blocks = []
    for name in order:
        table = tables[name]
        lines = [
            f"  {column} {bigquery_type(kind)}"
            + (" NOT NULL" if column in table.not_null else "")
            for column, kind in table.columns.items()
        ]
        if table.primary_key:
            lines.append(f"  PRIMARY KEY ({', '.join(table.primary_key)}) NOT ENFORCED")
        for columns, parent, parent_columns in table.foreign_keys:
            lines.append(
                f"  FOREIGN KEY ({', '.join(columns)}) REFERENCES {parent}({', '.join(parent_columns)}) NOT ENFORCED"
            )
        blocks.append(f"CREATE TABLE {name} (\n" + ",\n".join(lines) + "\n);\n")
    return HEADER + "\n" + "\n".join(blocks)


HEADER = f"""-- ADAPTED, not upstream. BigQuery DDL for the IBM FIBEN schema (https://github.com/{REPO}, FIBEN.sql at
-- commit {COMMIT}; the unchanged Db2/PostgreSQL DDL is in ../upstream/FIBEN.sql, the licence in
-- ../upstream/LICENSE). Generated by tools/sample_db_fiben.py (adapted_schema_text); a test regenerates it.
--
-- Adaptation:
-- * Db2 types become BigQuery types: BIGINT and INTEGER -> INT64; VARCHAR(1024) and CHARACTER(5) -> STRING;
--   DOUBLE PRECISION and REAL -> FLOAT64 (REAL is single precision in Db2; the CSV files write every value with
--   15 significant digits, which a FLOAT64 holds); TIMESTAMP -> DATETIME; DATE -> DATE.
-- * PRIMARY KEY and FOREIGN KEY, which upstream declares with ALTER TABLE ... ADD CONSTRAINT, are declared inline,
--   NOT ENFORCED, as BigQuery declares them; the constraint names are dropped. Tables come in dependency order.
-- * The Db2 schema qualifier (FIBEN) is dropped: the tables sit in the dataset the script is run in.
-- Tables, columns, column order, NOT NULL, primary keys and foreign keys are the upstream ones;
-- tools/sample_db_bench.py checks that against the upstream DDL on every run."""


# ---------------------------------------------------------------- loading the data


def copy_sql(table, path: Path) -> str:
    """The DuckDB ``COPY`` of one Db2 ``DEL`` file: comma separated, strings in double quotes, an empty field NULL,
    timestamps ``2015-05-20-00.00.00.000000``, dates ``20150930``, doubles ``+2.48800000000000E+003``."""

    return (
        f"COPY \"{table.name}\" FROM '{path}' (FORMAT csv, HEADER false, DELIMITER ',', QUOTE '\"', "
        "TIMESTAMPFORMAT '%Y-%m-%d-%H.%M.%S.%f', DATEFORMAT '%Y%m%d')"
    )


def load_data(con, tables) -> None:
    folder = data_dir()
    for table in tables:
        path = folder / f"{table.name}.csv"
        if path.stat().st_size:
            con.execute(copy_sql(table, path))


def line_count(path: Path) -> int:
    """Rows of a ``DEL`` file by an independent count (newlines: no FIBEN field holds one, which the load check confirms)."""

    count = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            count += block.count(b"\n")
    return count


# ---------------------------------------------------------------- adapting the queries


def _bare(identifier: exp.Identifier) -> None:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier.name):
        identifier.set("quoted", False)


def _column_types(tree: exp.Expression, types: dict[str, dict[str, str]]):
    """``{id(column node): declared type}`` for the columns of ``tree`` that resolve to a base table."""

    out: dict[int, str] = {}
    for scope in traverse_scope(tree):
        sources: dict[str, str] = {}
        current = scope
        while current is not None:  # inner scopes see the aliases of the scopes around them
            for alias, source in current.sources.items():
                if isinstance(source, exp.Table) and source.name.upper() in types:
                    sources.setdefault(alias.upper(), source.name.upper())
            current = current.parent
        for column in scope.columns:
            if column.table:
                table = sources.get(column.table.upper())
                candidates = [table] if table else []
            else:
                candidates = [
                    t
                    for alias, t in sources.items()
                    if column.name.upper() in types[t]
                ]
            if len(set(candidates)) == 1 and column.name.upper() in types[candidates[0]]:
                out[id(column)] = types[candidates[0]][column.name.upper()]
    return out


def adapt_query(sql: str, types: dict[str, dict[str, str]]) -> tuple[str, list[str]]:
    """A FIBEN query (Db2 SQL as the benchmark writes it) as BigQuery SQL, with the list of what changed.

    ``types`` is ``{TABLE: {COLUMN: BigQuery type}}`` in upper case. The changes are all syntactic or type
    coercions that BigQuery does not do on its own; nothing is added to or taken from the query's meaning:

    - the Db2 schema qualifier ``FIBEN.`` and the quotes round the upper-case names are dropped;
    - ``FETCH FIRST n ROWS ONLY`` becomes ``LIMIT n``;
    - ``YEAR(x)`` and ``MONTH(x)`` become ``EXTRACT(YEAR FROM x)`` (BigQuery has no such functions);
    - a string literal compared with an ``INT64`` column (Db2 converts it) becomes a number.
    """

    tree = sqlglot.parse_one(sql, read="postgres")
    changes: list[str] = []
    if any(
        t.args.get("db") and t.args["db"].name.upper() == SCHEMA
        for t in tree.find_all(exp.Table)
    ):
        changes.append("schema qualifier FIBEN dropped")
    quoted = any(i.args.get("quoted") for i in tree.find_all(exp.Identifier))
    for table in tree.find_all(exp.Table):
        db = table.args.get("db")
        if db is not None and db.name.upper() == SCHEMA:
            table.set("db", None)
    for identifier in tree.find_all(exp.Identifier):
        _bare(identifier)
    if quoted:
        changes.append("quoted upper-case identifiers written bare")
    limit = tree.args.get("limit")
    if isinstance(limit, exp.Fetch):
        tree.set("limit", exp.Limit(expression=limit.args["count"]))
        changes.append("FETCH FIRST n ROWS ONLY -> LIMIT n")
    for kind, node in (("YEAR", exp.Year), ("MONTH", exp.Month)):
        for call in list(tree.find_all(node)):
            call.replace(
                exp.Extract(this=exp.var(kind), expression=call.this.copy())
            )
            changes.append(f"{kind.lower()}(x) -> EXTRACT({kind} FROM x)")
    typed = _column_types(tree, types)

    def integer_column(node) -> bool:
        return isinstance(node, exp.Column) and typed.get(id(node)) == "INT64"

    def number(literal):
        if (
            isinstance(literal, exp.Literal)
            and literal.is_string
            and re.fullmatch(r"-?\d+", literal.name)
        ):
            return exp.Literal.number(int(literal.name))
        return None

    coerced = 0
    for node in list(tree.find_all(exp.Binary)):
        if not isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            continue
        for column_side, literal_side in (("this", "expression"), ("expression", "this")):
            value = number(node.args[literal_side])
            if value is not None and integer_column(node.args[column_side]):
                node.set(literal_side, value)
                coerced += 1
    for node in list(tree.find_all(exp.Between)):
        if integer_column(node.this):
            for side in ("low", "high"):
                value = number(node.args[side])
                if value is not None:
                    node.set(side, value)
                    coerced += 1
    for node in list(tree.find_all(exp.In)):
        if integer_column(node.this) and node.expressions:
            values = [number(e) for e in node.expressions]
            if all(v is not None for v in values):
                node.set("expressions", values)
                coerced += len(values)
    if coerced:
        changes.append(f"{coerced} string literal(s) compared with an INT64 column written as numbers")
    return tree.sql(dialect="bigquery"), sorted(set(changes))


def bigquery_types(schema) -> dict[str, dict[str, str]]:
    """``{TABLE: {COLUMN: type}}`` upper-cased, from the harness's ``TableDef`` objects (adapted BigQuery schema)."""

    return {
        t.name.upper(): {c.upper(): k.split("(")[0] for c, k in t.columns.items()}
        for t in schema.values()
    }


# ---------------------------------------------------------------- the upstream query file


def upstream_queries() -> list[dict]:
    """The 300 entries of ``FIBEN_Queries.json`` (committed unchanged)."""

    return json.loads(
        (FOLDER / "upstream" / "FIBEN_Queries.json").read_text(encoding="utf-8")
    )


def targets(entries: list[dict] | None = None) -> dict[int, dict]:
    """``{uniqueQueryID: the entry holding the target SQL}``: the 237 non-paraphrased entries
    (a paraphrase has its own question and points to its target by ``uniqueQueryID``)."""

    entries = upstream_queries() if entries is None else entries
    out = {e["uniqueQueryID"]: e for e in entries if not e["isParaphrased"]}
    return out


def result_digest(rows) -> str:
    """A digest of a result as a multiset of rows (floats to 9 significant digits), independent of row order."""

    def norm(value):
        if isinstance(value, float):
            return f"{value:.9g}"
        return value

    items = sorted(repr(tuple(norm(v) for v in row)) for row in rows)
    return hashlib.sha256("\n".join(items).encode()).hexdigest()[:16]


def _to_duckdb(sql: str) -> str:
    return sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]


def features(tree: exp.Expression, query_type: str) -> list[str]:
    """What the query exercises, for the workload file."""

    out = [f"upstream type {query_type.lower()}" if query_type else "upstream type"]
    selects = list(tree.find_all(exp.Select))
    if len(selects) > 1:
        out.append("subquery")
    if tree.find(exp.Subquery) is not None and any(
        isinstance(s.parent, (exp.In,)) or isinstance(s.parent, exp.Subquery) and isinstance(s.parent.parent, exp.In)
        for s in selects[1:]
    ):
        out.append("IN subquery")
    if tree.find(exp.Not) is not None and tree.find(exp.In) is not None:
        out.append("NOT IN")
    if tree.find(exp.Having) is not None:
        out.append("HAVING")
    elif tree.find(exp.Group) is not None:
        out.append("GROUP BY")
    if tree.find(exp.Union) is not None:
        out.append("set operation")
    if tree.find(exp.With) is not None:
        out.append("CTE")
    if tree.args.get("limit") is not None:
        out.append("LIMIT")
    joins = sum(len(s.args.get("joins") or []) for s in selects)
    if joins >= 4:
        out.append("5 or more tables joined")
    return out


def limit_determined(con, tree: exp.Expression) -> tuple[bool, str]:
    """Is the result of a query with ``LIMIT n`` fully determined by the data?

    Without ``ORDER BY`` it is, only if the query returns at most ``n`` rows anyway. With it, only if the sort keys
    of row ``n`` and row ``n + 1`` differ: a tie at the cutoff lets the engine keep either row, so a rewrite that
    changes nothing about the query's meaning can still change the rows (and no checker can call that wrong).
    """

    limit = tree.args.get("limit")
    if limit is None:
        return True, ""
    if not isinstance(tree, exp.Select):
        return False, "LIMIT on a set operation, not analysed"
    n = int(limit.expression.name)
    probe = tree.copy()
    probe.set("limit", None)
    order = probe.args.get("order")
    if order is None:
        count = con.execute(f"SELECT COUNT(*) FROM ({_to_duckdb(probe.sql('bigquery'))})").fetchone()[0]
        if count <= n:
            return True, ""
        return False, f"LIMIT {n} without ORDER BY on a query that returns {count} rows"
    aliases = {
        e.alias: e.this for e in tree.expressions if isinstance(e, exp.Alias)
    }
    keys = []
    for ordered in order.expressions:
        key = ordered.this
        if isinstance(key, exp.Literal) and not key.is_string:
            key = tree.expressions[int(key.name) - 1]
        if isinstance(key, exp.Column) and not key.table and key.name in aliases:
            key = aliases[key.name]
        if isinstance(key, exp.Alias):
            key = key.this
        keys.append(key.copy())
    group = probe.args.get("group")
    if group is not None:  # GROUP BY may name an output alias
        for position, item in enumerate(group.expressions):
            if isinstance(item, exp.Column) and not item.table and item.name in aliases:
                group.expressions[position] = aliases[item.name].copy()
            elif isinstance(item, exp.Literal) and not item.is_string:
                target = tree.expressions[int(item.name) - 1]
                group.expressions[position] = (
                    target.this.copy() if isinstance(target, exp.Alias) else target.copy()
                )
    probe.set("expressions", [exp.alias_(k, f"k{i}") for i, k in enumerate(keys)])
    new_order = order.copy()
    for ordered, key in zip(new_order.expressions, keys):
        ordered.set("this", key.copy())
    probe.set("order", new_order)
    probe.set("limit", exp.Limit(expression=exp.Literal.number(n + 1)))
    rows = con.execute(_to_duckdb(probe.sql("bigquery"))).fetchall()
    if len(rows) > n and rows[n - 1] == rows[n]:
        return False, f"LIMIT {n} with a tie in the sort key at the cutoff"
    return True, ""


def build_workload(adapter, con) -> dict:
    """The workload file's content from ``FIBEN_Queries.json`` and the loaded data (needs the data).

    Every one of the 237 targets is either scored (``queries``) or listed with the reason it is not (``not_scored``).
    """

    import re as _re

    types = bigquery_types(adapter.schema())
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    for name in manifest():
        con.execute(f'CREATE VIEW {SCHEMA}."{name}" AS SELECT * FROM main."{name}"')
    scored, skipped = [], []
    seen: dict[str, int] = {}
    for unique_id, entry in sorted(targets().items()):
        sql = entry["SQL"].strip()
        item = {
            "id": f"fb-q{unique_id}",
            "upstream_id": unique_id,
            "question": entry["question"].strip(),
            "query_type": entry["queryType"].lower(),
        }
        if sql in seen:
            skipped.append({**item, "reason": "duplicate", "detail": f"the same SQL text as upstream id {seen[sql]}"})
            continue
        seen[sql] = unique_id
        try:
            original = con.execute(sql).fetchall()
        except Exception as error:  # noqa: BLE001
            skipped.append({**item, "reason": "broken upstream", "detail": f"does not run as written: {str(error).splitlines()[0][:100]}"})
            continue
        adapted, changes = adapt_query(sql, types)
        try:
            got = con.execute(_to_duckdb(adapted)).fetchall()
        except Exception as error:  # noqa: BLE001
            skipped.append({**item, "reason": "adaptation", "detail": f"the adapted query does not run: {str(error).splitlines()[0][:100]}", "sql": adapted})
            continue
        if result_digest(got) != result_digest(original):
            skipped.append({**item, "reason": "adaptation", "detail": "the adapted query returns different rows", "sql": adapted})
            continue
        if not original:
            skipped.append({**item, "reason": "empty result", "detail": "returns no row on the data: a rewrite that keeps the empty result is not shown right", "sql": adapted, "adaptation": "; ".join(changes)})
            continue
        determined, why = limit_determined(con, sqlglot.parse_one(adapted, read="bigquery"))
        if not determined:
            skipped.append({**item, "reason": "result not determined", "detail": why, "sql": adapted, "adaptation": "; ".join(changes)})
            continue
        scored.append(
            {
                **item,
                "origin": "upstream-query",
                "adaptation": "; ".join(changes) or "none",
                "features": features(sqlglot.parse_one(adapted, read="bigquery"), entry["queryType"]),
                "rows": len(original),
                "digest": result_digest(original),
                "sql": adapted,
            }
        )
    return {"queries": scored, "not_scored": skipped}


def check_workload(adapter, con) -> list[str]:
    """Problems of the stored workload on the loaded data: the adapted queries must return the recorded rows
    (count and digest of the original query's result), and ``FIBEN_Queries.json`` must be as upstream's README says."""

    problems = []
    entries = upstream_queries()
    counts = {
        "queries": len(entries),
        "distinct targets": len(targets(entries)),
        "nested": sum(e["queryType"].lower() != "non-nested" for e in entries),
    }
    if counts != {"queries": 300, "distinct targets": 237, "nested": 170}:
        problems.append(f"FIBEN_Queries.json has {counts}, upstream's README says 300 questions, 237 targets, 170 nested")
    for query in adapter.workload():
        if query["origin"] != "upstream-query":
            continue
        try:
            rows = con.execute(_to_duckdb(query["sql"])).fetchall()
        except Exception as error:  # noqa: BLE001
            problems.append(f"{query['id']}: does not run: {str(error).splitlines()[0][:80]}")
            continue
        if len(rows) != query["rows"] or result_digest(rows) != query["digest"]:
            problems.append(f"{query['id']}: returns {len(rows)} rows, the recorded original returns {query['rows']} (digest differs)")
    return problems


def main(argv: list[str] | None = None) -> int:
    """``--build``: rewrite ``adapted/schema.sql``, ``data_manifest.json`` and ``workload.json`` from upstream and the data."""

    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--build-schema", action="store_true")
    parser.add_argument("--build-manifest", action="store_true")
    parser.add_argument("--build-workload", action="store_true")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT / "tools"))
    import sample_db_bench as bench

    if args.build_schema:
        text = adapted_schema_text(
            (FOLDER / "upstream" / "FIBEN.sql").read_text(encoding="utf-8"), bench.read_ddl
        )
        (FOLDER / "adapted" / "schema.sql").write_text(text, encoding="utf-8")
    if args.build_manifest:
        folder = data_dir_unchecked()
        files = {}
        for table in (FOLDER / "upstream" / "tablelist.txt").read_text().split():
            path = folder / f"{table}.csv"
            files[table] = {"bytes": path.stat().st_size, "sha256": sha256_file(path), "rows": line_count(path)}
        (FOLDER / "data_manifest.json").write_text(
            json.dumps({"note": MANIFEST_NOTE, "archive": {"bytes": ARCHIVE_BYTES, "sha256": ARCHIVE_SHA256}, "files": files}, indent=1) + "\n",
            encoding="utf-8",
        )
    if args.build_workload:
        adapter = bench.ADAPTERS["fiben"]
        con = adapter.connect()
        built = build_workload(adapter, con)
        previous = json.loads((FOLDER / "workload.json").read_text(encoding="utf-8")) if (FOLDER / "workload.json").exists() else {}
        authored = [q for q in previous.get("queries", []) if q["origin"] == "authored"]
        out = {"about": WORKLOAD_ABOUT, "queries": built["queries"] + authored, "not_scored": built["not_scored"]}
        (FOLDER / "workload.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    return 0


MANIFEST_NOTE = (
    "data.zip of IBM/fiben-benchmark at the pinned commit: size, SHA-256 and row count (newline count) of each of the "
    "152 CSV files in the archive; generated by tools/sample_db_fiben.py --build-manifest"
)
WORKLOAD_ABOUT = (
    "FIBEN workload for tools/sample_db_bench.py. origin 'upstream-query': the SQL targets of upstream/FIBEN_Queries.json "
    "(upstream_id = its uniqueQueryID), adapted from Db2 SQL to BigQuery as 'adaptation' says; rows and digest are the row count "
    "and the result digest of the ORIGINAL query run on the data, which the adapted query must reproduce. 'authored': written for "
    "this eval. 'not_scored': upstream targets left out, each with the reason. Generated by tools/sample_db_fiben.py --build-workload."
)


def data_dir_unchecked() -> Path:
    """The extracted CSV folder, downloaded if needed, without the manifest check (the manifest is built from it)."""

    out = CACHE / "data"
    archive = _download()
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for table in (FOLDER / "upstream" / "tablelist.txt").read_text().split():
            target = out / f"{table}.csv"
            if not target.exists():
                with zf.open(f"data/{table}.csv") as source, target.open("wb") as sink:
                    for block in iter(lambda: source.read(1 << 20), b""):
                        sink.write(block)
    return out


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
