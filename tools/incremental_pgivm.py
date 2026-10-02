"""Extract pg_ivm's regression workloads and adapt them to Dataform incremental models.

pg_ivm (https://github.com/sraoss/pg_ivm, PostgreSQL License) tests incremental
view maintenance with SQL scripts: create a view over base tables, then run
inserts, updates and deletes and compare the view with a full recomputation.
This script reads ``sql/pg_ivm.sql`` and ``sql/outer_join.sql`` from a checkout
and writes ``tests/fixtures/incremental/pgivm_cases.json``:

* ``original``: the view query, the statements that ran before it was created and
  the statements that ran after, verbatim (Postgres SQL);
* ``adapted``: the same workload as a DuckDB script plus the Dataform model it is
  checked against, or ``unsupported`` with the reason (data-modifying CTEs,
  subqueries or CTEs in FROM, set operations, functions, ...).

Adapted model: pg_ivm maintains a view through its own engine, which Dataform
does not have. The Dataform counterpart is the common append pattern: every
source table gains a ``_loaded_at`` load timestamp (assigned in insertion order),
the model projects the first table's ``_loaded_at`` (``MAX`` of it for an
aggregate) as ``__loaded_at`` and each incremental run reads only rows past
``MAX(__loaded_at)`` of its own table. Whether that stays equal to the full query
is exactly the question the eval asks; most pg_ivm workloads (updates, deletes,
changes to the joined tables) make it diverge.

    python tools/incremental_pgivm.py PGIVM_CHECKOUT      # pinned: v1.16, 22b4b45
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

import sqlglot
from sqlglot import exp

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "incremental" / "pgivm_cases.json"
FILES = ("sql/pg_ivm.sql", "sql/outer_join.sql")
LOADED = "_loaded_at"
TS_DEFAULT = "(TIMESTAMP '2024-01-01' + to_hours(CAST(nextval('_load_seq') AS INTEGER)))"


def split_statements(text: str) -> list[tuple[int, str]]:
    """``(line, statement)`` pairs; psql meta lines and comments are dropped."""

    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 1
    quote: str | None = None
    dollar = False
    i, line = 0, 1
    at_line_start = True
    while i < len(text):
        c = text[i]
        if at_line_start and c == "\\" and not quote and not dollar and not buf:
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        at_line_start = c == "\n"
        if c == "\n":
            line += 1
        if not quote and not dollar and text.startswith("--", i):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        if not quote and text.startswith("$$", i):
            dollar = not dollar
            buf.append("$$")
            i += 2
            continue
        if dollar:
            buf.append(c)
        elif quote:
            buf.append(c)
            if c == quote:
                quote = None
        elif c == "'" or c == '"':
            quote = c
            buf.append(c)
        elif c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append((start, stmt))
            buf = []
            start = line
        else:
            if not buf and not c.isspace():
                start = line
            buf.append(c)
        i += 1
    return out


def _pg_type(raw: str) -> str:
    raw = raw.lower()
    if raw.startswith(("int", "bigint", "smallint", "serial")):
        return "INT64"
    if raw.startswith(("text", "varchar", "char")):
        return "STRING"
    if raw.startswith(("numeric", "float", "double", "real", "decimal")):
        return "FLOAT64"
    if raw.startswith("bool"):
        return "BOOL"
    return "INT64"


def _create_table(stmt: str):
    match = re.match(r"CREATE\s+TABLE\s+([\w.\"]+)\s*\((.*)\)\s*$", stmt, re.I | re.S)
    if not match:
        return None
    columns: dict[str, str] = {}
    depth, part, parts = 0, "", []
    for c in match.group(2):
        depth += c == "("
        depth -= c == ")"
        if c == "," and depth == 0:
            parts.append(part)
            part = ""
        else:
            part += c
    parts.append(part)
    for p in parts:
        words = p.split()
        if not words or words[0].upper() in {"PRIMARY", "FOREIGN", "UNIQUE", "CONSTRAINT", "CHECK"}:
            continue
        columns[words[0].strip('"')] = _pg_type(words[1]) if len(words) > 1 else "INT64"
    return match.group(1).strip('"').lower(), columns


def _to_duckdb_dml(stmt: str, tables: dict[str, dict[str, str]]) -> str | None:
    """A Postgres DML statement as DuckDB SQL, or None if it is not plain DML on a known table."""

    if re.match(r"TRUNCATE\b", stmt, re.I):
        name = re.sub(r"TRUNCATE\s+(TABLE\s+)?", "", stmt, flags=re.I).strip().lower()
        return f'DELETE FROM "{name}"' if name in tables else None
    try:
        tree = sqlglot.parse_one(stmt, read="postgres")
    except sqlglot.errors.SqlglotError:
        return None
    if isinstance(tree, exp.Insert):
        target = tree.this.this.name if isinstance(tree.this, exp.Schema) else tree.this.name
        if target.lower() not in tables:
            return None
        if not isinstance(tree.this, exp.Schema):
            tree.set("this", exp.Schema(this=tree.this, expressions=[exp.to_identifier(c) for c in tables[target.lower()]]))
        return tree.sql(dialect="duckdb")
    if isinstance(tree, (exp.Update, exp.Delete)) and tree.this.name.lower() in tables:
        return tree.sql(dialect="duckdb")
    return None


def _model_sql(query: str, first_alias: str, aggregate: bool) -> tuple[str, str] | str:
    """``(full, incremental)`` Postgres SQL for the append model, or a reason it is unsupported."""

    try:
        tree = sqlglot.parse_one(query, read="postgres")
    except sqlglot.errors.SqlglotError as exc:
        return f"parse: {str(exc)[:60]}"
    if not isinstance(tree, exp.Select):
        return "set operation"
    if tree.args.get("with_") or tree.args.get("with"):
        return "CTE"
    source = tree.args.get("from_") or tree.args.get("from")
    if source is None or not isinstance(source.this, exp.Table):
        return "no plain base table in FROM"
    joins = tree.args.get("joins") or []
    if any(not isinstance(j.this, exp.Table) for j in joins):
        return "subquery or function in FROM"
    if any(isinstance(n, (exp.Subquery, exp.Exists)) for n in tree.walk() if n is not tree):
        return "subquery expression"
    if tree.args.get("distinct"):
        return "DISTINCT"
    driving = source.this.alias or source.this.name
    column = exp.column(LOADED, table=driving)
    aggregate = any(isinstance(n, exp.AggFunc) for n in tree.walk())
    tree.append("expressions", exp.alias_(exp.Max(this=column) if aggregate else column, "__loaded_at"))
    full = tree.sql(dialect="postgres")
    incremental = tree.copy()
    condition = exp.GT(this=exp.column(LOADED, table=driving), expression=sqlglot.parse_one(
        "COALESCE((SELECT MAX(__loaded_at) FROM __SELF__), TIMESTAMP '1970-01-01')", read="postgres"))
    incremental.where(condition, append=True, copy=False)
    return full, incremental.sql(dialect="postgres")


def extract(checkout: Path) -> list[dict]:
    cases: list[dict] = []
    for relative in FILES:
        text = (checkout / relative).read_text(encoding="utf-8")
        statements = split_statements(text)
        globals_: dict[str, dict[str, str]] = {}
        tables = dict(globals_)
        history: list[tuple[str, str]] = []  # (original, duckdb) of DML so far
        committed: list[tuple[str, str]] = []  # DML outside any transaction
        live: list[dict] = []  # views created in this transaction
        savepoints: dict[str, tuple[int, dict[int, int]]] = {}
        in_txn = False
        tainted = False

        def emit(view: dict, end: str) -> None:
            if not view["after"]:
                return
            cases.append(_case(relative, view, tables, end))

        def finish_txn() -> None:
            nonlocal tables, history, live, savepoints, in_txn, tainted
            for view in live:
                emit(view, "end")
            tables = dict(globals_)
            history, live, savepoints, in_txn, tainted = list(committed), [], {}, False, False

        for line, stmt in statements:
            head = stmt.split(None, 2)
            keyword = head[0].upper() if head else ""
            if keyword == "BEGIN":
                finish_txn()
                in_txn = True
            elif keyword in {"ROLLBACK", "COMMIT", "END"} and len(head) >= 2 and head[1].upper() == "TO":
                name = head[-1].lower()
                if name in savepoints:
                    for view in live:
                        emit(view, f"rollback to {name}")
                    keep_history, keep_after = savepoints[name]
                    history = history[:keep_history]
                    for view in live:
                        view["after"] = view["after"][: keep_after.get(id(view), 0)]
                        view["tainted"] = False
                    tainted = False
            elif keyword in {"ROLLBACK", "COMMIT", "END"}:
                finish_txn()
            elif keyword == "SAVEPOINT":
                savepoints[head[1].lower()] = (len(history), {id(v): len(v["after"]) for v in live})
            elif keyword == "CREATE" and re.match(r"CREATE\s+TABLE", stmt, re.I):
                parsed = _create_table(stmt)
                if parsed:
                    tables[parsed[0]] = parsed[1]
                    if not in_txn:
                        globals_[parsed[0]] = parsed[1]
            elif keyword == "ALTER" and re.search(r"DROP\s+COLUMN", stmt, re.I):
                match = re.match(r"ALTER\s+TABLE\s+(\w+)\s+DROP\s+COLUMN\s+(\w+)", stmt, re.I)
                if match and match.group(1).lower() in tables:
                    tables[match.group(1).lower()].pop(match.group(2), None)
                    if not in_txn:
                        globals_[match.group(1).lower()] = tables[match.group(1).lower()]
            elif keyword == "SELECT" and "create_immv" in stmt:
                found = re.match(r"SELECT\s+pgivm\.create_immv\(\s*'([^']*)'\s*,\s*'((?:[^']|'')*)'\s*\)\s*$", stmt, re.I | re.S)
                if found:
                    live.append({
                        "name": found.group(1), "query": found.group(2).replace("''", "'"), "line": line,
                        "initial": list(history), "after": [], "tainted": False,
                    })
            elif keyword in {"INSERT", "UPDATE", "DELETE", "TRUNCATE"} or (keyword == "WITH" and re.search(r"\b(INSERT|UPDATE|DELETE)\b", stmt, re.I)):
                converted = None if keyword == "WITH" else _to_duckdb_dml(stmt, tables)
                history.append((stmt, converted))
                if not in_txn:
                    committed.append((stmt, converted))
                for view in live:
                    view["after"].append((stmt, converted))
        finish_txn()
    return cases


def _case(relative: str, view: dict, tables: dict[str, dict[str, str]], end: str) -> dict:
    case = {
        "id": f"pg_ivm/{Path(relative).stem}:{view['line']}/{view['name'].split('(')[0]}/{len(view['after'])}",
        "origin": "adapted",
        "source": {"repository": "https://github.com/sraoss/pg_ivm", "tag": "v1.16", "file": relative, "line": view["line"], "licence": "PostgreSQL License"},
        "original": {
            "view": view["name"], "query": view["query"],
            "initial": [s for s, _ in view["initial"]], "after": [s for s, _ in view["after"]], "ends": end,
        },
    }
    reason = None
    if any(d is None for _, d in view["initial"] + view["after"]):
        reason = "statement not plain DML on a base table (data-modifying CTE or other)"
    else:
        try:
            names = {t.name.lower() for t in sqlglot.parse_one(view["query"], read="postgres").find_all(exp.Table)}
        except sqlglot.errors.SqlglotError:
            names = set()
        base = {n: tables[n] for n in names if n in tables}
        if not base or len(base) != len(names):
            reason = "view reads something other than a base table"
        else:
            first = None
            built = None
            try:
                tree = sqlglot.parse_one(view["query"], read="postgres")
                src = tree.args.get("from_") or tree.args.get("from")
                first = src.this.alias or src.this.name if src is not None and isinstance(src.this, exp.Table) else None
            except sqlglot.errors.SqlglotError:
                pass
            built = _model_sql(view["query"], first or "", False) if first else "no FROM table"
            if isinstance(built, str):
                reason = built
            else:
                case["adapted"] = {
                    "tables": {n: {"columns": cols} for n, cols in base.items()},
                    "initial": [d for _, d in view["initial"] if _touches(d, base)],
                    "batches": [[d] for _, d in view["after"] if _touches(d, base)],
                    "full_sql": built[0], "incremental_sql": built[1], "dialect": "postgres",
                    "load_column": LOADED, "load_default": TS_DEFAULT,
                }
    if reason:
        case["unsupported"] = reason
    return case


def _touches(duck_sql: str, base: dict) -> bool:
    match = re.search(r'(?:INSERT INTO|UPDATE|DELETE FROM)\s+"?(\w+)"?', duck_sql, re.I)
    return bool(match and match.group(1).lower() in base)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    checkout = Path(sys.argv[1])
    commit = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    cases = extract(checkout)
    for n, case in enumerate(cases):
        case["source"]["commit"] = commit
        case["id"] += f"#{n}"
    # drop exact duplicates (same view, same workload), keep provenance of the first
    seen, unique = set(), []
    for case in cases:
        key = json.dumps([case["original"]["query"], case["original"]["initial"], case["original"]["after"]], sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(case)
    OUT.write_text(json.dumps({"version": 1, "cases": unique}, indent=1) + "\n", encoding="utf-8")
    ok = sum("adapted" in c for c in unique)
    print(f"{len(cases)} workloads, {len(unique)} after de-duplication, {ok} adapted, {len(unique) - ok} unsupported -> {OUT}")


if __name__ == "__main__":
    main()
