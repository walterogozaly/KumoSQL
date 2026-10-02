"""Are KumoSQL's cost recommendations correct, and do they save what the estimate says?

For every query of a workload (one SQL file per query, run on PostgreSQL) this
tool collects the rewrites KumoSQL recommends from two sources:

* ``rules``: the canonical rule pipeline (``kumosql.rewrite.canonical_rule_order``
  without ``format_sql``), which works on BigQuery SQL; the query is transpiled
  to BigQuery and back, and the round-tripped original is the baseline, so the
  transpiler cannot be mistaken for a rule;
* ``optimizer``: ``kumosql.query_optimizer.optimize`` with the EXPLAIN cost
  guard and the database's own catalog.

Each recommendation is then judged in a fixed order, and the steps are kept
apart in the output:

1. correctness: the evidence label (proven, or not) and whether the rewrite,
   executed on the database, returns the same rows as the baseline;
2. estimated benefit: PostgreSQL's EXPLAIN total cost before and after (the
   local stand-in for a BigQuery dry run);
3. observed benefit: median wall time of five alternating runs after a warm-up.

A recommendation counts as valid only if it is correct; its benefit is then
reported as estimated and as observed, never mixed.

    python tools/cost_validity_bench.py --workload tpcds=queries/tpcds --workload dsb=queries/dsb --out results.json
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import sqlglot  # noqa: E402

from kumosql import query_optimizer as qo  # noqa: E402
from kumosql.rewrite import apply_rules, canonical_rule_order  # noqa: E402
from rewrite_bench import Executor, _catalog, _explain_cost, _jsonable, _norm  # noqa: E402
from sqlglot import exp  # noqa: E402

FASTER = 1.10  # an observed speedup of at least 10% counts as a saving
CHEAPER = 0.98  # an estimate at least 2% lower counts as a predicted saving


def load_workload(spec: str) -> list[dict]:
    """``DB=DIR``: every statement of every ``*.sql`` file in DIR, run on database DB."""

    database, folder = spec.split("=", 1)
    queries = []
    for path in sorted(Path(folder).glob("*.sql")):
        statements = [s for s in sqlglot.transpile(path.read_text(encoding="utf-8"), read="postgres", write="postgres") if s.strip()]
        for index, sql in enumerate(statements):
            name = path.stem if len(statements) == 1 else f"{path.stem}{'abcdefgh'[index]}"
            queries.append({"name": f"{database}/{name}", "database": database, "sql": sql})
    return queries


def rule_recommendation(sql: str) -> dict:
    """The rule pipeline's rewrite of ``sql`` (both sides round-tripped through BigQuery SQL)."""

    try:
        bigquery = sqlglot.transpile(sql, read="postgres", write="bigquery")[0]
        baseline = sqlglot.transpile(bigquery, read="bigquery", write="postgres")[0]
        names = [n for n in canonical_rule_order() if n != "format_sql"]
        result = apply_rules(names, bigquery)
    except Exception as error:  # noqa: BLE001 - a crash is no recommendation
        return {"error": f"{type(error).__name__}: {str(error)[:200]}"}
    rules = [s.rule for s in result.steps if s.changes]
    if not rules:
        return {}
    try:
        rewritten = sqlglot.transpile(result.sql, read="bigquery", write="postgres")[0]
    except Exception as error:  # noqa: BLE001
        return {"error": f"{type(error).__name__}: {str(error)[:200]}"}
    if " ".join(rewritten.split()) == " ".join(baseline.split()):
        return {}
    return {"baseline": baseline, "sql": rewritten, "steps": rules, "label": result.verification.status.value}


def optimizer_recommendation(query: dict, explain: tuple) -> dict:
    executor = Executor(explain[0], explain[1], 1, 60)
    try:
        catalog = _catalog({"profile": {}, "database": query["database"]}, executor)
        outcome = qo.optimize(query["sql"], catalog, dialect="postgres", cost=_explain_cost(executor, query["database"]))
    except Exception as error:  # noqa: BLE001
        return {"error": f"{type(error).__name__}: {str(error)[:200]}"}
    finally:
        for conn in executor.connections.values():
            conn.close()
    if outcome.sql is None:
        return {}
    return {"baseline": query["sql"], "sql": outcome.sql, "steps": list(outcome.steps), "label": "proven"}


def _recommend(job: tuple) -> dict:
    query, explain = job
    started = time.perf_counter()
    found = {"rules": rule_recommendation(query["sql"]), "optimizer": optimizer_recommendation(query, explain)}
    found["seconds"] = round(time.perf_counter() - started, 2)
    return found


def order_key_positions(sql: str) -> list[int] | None:
    """Output positions of the top-level ``ORDER BY`` keys, or None when a key is not an output column."""

    tree = sqlglot.parse_one(sql, read="postgres")
    order = tree.args.get("order")
    if not isinstance(tree, exp.Select) or order is None:
        return None
    names = [e.alias_or_name.lower() for e in tree.expressions]
    positions = []
    for item in order.expressions:
        key = item.this
        if isinstance(key, exp.Literal) and key.is_int:
            positions.append(int(key.name) - 1)
        elif isinstance(key, exp.Column) and key.name.lower() in names:
            match = [i for i, e in enumerate(tree.expressions) if e.alias_or_name.lower() == key.name.lower()]
            if len(match) != 1:
                return None
            positions.append(match[0])
        else:
            return None
    return positions


def same_rows(left: list, right: list, baseline: str) -> bool:
    """Same bag of rows; under a top-level ORDER BY, also the same sequence of sort keys.

    Rows that tie on every sort key may come back in any order, so only the keys'
    sequence is compared; when a key is not an output column, the bag alone is.
    """

    a = [tuple(_norm(v) for v in r) for r in left]
    b = [tuple(_norm(v) for v in r) for r in right]
    if Counter(a) != Counter(b):
        return False
    positions = order_key_positions(baseline) if qo.has_top_level_order(baseline) else None
    if not positions:
        return True
    return [tuple(r[i] for i in positions) for r in a] == [tuple(r[i] for i in positions) for r in b]


def judge(executor: Executor, database: str, baseline: str, sql: str) -> dict:
    cost = _explain_cost(executor, database)
    before, after = cost(baseline), cost(sql)
    record: dict = {"estimate": [before, after]}
    got = executor.compare(database, baseline, sql)
    if "benchmark_error" in got:
        record["outcome"] = "baseline_error"
        record["error"] = got["benchmark_error"][:200]
        return record
    if "rewrite_error" in got:
        record["outcome"] = "rewrite_error"
        record["error"] = got["rewrite_error"][:200]
        return record
    same = same_rows(_jsonable(got["benchmark_rows"]), _jsonable(got["rewrite_rows"]), baseline)
    record["outcome"] = "same_rows" if same else "different_rows"
    record["ms"] = [round(got["benchmark_ms"], 2), round(got["rewrite_ms"], 2)]
    record["speedup"] = round(got["benchmark_ms"] / max(got["rewrite_ms"], 1e-6), 3)
    return record


def summarize(records: list[dict], source: str) -> dict:
    made = [r for r in records if r.get(source, {}).get("sql")]
    judged = [r[source] for r in made if "outcome" in r[source]]
    same = [j for j in judged if j["outcome"] == "same_rows"]
    proven = [r[source] for r in made if r[source]["label"] == "proven"]
    predicted = [j for j in same if None not in j["estimate"] and j["estimate"][1] <= j["estimate"][0] * CHEAPER]
    flat = [j for j in same if None not in j["estimate"] and j["estimate"][0] * CHEAPER < j["estimate"][1] <= j["estimate"][0] / CHEAPER]
    faster = [j for j in same if j["speedup"] >= FASTER]
    slower = [j for j in same if j["speedup"] <= 1 / FASTER]
    realized = [j for j in predicted if j["speedup"] >= FASTER]
    return {
        "queries": len(records),
        "recommendations": len(made),
        "proven": len(proven),
        "executed": len(judged),
        "same_rows": len(same),
        "different_rows": sum(j["outcome"] == "different_rows" for j in judged),
        "rewrite_errors": sum(j["outcome"] == "rewrite_error" for j in judged),
        "baseline_errors": sum(j["outcome"] == "baseline_error" for j in judged),
        "estimated_cheaper": len(predicted),
        "estimated_unchanged": len(flat),
        "observed_faster": len(faster),
        "observed_slower": len(slower),
        "estimated_cheaper_and_observed_faster": len(realized),
        "geomean_speedup": round(statistics.geometric_mean([j["speedup"] for j in same]), 3) if same else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workload", action="append", required=True, help="DB=DIR of .sql files, run on database DB")
    parser.add_argument("--host", default="/tmp")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds per statement")
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--only", nargs="*", help="query names, or prefixes ending in /")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--rejudge", type=Path, help="reuse the recommendations of an earlier --out file and only execute them again")
    args = parser.parse_args(argv)

    queries = [q for spec in args.workload for q in load_workload(spec)]
    if args.only:
        queries = [q for q in queries if any(q["name"] == o or (o.endswith("/") and q["name"].startswith(o)) for o in args.only)]
    databases = {q["database"]: q["database"] for q in queries}
    explain = (databases, args.host)
    if args.rejudge:
        earlier = {r["query"]: r for r in json.loads(args.rejudge.read_text())["queries"]}
        keep = ("baseline", "sql", "steps", "label", "error")
        found = [
            {"seconds": earlier[q["name"]]["seconds"], **{s: {k: v for k, v in earlier[q["name"]][s].items() if k in keep} for s in ("rules", "optimizer")}}
            for q in queries
        ]
    else:
        with ProcessPoolExecutor(args.jobs) as pool:
            found = list(pool.map(_recommend, [(q, explain) for q in queries]))

    executor = Executor(databases, args.host, args.runs, args.timeout)
    records = []
    for query, recs in zip(queries, found):
        record = {"query": query["name"], "seconds": recs["seconds"]}
        for source in ("rules", "optimizer"):
            rec = recs[source]
            if rec.get("sql"):
                rec.update(judge(executor, query["database"], rec["baseline"], rec["sql"]))
            record[source] = rec
            line = rec.get("outcome", rec.get("error", "none" if not rec.get("sql") else "?"))
            extra = f" est={rec['estimate']} speedup={rec.get('speedup')}" if rec.get("sql") else ""
            print(f"{query['name']:<36} {source:<9} {line}{extra}", flush=True)
        records.append(record)

    summary = {source: summarize(records, source) for source in ("rules", "optimizer")}
    print()
    for source, s in summary.items():
        print(source, json.dumps(s))
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "queries": records}, indent=1, default=str))
    wrong = sum(s["different_rows"] for s in summary.values())
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
