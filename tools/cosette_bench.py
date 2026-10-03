"""Score Cosette's examples and the SPES-only Calcite pairs with KumoSQL's provers.

Fixtures come from ``tools/cosette_to_sql.py`` (Cosette, BSD-2-Clause) and
``tools/spes_to_sql.py`` (SPES, Apache-2.0); see the READMEs in
``tests/fixtures/cosette`` and ``tests/fixtures/spes``. Each pair gets one verdict:

* ``proven``: the prover shows the queries equivalent (an unbounded proof).
* ``refuted``: no proof, and a random DuckDB database on which the two queries
  return different bags (executed evidence).
* ``unknown``: neither.
* ``wrong``: proven, yet a counterexample exists or the source labels the pair
  not equivalent. Must stay 0.

Cosette carries labels (equivalent, not equivalent, equivalent under the keys
it states); ``correct`` counts proofs of equivalent pairs plus refutations of
not-equivalent ones. A refutation of a pair labelled equivalent is listed as a
label dispute. Cases with an uninterpreted predicate (a hidden ``__`` column)
are never refuted, because a random hidden column need not be a function of
the visible row.

    python tools/cosette_bench.py [cosette|spes]
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sqlsolver_bench as sb  # noqa: E402
from bench_sql_repairs import fold_table_names, uniquify_star_columns  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _tables(ddl: str) -> dict:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "schema.sql"
        path.write_text(ddl, encoding="utf-8")
        return sb.load_schema(path)


def load(suite: str) -> list[dict]:
    if suite == "cosette":
        return [json.loads(line) for line in (ROOT / "cosette" / "cosette_cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    ddl = (ROOT / "sqlsolver" / "calcite.schema.sql").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in (ROOT / "spes" / "spes_only_pairs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        row.setdefault("ddl", ddl)
    return rows


def repaired(left: str, right: str, tables: dict) -> tuple[str, str]:
    """One spelling per table across the pair (Cosette ignores case) and repeated star columns named as DuckDB names them."""

    try:
        left, right = fold_table_names(left, right)
    except Exception:  # a pair the repairs cannot read is scored as written
        pass
    columns = {name: [c.name for c in t.columns] for name, t in tables.items()}
    out = []
    for sql in (left, right):
        try:
            sql = uniquify_star_columns(sql, columns)
        except Exception:
            pass
        out.append(sql)
    return out[0], out[1]


def replay(counterexample, tables: dict, db, left: str, right: str, constants: bool):
    """Run both queries on the prover's counterexample database; the rows if DuckDB confirms a difference.

    The prover's model may hold values outside a column's declared type (it reasons over the values the
    queries compare); those are mapped one-to-one onto values of the column's type, and missing NOT NULL
    values get a default. DuckDB has the last word: only a database on which the executed queries differ,
    and which meets the declared keys, counts.
    """

    from collections import Counter

    from kumosql.duckdb_load import insert_rows

    by_lower = {name.lower(): name for name in tables}
    data: dict[str, list[tuple]] = {name: [] for name in tables}
    for name, rows in (counterexample.tables or {}).items():
        table = tables.get(by_lower.get(name.lower(), ""))
        if table is None:
            if rows:
                return None
            continue
        for row in rows:
            values = {k.lower(): v for k, v in row.items()}
            data[table.name].append(tuple(values.get(c.name.lower()) for c in table.columns))
    for name, rows in data.items():
        table = tables[name]
        for position, column in enumerate(table.columns):
            numeric = column.type.split("(")[0] in ("INT", "INTEGER", "BIGINT", "SMALLINT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT")
            odd = sorted({str(r[position]) for r in rows if r[position] is not None and isinstance(r[position], str) == numeric})
            mapping = {v: (10_000 + i if numeric else f"v{i}") for i, v in enumerate(odd)}
            fixed = []
            for row in rows:
                value = row[position]
                if value is None and column.not_null:
                    value = 0 if numeric else ""
                elif value is not None and str(value) in mapping and isinstance(value, str) == numeric:
                    value = mapping[str(value)]
                fixed.append(row[:position] + (value,) + row[position + 1 :])
            rows[:] = fixed
        for key in ([table.primary_key] if table.primary_key else []) + list(table.unique):
            positions = [[c.name for c in table.columns].index(k) for k in key]
            seen = [tuple(r[p] for p in positions) for r in rows]
            if len(set(seen)) != len(seen):
                return None
    try:
        left_sql, right_sql = sb.to_dialect(sb.spark_days(left), "duckdb"), sb.to_dialect(sb.spark_days(right), "duckdb")
        if constants:
            left_sql, right_sql = sb.constant_groupings(sb.to_dialect(sb.name_values(sb.spark_days(left)), "duckdb")), sb.constant_groupings(sb.to_dialect(sb.name_values(sb.spark_days(right)), "duckdb"))
        for name, rows in data.items():
            db.execute(f'DELETE FROM "{name}"')
            insert_rows(db, f'"{name}"', rows)
        a, b = Counter(db.execute(left_sql).fetchall()), Counter(db.execute(right_sql).fetchall())
    except Exception:
        return None
    return (left_sql, right_sql, a, b) if a != b else None


def search(left: str, right: str, tables: dict, constants: bool):
    """A second, stronger executed search (``kumosql.counterexample``: values seeded from the queries'
    literals, repeated values, keys honoured) for pairs the plain random search does not separate.

    Skipped for Calcite pairs with a literal in GROUP BY or a VALUES source, which the shared harness
    translates specially (``sqlsolver_bench.constant_groupings`` / ``name_values``).
    """

    import sqlglot
    from sqlglot import exp

    from kumosql import counterexample as cx

    if constants:
        for sql in (left, right):
            tree = sqlglot.parse_one(sql, read="mysql")
            if any(isinstance(e, exp.Literal) for g in tree.find_all(exp.Group) for e in g.expressions) or tree.find(exp.Values):
                return None
    spec = cx.Spec({
        name: cx.Table(
            name,
            [cx.Column(c.name, c.type, not_null=c.not_null) for c in t.columns],
            primary_key=tuple(t.primary_key or ()),
            unique=[tuple(u) for u in t.unique],
        )
        for name, t in tables.items()
    })
    found = cx.find_counterexample(spec, left, right, trials=150, seed=0)
    return found or None


def run(suite: str, prove=sb.prove_result, trials: int = 60) -> dict:
    out = {"total": 0, "proven": [], "refuted": [], "unknown": [], "wrong": [], "disputed": [], "correct": 0, "seconds": 0.0}
    constants = suite == "spes"  # SPES pairs are Calcite's: a literal in GROUP BY is a constant
    start = time.time()
    schemas: dict[str, tuple] = {}
    for case in load(suite):
        out["total"] += 1
        name = case["name"]
        label = case.get("label", "equivalent")
        if case["ddl"] not in schemas:
            tables = _tables(case["ddl"])
            schemas[case["ddl"]] = (tables, sb.new_database(tables))
        tables, db = schemas[case["ddl"]]
        left, right = repaired(case["sql_a"], case["sql_b"], tables)
        try:
            result = prove(left, right, tables, constants) if constants else prove(left, right, tables)
        except Exception:  # a crash is a failure to prove, never a proof
            result = False
        proof = result if isinstance(result, bool) else result.proven
        counter = sb.differ(left, right, tables, db, trials, constants=constants)
        if counter in (None, False) and getattr(result, "counterexample", None) is not None:
            counter = replay(result.counterexample, tables, db, left, right, constants)
        if counter in (None, False) and not proof:
            try:
                counter = search(left, right, tables, constants)
            except Exception:  # a query the second search cannot read: no evidence either way
                counter = None
        found = counter not in (None, False) and "__" not in case["ddl"]
        if proof and (found or label == "not_equivalent"):
            out["wrong"].append(name)
        elif proof:
            out["proven"].append(name)
            out["correct"] += 1
        elif found:
            out["refuted"].append(name)
            if label == "not_equivalent":
                out["correct"] += 1
            else:
                out["disputed"].append(name)
        else:
            out["unknown"].append(name)
    out["scored"] = out["total"] - len(out["disputed"])  # a pair labelled equivalent with a replayed counterexample is not one to prove
    out["seconds"] = time.time() - start
    return out


def main(argv: list[str] | None = None) -> int:
    suites = (argv if argv is not None else sys.argv[1:]) or ["cosette", "spes"]
    bad = 0
    for suite in suites:
        r = run(suite)
        print(
            f"{suite}: {r['correct']}/{r['scored']} correct ({r['total']} pairs), 0 wrong" if not r["wrong"] else f"{suite}: {len(r['wrong'])} WRONG",
            f"| proven {len(r['proven'])}, refuted {len(r['refuted'])} (label disputes {len(r['disputed'])}), unknown {len(r['unknown'])}, {r['seconds']:.1f}s",
        )
        for key in ("wrong", "disputed", "refuted"):
            if r[key]:
                print(f"  {key}: {', '.join(r[key])}")
        bad += len(r["wrong"])
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
