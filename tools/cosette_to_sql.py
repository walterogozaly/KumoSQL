"""Convert the Cosette example corpus (``.cos`` files) into SQL fixture pairs.

Cosette (https://github.com/uwdb/Cosette, BSD-2-Clause) states each problem in
a small DSL::

    schema s(a:int, b:int, ??);   -- ``??``: more columns, unknown
    table r(s);
    predicate b(s);               -- uninterpreted predicate over a row of s
    query q1 `select * from r x where b(x)`;
    query q2 `...`;
    verify q1 q2;

Each file becomes one case ``{name, source_dir, label, schema, constraints,
sql_a, sql_b, ...}`` in ``tests/fixtures/cosette/cosette_cases.jsonl``; files
that cannot be translated faithfully go to ``cosette_skipped.jsonl`` with the
reason.  Translation rules (see ``tests/fixtures/cosette/README.md``):

* every declared column is ``NOT NULL`` (Cosette's semantics has no NULL);
  ``int`` and the generic types ``ty``, ``ty0`` ... become ``INTEGER``,
  ``str``/``string`` become ``VARCHAR``;
* an open schema (``??``) keeps only its declared columns;
* a unary predicate ``b(s)`` becomes a hidden ``BOOLEAN NOT NULL`` column
  ``__b`` on every table of schema ``s`` and ``b(x)`` becomes ``x.__b``;
  predicates over several rows are not translated;
* labels: ``sqlrewrites`` and ``calcite`` are equivalent, ``inequal_queries``
  not equivalent, ``conditional`` equivalent only under the constraints listed
  in ``CONDITIONAL`` below (taken from ``examples/conditional/conditional.md``
  and the ``.cos`` comments); conditional files whose precondition the source
  does not state, or states in a form keys cannot express, are skipped.

Every pair is parsed with sqlglot (``read="mysql"``) and run in DuckDB on an
empty and on random small databases; a failure is a skip.

    python tools/cosette_to_sql.py --src /path/to/Cosette
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import random
import re
import subprocess
import sys

import sqlglot

sys.path.insert(0, str(Path(__file__).resolve().parent))
from calcite_corpora import skip_category  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "cosette"

FOLDER_LABELS = {
    "sqlrewrites": "equivalent",
    "calcite": "equivalent",
    "inequal_queries": "not_equivalent",
    "conditional": "conditional",
    "to_be_supported": "equivalent",
}

TYPE_MAP = {"int": "INTEGER", "str": "VARCHAR", "string": "VARCHAR"}

# Conditional examples: the constraints under which the pair is equivalent.
# Only preconditions stated in the source are recorded; anything else is a skip.
CONDITIONAL = {
    "ex1sigmod92": {
        "constraints": [{"kind": "primary_key", "table": "itm", "columns": ["itemn"]}],
        "source": "conditional.md item 6: 'Preconditon: itemn is the primary key of itm'; "
        ".cos comment 'itemn is a key'",
    },
    "ex2sigmod83": {
        "constraints": [{"kind": "primary_key", "table": "r2", "columns": ["a"]}],
        "source": "conditional.md item 5: 'Here we assume that A is the key'; "
        ".cos comment 'a is primary key' on r2",
    },
    "ex2sigmod92": {
        "constraints": [{"kind": "primary_key", "table": "itm", "columns": ["itemno"]}],
        "source": "conditional.md item 7: 'Assume itemno is the primary key of itm'",
    },
    "ex2sigmod92simpl": {
        "constraints": [{"kind": "primary_key", "table": "itm", "columns": ["itemno"]}],
        "source": "simplified ex2sigmod92 (same file header, same schemas); "
        "conditional.md item 7: 'Assume itemno is the primary key of itm'",
    },
    "ex3sigmod92": {
        "constraints": [{"kind": "primary_key", "table": "itp", "columns": ["itemn"]}],
        "source": "conditional.md item 8: 'itemn is a key of itp'; .cos comment",
    },
}
CONDITIONAL_SKIPS = {
    "fkPennTR": "precondition not expressible as keys: conditional.md items 1-2 say the "
    "queries agree only if 'Security' uses only its own employees on the projects it "
    "runs (beyond the TMember foreign key and Empl key); the queries also compare the "
    "int column DName with the string 'Security'",
    "index_sigmod82": "precondition not stated: conditional.md item 4 gives no key, and "
    "without one (e.g. payroll.ssno unique) the bag multiplicities differ",
    "inline-exists": "precondition not stated: not described in conditional.md and the "
    ".cos file gives no key; the EXISTS-to-join rewrite needs one",
    "missing-pred": "precondition not stated: not described in conditional.md and the "
    ".cos file gives no constraint under which the predicates agree",
}
# Source labels contradicted by a direct argument; listed, not shipped.
LABEL_DISPUTES = {
    ("inequal_queries", "344Q1"): "folder says not equivalent, but q1's extra table w is "
    "satisfied by w := v (x.usrUid = v.picUid and v.picSize = v.picSize), so under "
    "DISTINCT q1 = q2 for every database; label contradicted",
}


class Skip(Exception):
    pass


def strip_comments(text: str) -> str:
    """Drop ``--`` comments outside backtick-quoted SQL."""

    out, in_sql = [], False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "`":
            in_sql = not in_sql
            out.append(ch)
        elif not in_sql and text.startswith("--", i):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def parse_cos(text: str) -> dict:
    text = strip_comments(text)
    schemas, tables, predicates, queries, verify = {}, {}, {}, {}, None
    for m in re.finditer(r"\bquery\s+(\w+)\s*`([^`]*)`", text):
        queries[m.group(1)] = m.group(2).strip()
    rest = re.sub(r"`[^`]*`", "``", text)
    for m in re.finditer(r"\bschema\s+(\w+)\s*\(([^)]*)\)", rest):
        cols, is_open = [], False
        for item in [p.strip() for p in m.group(2).split(",") if p.strip()]:
            if item == "??":
                is_open = True
                continue
            if ":" not in item:
                raise Skip(f"unreadable schema item {item!r}")
            name, typ = (s.strip() for s in item.split(":", 1))
            cols.append((name, typ))
        schemas[m.group(1)] = {"columns": cols, "open": is_open}
    for m in re.finditer(r"\btable\s+(\w+)\s*\(\s*(\w+)\s*\)", rest):
        tables[m.group(1)] = m.group(2)
    for m in re.finditer(r"\bpredicate\s+(\S+?)\s*\(([^)]*)\)", rest):
        predicates[m.group(1)] = [a.strip() for a in m.group(2).split(",")]
    m = re.search(r"\bverify\s+(\w+)\s+(\w+)", rest)
    if m:
        verify = (m.group(1), m.group(2))
    return {"schemas": schemas, "tables": tables, "predicates": predicates,
            "queries": queries, "verify": verify}


def sql_type(t: str) -> str:
    if t in TYPE_MAP:
        return TYPE_MAP[t]
    if re.fullmatch(r"ty\d*", t):
        return "INTEGER"
    raise Skip(f"unknown column type {t!r}")


def translate(parsed: dict) -> dict:
    if not parsed["verify"]:
        raise Skip("no verify statement")
    qa, qb = parsed["verify"]
    if qa not in parsed["queries"] or qb not in parsed["queries"]:
        raise Skip("verify names a query that is missing or not backtick-quoted")
    sql = [parsed["queries"][qa], parsed["queries"][qb]]
    for pname, args in parsed["predicates"].items():
        if len(args) != 1:
            raise Skip(f"predicate {pname}({', '.join(args)}) ranges over {len(args)} rows; "
                       "only unary predicates can be a hidden column")
        if not re.fullmatch(r"[A-Za-z_]\w*", pname):
            raise Skip(f"predicate name {pname!r} is not an SQL identifier")
        if args[0] not in parsed["schemas"]:
            raise Skip(f"predicate {pname} over undeclared schema {args[0]}")
    hidden = {}
    for pname, (sname,) in parsed["predicates"].items():
        hidden.setdefault(sname, []).append(pname)

    def call(m: re.Match) -> str:
        return f"{m.group(2)}.__{m.group(1)}"

    for pname in parsed["predicates"]:
        pat = re.compile(rf"\b({re.escape(pname)})\s*\(\s*(\w+)\s*\)")
        sql = [pat.sub(call, s) for s in sql]
        if any(re.search(rf"\b{re.escape(pname)}\s*\(", s) for s in sql):
            raise Skip(f"predicate {pname} applied to something other than a row alias")

    for s in sql:
        if re.search(r"\(\s*VALUES\s*\)", s, re.I):
            raise Skip("empty relation `(VALUES)` (Cosette's stand-in for an input Calcite "
                       "proved empty) has no SQL form")
    tables = []
    used = " ".join(sql).lower()
    for tname, sname in parsed["tables"].items():
        if sname not in parsed["schemas"]:
            raise Skip(f"table {tname} uses undeclared schema {sname}")
        sch = parsed["schemas"][sname]
        cols = [{"name": c, "type": sql_type(t), "source_type": t, "nullable": False}
                for c, t in sch["columns"]]
        cols += [{"name": f"__{p}", "type": "BOOLEAN", "source_type": f"predicate {p}({sname})",
                  "nullable": False, "hidden_predicate": p} for p in hidden.get(sname, [])]
        referenced = re.search(rf"\b{re.escape(tname.lower())}\b", used) is not None
        if not cols:
            if referenced:
                raise Skip(f"table {tname} has no declared columns (schema {sname}(??)) "
                           "and no predicate column, so it cannot be written as SQL")
            continue
        tables.append({"name": tname, "schema": sname, "open": sch["open"], "columns": cols})
    return {"tables": tables, "sql_a": sql[0], "sql_b": sql[1],
            "predicates": {p: a[0] for p, a in parsed["predicates"].items()}}


def ddl(tables: list[dict], constraints: list[dict]) -> str:
    stmts = []
    for t in tables:
        parts = [f"  {c['name']} {c['type']} NOT NULL" for c in t["columns"]]
        for k in constraints:
            if k["kind"] == "primary_key" and k["table"].lower() == t["name"].lower():
                parts.append(f"  PRIMARY KEY ({', '.join(k['columns'])})")
        stmts.append(f"CREATE TABLE {t['name']} (\n" + ",\n".join(parts) + "\n);")
    return "\n".join(stmts)


# --- validation ------------------------------------------------------------

def check_sqlglot(sql: str) -> None:
    if "||" in sql:
        raise Skip("uses `||`, which sqlglot's mysql dialect reads as OR")
    try:
        trees = sqlglot.parse(sql, read="mysql")
    except Exception as exc:  # noqa: BLE001
        raise Skip(f"sqlglot(mysql) cannot parse: {str(exc).splitlines()[0][:200]}") from exc
    if len([t for t in trees if t is not None]) != 1:
        raise Skip("sqlglot(mysql) did not read exactly one statement")


def _value(col: dict, rng: random.Random, i: int, key_cols: set[str]):
    if col["name"] in key_cols:
        return i
    if col["type"] == "BOOLEAN":
        return rng.random() < 0.5
    if col["type"] == "VARCHAR":
        return rng.choice(["a", "b", "hello", "hello hi", "Security"])
    return rng.choice([0, 1, 2, 3, 5, 10, 20, 29, 468, 1001])


def _bag(rows):
    return sorted(repr(r) for r in rows)


def duckdb_sql(sql: str) -> str:
    """Calcite's generated names (``$f0``) quoted so DuckDB reads them; nothing else changes."""

    return re.sub(r'(?<![\w"])(\$\w+)', r'"\1"', sql)


def run_duckdb(schema_ddl: str, tables: list[dict], constraints: list[dict], sql_a: str,
               sql_b: str, trials: int = 40, seed: int = 0) -> dict:
    """Run both queries on an empty and on random databases.

    Returns ``{"executes": True, "random_db_disagreements": n, "trials": k}``;
    raises Skip if either query fails to run.
    """

    import duckdb

    keys = {k["table"].lower(): set(k["columns"]) for k in constraints
            if k["kind"] == "primary_key"}
    rng = random.Random(seed)
    disagreements = by_name = 0
    for trial in range(trials + 1):
        con = duckdb.connect()
        con.execute(schema_ddl)
        if trial:
            for t in tables:
                n = rng.randint(0, 4)
                kc = keys.get(t["name"].lower(), set())
                rows = [tuple(_value(c, rng, i, kc) for c in t["columns"]) for i in range(n)]
                if rows:
                    ph = ", ".join("?" for _ in t["columns"])
                    con.executemany(f"INSERT INTO {t['name']} VALUES ({ph})", rows)
        try:
            cur = con.execute(duckdb_sql(sql_a))
            a, names_a = cur.fetchall(), [d[0].lower() for d in cur.description]
            cur = con.execute(duckdb_sql(sql_b))
            b, names_b = cur.fetchall(), [d[0].lower() for d in cur.description]
        except Exception as exc:  # noqa: BLE001
            where = "empty database" if not trial else "random database"
            if "Conversion Error" in str(exc):
                raise Skip("a query compares a column with a value of another type "
                           f"({str(exc).splitlines()[0][:120]}); Cosette declared the "
                           "column int, so the comparison has no faithful SQL typing") from exc
            raise Skip(f"duckdb fails on {where}: {str(exc).splitlines()[0][:200]}") from exc
        finally:
            con.close()
        if _bag(a) != _bag(b):
            disagreements += 1
            if sorted(names_a) == sorted(names_b) and len(set(names_a)) == len(names_a):
                order = [names_b.index(n) for n in names_a]
                if _bag(a) != _bag([tuple(r[i] for i in order) for r in b]):
                    by_name += 1
            else:
                by_name += 1
    return {"executes": True, "trials": trials, "random_db_disagreements": disagreements,
            "disagreements_after_matching_columns_by_name": by_name}


# Primary keys of Calcite's test catalog (MockCatalogReader: EMP.EMPNO, DEPT.DEPTNO),
# used only to explain why a calcite pair disagrees on Cosette's keyless schema.
CALCITE_KEYS = [{"kind": "primary_key", "table": "emp", "columns": ["empno"]},
                {"kind": "primary_key", "table": "dept", "columns": ["deptno"]}]


def disagreement_reason(label: str, run: dict, tr: dict, constraints: list[dict],
                        schema_ddl: str) -> str:
    reason = (f"labelled {label}, but {run['random_db_disagreements']} of {run['trials']} "
              "random DuckDB databases give different results on the schema the .cos file "
              "declares")
    if not run["disagreements_after_matching_columns_by_name"]:
        return reason + ("; they agree once output columns are matched by name, i.e. the "
                         ".cos schema's column order differs from the column list the other "
                         "query spells out")
    names = {t["name"].lower() for t in tr["tables"]}
    keys = [k for k in CALCITE_KEYS if k["table"] in names]
    if keys and not constraints:
        try:
            again = run_duckdb(ddl(tr["tables"], keys), tr["tables"], keys,
                               tr["sql_a"], tr["sql_b"])
        except Skip:
            again = None
        if again and not again["random_db_disagreements"]:
            return reason + ("; they agree on every random database once "
                             + ", ".join(f"{k['table']}.{k['columns'][0]}" for k in keys)
                             + " are keys (as in Calcite's catalog), which the .cos file "
                             "does not declare")
    return reason


# --- driver ------------------------------------------------------------------

def cosette_results(src: Path) -> dict:
    path = src / "examples" / "calcite" / "calcite_result_with_label.csv"
    with path.open(newline="") as fh:
        return {r["Case"]: {"result": r["Result"], "reason": r["Reason"], "remark": r["Remark"]}
                for r in csv.DictReader(fh)}


def convert(src: Path) -> tuple[list[dict], list[dict]]:
    cases, skipped = [], []
    calcite_info = cosette_results(src)
    for path in sorted((src / "examples").glob("*/*.cos")):
        folder, name = path.parent.name, path.stem
        rec = {"name": name, "source_dir": folder,
               "source_file": f"examples/{folder}/{path.name}"}
        try:
            if (folder, name) in LABEL_DISPUTES:
                raise Skip(LABEL_DISPUTES[(folder, name)])
            label = FOLDER_LABELS.get(folder)
            if label is None:
                raise Skip(f"unknown folder {folder}")
            constraints, constraint_source = [], None
            if folder == "conditional":
                if name in CONDITIONAL_SKIPS:
                    raise Skip(CONDITIONAL_SKIPS[name])
                if name not in CONDITIONAL:
                    raise Skip("conditional example without a recorded precondition")
                constraints = CONDITIONAL[name]["constraints"]
                constraint_source = CONDITIONAL[name]["source"]
            parsed = parse_cos(path.read_text(encoding="utf-8"))
            tr = translate(parsed)
            for k in constraints:
                table = next((t for t in tr["tables"] if t["name"].lower() == k["table"]), None)
                if table is None or not set(k["columns"]) <= {c["name"] for c in table["columns"]}:
                    raise Skip(f"constraint {k} does not match the declared schema")
            for s in (tr["sql_a"], tr["sql_b"]):
                check_sqlglot(s)
            schema_ddl = ddl(tr["tables"], constraints)
            run = run_duckdb(schema_ddl, tr["tables"], constraints, tr["sql_a"], tr["sql_b"])
            if label != "not_equivalent" and run["random_db_disagreements"]:
                raise Skip(disagreement_reason(label, run, tr, constraints, schema_ddl))
            case = {**rec, "label": label, "schema": {"tables": tr["tables"]},
                    "constraints": constraints}
            if constraint_source:
                case["constraint_source"] = constraint_source
            if tr["predicates"]:
                case["predicates"] = {p: f"__{p} on tables of schema {s}"
                                      for p, s in tr["predicates"].items()}
            case.update({"sql_a": tr["sql_a"], "sql_b": tr["sql_b"], "ddl": schema_ddl,
                         "duckdb": run})
            if folder == "calcite" and name in calcite_info:
                case["cosette_result"] = calcite_info[name]
            cases.append(case)
        except Skip as exc:
            skipped.append({**rec, "category": skip_category(str(exc)), "reason": str(exc)})
    return cases, skipped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, required=True, help="Cosette checkout")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    commit = subprocess.run(["git", "-C", str(args.src), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    cases, skipped = convert(args.src)
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "cosette_cases.jsonl").open("w", encoding="utf-8") as fh:
        for c in cases:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    with (args.out / "cosette_skipped.jsonl").open("w", encoding="utf-8") as fh:
        for s in skipped:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")
    summary: dict = {"source": "https://github.com/uwdb/Cosette", "commit": commit,
                     "converted": len(cases), "skipped": len(skipped), "by_folder": {}}
    for c in cases:
        f = summary["by_folder"].setdefault(c["source_dir"], {"converted": 0, "skipped": 0})
        f["converted"] += 1
    for s in skipped:
        f = summary["by_folder"].setdefault(s["source_dir"], {"converted": 0, "skipped": 0})
        f["skipped"] += 1
        by = f.setdefault("skipped_by_category", {})
        by[s["category"]] = by.get(s["category"], 0) + 1
    summary["not_equivalent_with_random_counterexample"] = sorted(
        c["name"] for c in cases
        if c["label"] == "not_equivalent" and c["duckdb"]["random_db_disagreements"])
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    for s in skipped:
        print(f"skip {s['source_dir']}/{s['name']}: {s['reason']}")


if __name__ == "__main__":
    main()
