"""Replay the witnesses of a refutation sweep on a second engine (sqlite3) to rule out a judge bug.

The sweep (``tools/refutation_sweep.py``) logs every database on which counterexample synthesis
made two queries differ; DuckDB (the replay judge) said so. This script loads each logged witness
into an in-memory SQLite database, runs both queries there (translated with sqlglot), and compares
the result bags. A witness SQLite reproduces is confirmed by a second engine. A witness on which
SQLite returns the same bag is a candidate judge bug and is listed for a manual look. A query SQLite
cannot run (no translation, a function it lacks) is reported as ``cannot_run`` and not counted either way.

    python tools/refutation_sweep_recheck.py out/*.jsonl --json recheck.json

SQLite differs from BigQuery and DuckDB in places that matter here (integer division, string
comparison, NULL ordering inside ARRAY_AGG), so ``same`` means "look at it", not "the judge is wrong".
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import sqlite3
import sys
from pathlib import Path

import sqlglot

AFFINITY = (
    (("INT", "SERIAL"), "INTEGER"),
    (("CHAR", "TEXT", "STRING", "CLOB", "UUID"), "TEXT"),
    (("FLOA", "DOUB", "REAL", "NUMERIC", "DEC", "NUMBER"), "REAL"),
    (("BOOL",), "INTEGER"),
)


def affinity(declared: str) -> str:
    upper = declared.upper()
    for needles, kind in AFFINITY:
        if any(n in upper for n in needles):
            return kind
    return "TEXT"  # dates and timestamps are stored as ISO text


def value(v):
    if isinstance(v, bool):
        return int(v)
    return v


def comparable(v):
    """A cell as a hashable value: integral floats read as ints, floats rounded to 6 significant digits."""

    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if v == int(v) and abs(v) < 1e15:
            return int(v)
        return float(f"{v:.6g}")
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, str):
        return v
    return v


def bag(rows):
    return collections.Counter(tuple(comparable(c) for c in row) for row in rows)


def flat(name: str) -> str:
    """``proj.dataset.table`` as one SQLite table name."""

    return name.replace("`", "").replace(".", "__")


def translate(sql: str, dialect: str) -> str:
    """The query in SQLite's dialect, with every dotted table name folded into one identifier."""

    from sqlglot import exp

    tree = sqlglot.parse_one(sql, read=dialect)
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if isinstance(table.this, exp.Identifier) and (table.db or table.catalog):
            joined = ".".join(p for p in (table.catalog, table.db, table.name) if p)
            table.set("catalog", None)
            table.set("db", None)
            table.set("this", exp.to_identifier(flat(joined)))
        elif table.name.lower() in ctes:
            continue
    return tree.sql(dialect="sqlite")


def replay(record: dict) -> tuple[str, str]:
    """``(verdict, detail)``: ``reproduced``, ``same`` or ``cannot_run``."""

    dialect = record["dialect"]
    types = {t.lower(): {c.lower(): ty for c, ty in cols.items()} for t, cols in record.get("types", {}).items()}
    try:
        queries = [translate(record["left"], dialect), translate(record["right"], dialect)]
    except Exception as error:  # noqa: BLE001
        return "cannot_run", f"translation: {str(error)[:120]}"
    con = sqlite3.connect(":memory:")
    try:
        for table, rows in record["witness"].items():
            columns = record["schema"].get(table) or list(types.get(table.lower(), {}))
            if not columns:
                continue
            column_types = types.get(table.lower(), {})
            spelled = ", ".join(f'"{c}" {affinity(column_types.get(c.lower(), "INT"))}' for c in columns)
            name = flat(table)
            con.execute(f'CREATE TABLE "{name}" ({spelled})')
            for row in rows:
                con.execute(f'INSERT INTO "{name}" VALUES ({", ".join("?" * len(columns))})', [value(v) for v in row])
        results = []
        for sql in queries:
            try:
                results.append(con.execute(sql).fetchall())
            except sqlite3.Error as error:
                return "cannot_run", f"sqlite: {str(error)[:120]}"
    finally:
        con.close()
    if bag(results[0]) != bag(results[1]):
        return "reproduced", ""
    return "same", f"left {len(results[0])} rows, right {len(results[1])} rows"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+")
    parser.add_argument("--json", help="write [{record index, file, verdict, detail}] here")
    args = parser.parse_args(argv)
    out = []
    totals: collections.Counter = collections.Counter()
    for name in args.files:
        for number, line in enumerate(Path(name).read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            record = json.loads(line)
            verdict, detail = replay(record)
            totals[verdict] += 1
            out.append({"file": Path(name).name, "line": number, "eval_file": record["eval_file"], "test": record["test"], "verdict": verdict, "detail": detail})
    print(dict(totals))
    for row in out:
        if row["verdict"] == "same":
            print(f"same: {row['file']}:{row['line']} {row['test']} {row['detail']}")
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
