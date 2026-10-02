"""Score ``kumosql.output_properties`` against executed ground truth.

Two tracks, kept apart in the results:

* ``original`` cases (tests/fixtures/output_properties/cases.json, plus a held-out file used
  for the final evaluation only): each case is a query with labelled claims, "this output
  column is never NULL", "these columns are unique", "at most / exactly one row", each
  labelled as holding or not. Labels were written from the SQL semantics, then every label is
  checked by running the query on random databases that respect the declared NOT NULL columns
  and keys: a true label must never be violated, a false label must be violated by some
  database (otherwise it is reported as unverified).
* ``adapted`` queries: the queries of SQLSolver's published equivalence pairs (Calcite, Spark,
  TPC-H, TPC-C), analysed with their own schemas. There are no labels; every fact the analysis
  claims is checked against executed results.

Outcomes per labelled claim: ``proved`` (holds, analysis says so), ``unknown`` (holds, analysis
does not say so), ``correct_unknown`` (does not hold, analysis does not claim it), ``wrong``
(analysis claims something the data violates; must stay 0), ``unsupported`` (the analysis
declined the query) and ``error``. There is no timeout: the analysis is a single pass over the
syntax tree.

    python tools/output_properties_bench.py                  # development cases
    python tools/output_properties_bench.py --held-out       # held-out cases
    python tools/output_properties_bench.py --adapted        # SQLSolver queries
"""

from __future__ import annotations

import json
from pathlib import Path
import random
import re
import sys
import time

import sqlglot

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kumosql.output_properties import infer_properties  # noqa: E402
from kumosql.smt_equivalence import TableConstraints  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "output_properties"

DOMAINS = {
    "INT64": [0, 1, 2, 3, 5, 7, 11, 200],
    "STRING": ["a", "b", "x", "EU", "EUR", "US", "Eve"],
    "DATE": ["2024-01-01", "2024-01-02", "2024-02-01"],
}
DUCK = {"INT64": "BIGINT", "STRING": "VARCHAR", "DATE": "DATE"}


def load_cases(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def constraints_of(schema: dict) -> tuple[dict, dict]:
    columns = {t: list(spec["columns"]) for t, spec in schema["tables"].items()}
    constraints = {
        t: TableConstraints(not_null=frozenset(spec.get("not_null", ())), keys=tuple(tuple(k) for k in spec.get("keys", ())))
        for t, spec in schema["tables"].items()
    }
    return columns, constraints


def random_database(schema: dict, rng: random.Random) -> dict[str, list[tuple]]:
    """Rows that respect NOT NULL and key declarations; small domains so joins and ties are common."""

    db = {}
    for table, spec in schema["tables"].items():
        names = list(spec["columns"])
        rows, seen = [], [set() for _ in spec.get("keys", [])]
        for _ in range(rng.choice([0, 0, 1, 2, 3, 4, 5])):
            row = []
            for name in names:
                value = rng.choice(DOMAINS[spec["columns"][name]])
                if name not in spec.get("not_null", ()) and rng.random() < 0.3:
                    value = None
                row.append(value)
            clash = False
            for index, key in enumerate(spec.get("keys", [])):
                value = tuple(row[names.index(k)] for k in key)
                if None not in value and value in seen[index]:
                    clash = True
            if clash:
                continue
            for index, key in enumerate(spec.get("keys", [])):
                seen[index].add(tuple(row[names.index(k)] for k in key))
            rows.append(tuple(row))
        db[table] = rows
    return db


def new_connection(schema: dict):
    import duckdb

    db = duckdb.connect(":memory:")
    for table, spec in schema["tables"].items():
        columns = ", ".join(f'"{c}" {DUCK[t]}' for c, t in spec["columns"].items())
        db.execute(f'CREATE TABLE "{table}" ({columns})')
    return db


def literal(value) -> str:
    if value is None:
        return "NULL"
    return str(value) if isinstance(value, int) else "'" + str(value).replace("'", "''") + "'"


def load(db, schema: dict, data: dict) -> None:
    for table, rows in data.items():
        db.execute(f'DELETE FROM "{table}"')
        if rows:
            db.execute(f'INSERT INTO "{table}" VALUES ' + ", ".join("(" + ", ".join(literal(v) for v in row) + ")" for row in rows))


def to_duck(sql: str, dialect: str = "bigquery") -> str:
    sql = re.sub(r"@\w+", "5", sql)
    return sqlglot.transpile(sql, read=dialect, write="duckdb")[0]


def observe(db, query: str, outer: str | None):
    """Rows of the query, or, for a correlated query, the number of rows it yields per outer row."""

    if outer:
        key = outer.split()[-1]
        rows = db.execute(f"SELECT (SELECT COUNT(*) FROM ({query}) AS kq_inner) FROM {outer}").fetchall()
        return None, [r[0] for r in rows]
    cursor = db.execute(query)
    names = [d[0].lower() for d in cursor.description]
    return names, cursor.fetchall()


def violated(claim: list, names, rows, counts) -> bool:
    kind = claim[0]
    if kind == "rows":
        sizes = counts if counts is not None else [len(rows)]
        return any(n > 1 for n in sizes) if claim[1] == "at_most_one" else any(n != 1 for n in sizes)
    if kind == "non_null":
        return any(row[names.index(claim[1])] is None for row in rows)
    positions = [names.index(c) for c in claim[1]]
    projected = [tuple(row[p] for p in positions) for row in rows]
    return len(set(projected)) != len(projected)


def claims_made(props, positional: bool = False) -> list[list]:
    """Every fact the analysis asserts, in the claim format used by the labels.

    ``positional`` names columns by output position (as strings) instead of by name, for
    queries whose output repeats a name.
    """

    if positional:
        made = [["non_null", str(i)] for i, c in enumerate(props.columns) if c.non_null]
        made += [["unique", [str(p) for p in k.positions]] for k in props.keys if k.columns]
    else:
        made = [["non_null", c.name] for c in props.columns if c.non_null]
        made += [["unique", list(k.columns)] for k in props.keys if k.columns]
    if props.at_most_one_row:
        made.append(["rows", "at_most_one"])
    if props.exactly_one_row:
        made.append(["rows", "exactly_one"])
    return made


def implied(made: list[list], claim: list) -> bool:
    """Whether the analysis's claims include what ``claim`` states (a unique key implies its supersets)."""

    kind = claim[0]
    if kind == "non_null":
        return ["non_null", claim[1]] in made
    if kind == "rows":
        return ["rows", claim[1]] in made or (claim[1] == "at_most_one" and ["rows", "exactly_one"] in made)
    wanted = set(claim[1])
    return any(m[0] == "unique" and set(m[1]) <= wanted for m in made) or ["rows", "at_most_one"] in made or ["rows", "exactly_one"] in made


def run(filename: str = "cases.json", trials: int = 300, seed: int = 7) -> dict:
    data = load_cases(filename)
    out = {
        "cases": len(data["cases"]),
        "claims": 0,
        "proved": 0,
        "unknown": 0,
        "correct_unknown": 0,
        "unsupported": 0,
        "error": 0,
        "timeout": 0,
        "wrong": [],
        "label_wrong": [],
        "unverified": [],
        "missed": [],
        "analysis_claims": 0,
        "analysis_violations": [],
        "rows_changed": 0,
        "seconds": 0.0,
    }
    start = time.time()
    connections = {name: (schema, new_connection(schema), constraints_of(schema)) for name, schema in data["schemas"].items()}
    for case in data["cases"]:
        schema, db, (columns, constraints) = connections[case["schema"]]
        rng = random.Random(f"{seed}:{case['id']}")
        try:
            props = infer_properties(case["sql"], constraints, columns)
        except Exception as error:  # noqa: BLE001
            out["claims"] += len(case["claims"])
            out["error"] += len(case["claims"])
            out["wrong"].append((case["id"], f"crash: {error!r}"))
            continue
        out["claims"] += len(case["claims"])
        # Ground truth: run on random databases that respect the declarations.
        witnessed = [False] * len(case["claims"])
        made = [] if props.unsupported else claims_made(props)
        analysis_bad = set()
        sql = to_duck(case["sql"])
        for _ in range(trials):
            load(db, schema, random_database(schema, rng))
            try:
                names, rows = observe(db, sql, case.get("outer"))
            except Exception as error:  # noqa: BLE001
                out["error"] += len(case["claims"])
                out["wrong"].append((case["id"], f"execution failed: {error!r}"))
                break
            counts = rows if names is None else None
            rows = [] if names is None else rows
            for index, claim in enumerate(case["claims"]):
                if violated(claim, names, rows, counts):
                    witnessed[index] = True
            for claim in made:
                if claim[0] == "rows" and case.get("outer") is None and names is None:
                    continue
                if names is None and claim[0] != "rows":
                    continue
                if violated(claim, names, rows, counts):
                    analysis_bad.add(json.dumps(claim))
        else:
            if not props.unsupported:
                out["analysis_claims"] += len(made)
                for item in analysis_bad:
                    out["analysis_violations"].append((case["id"], item))
            for index, claim in enumerate(case["claims"]):
                label = claim[-1]
                spec = claim[:-1]
                if props.unsupported:
                    out["unsupported"] += 1
                    continue
                said = implied(made, spec)
                if label and witnessed[index]:
                    out["label_wrong"].append((case["id"], spec))
                    continue
                if said and (not label or witnessed[index]):
                    out["wrong"].append((case["id"], spec))
                elif said:
                    out["proved"] += 1
                elif label:
                    out["unknown"] += 1
                    out["missed"].append((case["id"], spec))
                else:
                    out["correct_unknown"] += 1
                    if not witnessed[index]:
                        out["unverified"].append((case["id"], spec))
    out["seconds"] = round(time.time() - start, 1)
    return out


def run_adapted(trials: int = 40, seed: int = 5) -> dict:
    """Analyse the queries of SQLSolver's pairs and check every claim against executed rows."""

    import duckdb
    import sqlsolver_bench as sb

    out = {"queries": 0, "supported": 0, "unsupported": 0, "unchecked": 0, "claims": 0, "violations": [], "non_null": 0, "unique": 0, "single_row": 0, "columns": 0, "seconds": 0.0}
    start = time.time()
    for suite, (pairs_file, schema_file) in sb.SUITES.items():
        tables = sb.load_schema(sb.FIXTURES / schema_file)
        columns = {t.name: [c.name for c in t.columns] for t in tables.values()}
        constraints = {
            t.name: TableConstraints(
                not_null=frozenset(c.name for c in t.columns if c.not_null),
                keys=tuple(k for k in ([t.primary_key] if t.primary_key else []) + list(t.unique)),
            )
            for t in tables.values()
        }
        db = sb.new_database(tables)
        seen = set()
        for left, right in sb.load_pairs(sb.FIXTURES / pairs_file):
            for sql in (left, right):
                sql = sb.spark_days(sql)
                if sql in seen:
                    continue
                seen.add(sql)
                out["queries"] += 1
                props = infer_properties(sql, constraints, columns, dialect="mysql")
                if props.unsupported:
                    out["unsupported"] += 1
                    continue
                made = claims_made(props, positional=True)
                out["supported"] += 1
                out["columns"] += len(props.columns)
                out["non_null"] += sum(1 for m in made if m[0] == "non_null")
                out["unique"] += sum(1 for m in made if m[0] == "unique")
                out["single_row"] += sum(1 for m in made if m[0] == "rows")
                if not made:
                    continue
                try:
                    duck_sql = sb.to_dialect(sql, "duckdb")
                    if suite == "calcite":
                        duck_sql = sb.constant_groupings(sb.to_dialect(sb.name_values(sql), "duckdb"))
                    used = [tables[n] for n in sorted(sb.referenced_tables(sql)) if n in tables]
                except sqlglot.errors.SqlglotError:
                    out["unchecked"] += 1
                    continue
                rng = random.Random(f"{seed}:{suite}:{sql}")
                bad = set()
                ran = False
                for _ in range(trials):
                    try:
                        for table in used:
                            db.execute(f'DELETE FROM "{table.name}"')
                            rows = sb.random_rows(table, rng)
                            if rows:
                                marks = ", ".join("?" * len(table.columns))
                                db.executemany(f'INSERT INTO "{table.name}" VALUES ({marks})', rows)
                        cursor = db.execute(duck_sql)
                        rows = cursor.fetchall()
                    except duckdb.Error:
                        break
                    ran = True
                    # position-based: the analysis lists columns in output order, and names can repeat
                    names = [str(i) for i in range(len(props.columns))]
                    if len(cursor.description) != len(names):
                        break
                    for claim in made:
                        if violated(claim, names, rows, None):
                            bad.add(json.dumps(claim))
                if not ran:
                    out["unchecked"] += 1
                out["claims"] += len(made) if ran else 0
                for item in bad:
                    out["violations"].append((suite, sql[:120], item))
    out["seconds"] = round(time.time() - start, 1)
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if "--adapted" in args:
        result = run_adapted()
        print(json.dumps({k: v for k, v in result.items() if k != "violations"}, indent=1))
        for v in result["violations"]:
            print("VIOLATION", v)
        return 1 if result["violations"] else 0
    result = run("held_out.json" if "--held-out" in args else "cases.json")
    keys = ["cases", "claims", "proved", "unknown", "correct_unknown", "unsupported", "error", "timeout", "seconds"]
    print(json.dumps({k: result[k] for k in keys}, indent=1))
    for key in ("wrong", "label_wrong", "unverified", "missed", "analysis_violations"):
        for item in result[key]:
            print(key.upper(), item)
    return 1 if (result["wrong"] or result["label_wrong"] or result["analysis_violations"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
