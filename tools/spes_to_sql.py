"""Extract the SPES Calcite pairs that SQLSolver's Calcite fixture does not already hold.

SPES (https://github.com/georgia-tech-db/spes, Apache-2.0) ships 232 Calcite
RelOptRulesTest pairs in ``testData/calcite_tests.json``; SQLSolver's
``tests/fixtures/sqlsolver/calcite_pairs.txt`` is the same list in the same
order, with some queries edited.  A SPES pair is dropped when SQLSolver has it
after whitespace/case/trailing-semicolon normalisation, or after table aliases
are renamed in order of appearance (either orientation).  The rest are parsed
with sqlglot (``read="mysql"``), transpiled to DuckDB and run on SQLSolver's Calcite schema
(``tests/fixtures/sqlsolver/calcite.schema.sql``), on an empty and on random
databases; a pair that fails to parse or run, or whose two sides disagree on a
random database, is listed in ``spes_only_skipped.jsonl`` with the reason.

    python tools/spes_to_sql.py --src /path/to/spes
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import random
import re
import sys

import sqlglot

sys.path.insert(0, str(Path(__file__).resolve().parent))
import calcite_corpora as cc  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from kumosql.duckdb_load import small_database  # noqa: E402

OUT = cc.FIXTURES / "spes"
SCHEMA = cc.FIXTURES / "sqlsolver" / "calcite.schema.sql"
STRINGS = ["a", "b", "foo", "abc", "Charlie", "Bill", "SALES", "Clerk"]


def schema_tables() -> tuple[list[str], list[dict]]:
    text = SCHEMA.read_text(encoding="utf-8")
    statements = [s for s in sqlglot.parse(text, read="mysql") if s is not None]
    tables = []
    for st in statements:
        schema = st.this
        cols, pk = [], set()
        for item in schema.expressions:
            if not isinstance(item, sqlglot.exp.ColumnDef):
                continue
            cons = " ".join(c.sql(dialect="mysql").upper() for c in item.args.get("constraints") or [])
            ref = re.search(r"REFERENCES\s+`?(\w+)`?\s*\(`?(\w+)`?\)", cons)
            if "PRIMARY KEY" in cons:
                pk.add(item.name)
            cols.append({"name": item.name, "type": item.args["kind"].sql(dialect="mysql").upper(),
                         "nullable": "NOT NULL" not in cons and "PRIMARY KEY" not in cons,
                         "references": (ref.group(1), ref.group(2)) if ref else None})
        tables.append({"name": schema.this.name, "columns": cols, "pk": pk})
    order = sorted(tables, key=lambda t: any(c["references"] for c in t["columns"]))
    return [s.sql(dialect="duckdb") for s in statements], order


def fill(con, tables: list[dict], rng: random.Random) -> None:
    keys: dict[tuple[str, str], list] = {}
    for t in tables:
        rows = []
        for i in range(rng.randint(0, 4)):
            row = []
            for c in t["columns"]:
                if c["name"] in t["pk"]:
                    v = [0, 10, 20, 30, 40][i]
                elif c["references"]:
                    pool = keys.get((c["references"][0].lower(), c["references"][1].lower()), [])
                    if not pool:
                        row = None
                        break
                    v = rng.choice(pool)
                elif c["nullable"] and rng.random() < 0.25:
                    v = None
                elif "CHAR" in c["type"] or "TEXT" in c["type"]:
                    v = rng.choice(STRINGS)
                elif "TINYINT" in c["type"]:
                    v = rng.choice([0, 1])
                else:
                    v = rng.choice([0, 1, 2, 5, 10, 20, 30, 100, 1000])
                row.append(v)
            if row is not None:
                rows.append(tuple(row))
        for c in t["columns"]:
            keys[(t["name"].lower(), c["name"].lower())] = [r[t["columns"].index(c)] for r in rows]
        if rows:
            ph = ", ".join("?" for _ in t["columns"])
            con.executemany(f'INSERT INTO "{t["name"]}" VALUES ({ph})', rows)


def duckdb_sql(sql: str) -> str:
    """The DuckDB spelling of a fixture query: sqlglot transpile, Calcite's ``$f0`` quoted."""

    out = sqlglot.transpile(sql, read="mysql", write="duckdb")[0]
    return re.sub(r'(?<![\w"])(\$\w+)', r'"\1"', out)


def check(ddl: list[str], tables: list[dict], q1: str, q2: str, trials: int = 40) -> str | None:
    import duckdb

    for q in (q1, q2):
        if "||" in q:
            return ("uses `||` (string concatenation in Calcite), which sqlglot's mysql "
                    "dialect reads as OR")
        try:
            sqlglot.parse_one(q, read="mysql")
        except Exception as exc:  # noqa: BLE001
            return f"sqlglot(mysql) cannot parse: {str(exc).splitlines()[0][:200]}"
    q1, q2 = (duckdb_sql(q) for q in (q1, q2))
    rng = random.Random(0)
    bad = 0
    for trial in range(trials + 1):
        con = small_database()
        for s in ddl:
            con.execute(s)
        if trial:
            fill(con, tables, rng)
        try:
            a = sorted(map(repr, con.execute(q1).fetchall()))
            b = sorted(map(repr, con.execute(q2).fetchall()))
        except Exception as exc:  # noqa: BLE001
            where = "empty database" if not trial else "random database"
            return f"duckdb fails on {where}: {str(exc).splitlines()[0][:200]}"
        finally:
            con.close()
        bad += a != b
    if bad:
        return (f"Calcite pair, but {bad} of {trials} random DuckDB databases on SQLSolver's "
                "Calcite schema give different results")
    return None


SEMI_JOIN_NOTE = (
    "SPES's text replaces the semi-join with an inner join on EMP rows that are not de-duplicated, so each "
    "DEPT row repeats once per matching employee: not equivalent under bag semantics (nothing makes EMP.DEPTNO unique)."
)
# Pairs whose SPES text is not equivalent although the Calcite rule is: the reason, kept in the fixture.
NOT_EQUIVALENT = {
    name: SEMI_JOIN_NOTE
    for name in ("testSemiJoinRule", "testSemiJoinRuleExists", "testSemiJoinTrim")
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, required=True, help="SPES checkout")
    args = ap.parse_args()
    commit = cc.git_head(args.src)
    spes = cc.load_spes(args.src)
    ss = cc.load_sqlsolver_calcite()
    plain = {(cc.norm(a), cc.norm(b)) for a, b in ss}
    alias = {(cc.norm_aliases(a), cc.norm_aliases(b)) for a, b in ss}
    ddl, tables = schema_tables()
    kept, skipped, dropped = [], [], {"normalised_text": 0, "alias_renaming": 0}
    for r in spes:
        k = (cc.norm(r["q1"]), cc.norm(r["q2"]))
        if k in plain or k[::-1] in plain:
            dropped["normalised_text"] += 1
            continue
        k = (cc.norm_aliases(r["q1"]), cc.norm_aliases(r["q2"]))
        if k in alias or k[::-1] in alias:
            dropped["alias_renaming"] += 1
            continue
        rec = {"name": r["name"], "spes_index": r["index"], "label": "equivalent",
               "sql_a": r["q1"], "sql_b": r["q2"]}
        if r["name"] in NOT_EQUIVALENT:
            rec["label"], rec["label_note"] = "not_equivalent", NOT_EQUIVALENT[r["name"]]
        if r["spes_name"] != r["name"]:
            rec["spes_name"] = r["spes_name"]
        rec["sqlsolver_index"] = r["index"]  # same test, edited text, in SQLSolver
        reason = check(ddl, tables, r["q1"], r["q2"])
        if reason:
            skipped.append({**rec, "category": cc.skip_category(reason), "reason": reason})
        else:
            kept.append(rec)
    OUT.mkdir(parents=True, exist_ok=True)
    for fname, rows in (("spes_only_pairs.jsonl", kept), ("spes_only_skipped.jsonl", skipped)):
        with (OUT / fname).open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {"source": "https://github.com/georgia-tech-db/spes", "commit": commit,
               "spes_pairs": len(spes), "already_in_sqlsolver": dropped,
               "spes_only": len(kept) + len(skipped), "kept": len(kept),
               "skipped": len(skipped),
               "skipped_by_category": dict(sorted(Counter(s["category"] for s in skipped).items())),
               "schema": "tests/fixtures/sqlsolver/calcite.schema.sql"}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    for s in skipped:
        print(f"skip {s['spes_index']} {s['name']}: {s['reason']}")


if __name__ == "__main__":
    main()
