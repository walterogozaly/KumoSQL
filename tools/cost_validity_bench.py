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

1. correctness: the evidence label (proven, or not) and, as a separate field,
   whether the rewrite, executed on the database, agrees with the baseline: the
   same bag of rows, the same output column names and types, and under a
   top-level ``ORDER BY`` the same sequence of sort keys (a key that is not an
   output column is appended to both select lists for the check). Floats compare
   rounded to 9 decimal places. Agreement on one dataset is evidence, not proof
   (``WHERE k = 10`` and ``WHERE k < 15`` agree on a table without 11..14);
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
from rewrite_bench import FLOAT_PLACES, Executor, _catalog, _explain_cost, _jsonable, _norm  # noqa: E402
from sqlglot import exp  # noqa: E402

FASTER = 1.10  # an observed speedup of at least 10% counts as a saving
CHEAPER = 0.98  # an estimate at least 2% lower counts as a predicted saving

# What a proof label means. Copied into every saved recommendation, so a record read alone carries them.
PROOF_ASSUMPTIONS = (
    "declared primary keys and NOT NULL columns hold in the data (PostgreSQL enforces them here; BigQuery does not)",
    "floating-point values are never NaN",
    "runtime errors (division by zero, overflow, failed casts) are not modeled",
    "column types are not compared by the proof",
    "rows that tie under ORDER BY and LIMIT may be chosen differently",
)
OPTIMIZER_ACCEPTANCE = "the optimizer accepts a proven rewrite whose EXPLAIN estimate is at most 2% above the original, so accepted does not mean cheaper"


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


def _key_slots(tree: exp.Select) -> list[int | None]:
    """For each top-level ``ORDER BY`` key, its output position, or None when it is not an output column.

    An integer is a position; an unqualified name that matches exactly one output name is that column;
    anything else (``t.k``, ``a + b``) is an output column only when a select item is that same expression.
    """

    names = [e.alias_or_name.lower() for e in tree.expressions]
    slots: list[int | None] = []
    for item in tree.args["order"].expressions:
        key = item.this
        slot = None
        if isinstance(key, exp.Literal) and key.is_int:
            slot = int(key.name) - 1
        elif isinstance(key, exp.Column) and not key.table:
            match = [i for i, n in enumerate(names) if n == key.name.lower()]
            slot = match[0] if len(match) == 1 else None
        if slot is None and not isinstance(key, exp.Literal):
            match = [i for i, e in enumerate(tree.expressions) if not isinstance(e, exp.Star) and e.unalias() == key]
            slot = match[0] if match else None
        slots.append(slot)
    return slots


def order_key_positions(sql: str) -> list[int] | None:
    """Output positions of the top-level ``ORDER BY`` keys, or None when a key is not an output column."""

    tree = sqlglot.parse_one(sql, read="postgres")
    if not isinstance(tree, exp.Select) or tree.args.get("order") is None:
        return None
    slots = _key_slots(tree)
    return None if None in slots else slots


def with_sort_keys(sql: str) -> tuple[str, list[int], int] | None:
    """The statement with its hidden ``ORDER BY`` keys appended to the select list, and every key's column.

    Returns ``(sql, positions, hidden)``, ``hidden`` being how many columns were appended. A key that is
    already an output column keeps its position; a hidden key is appended as a column named
    ``_kumo_sort_N`` and addressed from the end of the row (a negative position), so ``SELECT *`` needs
    no column count. Returns None when there is no top-level ``ORDER BY``
    on a plain ``SELECT``, or when a column cannot be added: ``SELECT DISTINCT`` would change which rows
    survive, and a set operation sorts by its output columns only.
    """

    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.SqlglotError:
        return None
    if not isinstance(tree, exp.Select) or tree.args.get("order") is None:
        return None
    slots = _key_slots(tree)
    if None not in slots:
        return sql, slots, 0
    if tree.args.get("distinct") is not None:
        return None
    tree = tree.copy()
    hidden = [item.this for item, slot in zip(tree.args["order"].expressions, slots) if slot is None]
    for number, key in enumerate(hidden):
        tree.append("expressions", exp.alias_(key.copy(), f"_kumo_sort_{number}"))
    positions, number = [], 0
    for slot in slots:
        if slot is None:
            positions.append(number - len(hidden))
            number += 1
        else:
            positions.append(slot)
    return tree.sql(dialect="postgres"), positions, len(hidden)


def _rows(rows: list) -> list[tuple]:
    return [tuple(_norm(v) for v in r) for r in rows]


def same_rows(left: list, right: list, baseline: str) -> bool:
    """Same bag of rows; under a top-level ORDER BY on output columns, also the same sequence of sort keys.

    Rows that tie on every sort key may come back in any order, so only the keys'
    sequence is compared. When a key is not an output column this function compares
    the bag alone; ``sort_keys_agree`` then checks the order with the hidden keys appended.
    """

    a, b = _rows(left), _rows(right)
    if Counter(a) != Counter(b):
        return False
    positions = order_key_positions(baseline) if qo.has_top_level_order(baseline) else None
    if not positions:
        return True
    return [tuple(r[i] for i in positions) for r in a] == [tuple(r[i] for i in positions) for r in b]


def sort_keys_agree(executor: Executor, database: str, baseline: str, sql: str) -> tuple[bool, str]:
    """Whether both statements order their rows alike, judged on the keys the baseline sorts by.

    Returns ``(agree, basis)``; ``basis`` says what was compared, so a pass is never read as more than it
    was: ``unordered`` (no top-level ORDER BY), ``output sort keys`` (compared by ``same_rows``),
    ``hidden sort keys appended`` (both statements re-run with the keys as extra columns; the rows with
    their keys must be the same bag and the key sequence the same), or ``bag only: ...`` with the reason
    the order could not be checked.
    """

    if not qo.has_top_level_order(baseline):
        return True, "unordered"
    plan = with_sort_keys(baseline)
    if plan is None:
        return True, "bag only: the sort keys cannot be added to the select list (DISTINCT or a set operation)"
    if plan[0] == baseline:
        return True, "output sort keys"
    other = with_sort_keys(sql)
    if other is None or len(other[1]) < len(plan[1]):
        return True, "bag only: the rewrite's sort keys cannot be added to its select list, or it sorts by fewer keys"
    ran = [executor.run(database, text) for text in (plan[0], other[0])]
    if not all(r["ok"] for r in ran):
        return True, "bag only: the statement with its sort keys did not run"
    # Each row as (its own columns, its sort keys); a rewrite may add tie-breaking keys after the baseline's.
    seen = []
    for (_, positions, hidden), result in zip((plan, other), ran):
        rows = _rows(_jsonable(result["rows"]))
        seen.append([(r[: len(r) - hidden], tuple(r[i] for i in positions[: len(plan[1])])) for r in rows])
    a, b = seen
    return Counter(a) == Counter(b) and [k for _, k in a] == [k for _, k in b], "hidden sort keys appended"


def judge(executor: Executor, database: str, baseline: str, sql: str) -> dict:
    """Execute both statements and compare what a consumer sees.

    ``outcome`` is the dataset agreement and only that; the proof label is a separate field the caller
    keeps (``label``). ``agreement_basis`` says what the comparison covered.
    """

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
    schema = got.get("benchmark_schema"), got.get("rewrite_schema")
    if schema[0] is not None:
        record["schema"] = schema[0]
    same = same_rows(_jsonable(got["benchmark_rows"]), _jsonable(got["rewrite_rows"]), baseline)
    ordered, order_basis = sort_keys_agree(executor, database, baseline, sql) if same else (True, "not checked: rows differ")
    record["agreement_basis"] = {
        "rows": "same bag of rows",
        "order": order_basis,
        "schema": "output column names and types" if schema[0] is not None else "not compared",
        "float_places": FLOAT_PLACES,
    }
    if not same or not ordered:
        record["outcome"] = "different_rows"
    elif schema[0] is not None and schema[0] != schema[1]:
        record["outcome"] = "different_schema"
        record["rewrite_schema"] = schema[1]
    else:
        record["outcome"] = "same_rows"
    record["ms"] = [round(got["benchmark_ms"], 2), round(got["rewrite_ms"], 2)]
    record["speedup"] = round(got["benchmark_ms"] / max(got["rewrite_ms"], 1e-6), 3)
    return record


def summarize(records: list[dict], source: str) -> dict:
    """Counts per source. The proof label (``proven``, ``unproven``) and the dataset agreement (``same_rows`` and the rest) are counted apart."""

    made = [r for r in records if r.get(source, {}).get("sql")]
    outcomes = [r[source] for r in made if "outcome" in r[source]]
    judged = [j for j in outcomes if j["outcome"] != "baseline_error"]
    same = [j for j in judged if j["outcome"] == "same_rows"]
    proven = [r[source] for r in made if r[source]["label"] == "proven"]
    unproven = [r[source] for r in made if r[source]["label"] != "proven"]
    predicted = [j for j in same if None not in j["estimate"] and j["estimate"][1] <= j["estimate"][0] * CHEAPER]
    flat = [j for j in same if None not in j["estimate"] and j["estimate"][0] * CHEAPER < j["estimate"][1] <= j["estimate"][0] / CHEAPER]
    faster = [j for j in same if j["speedup"] >= FASTER]
    slower = [j for j in same if j["speedup"] <= 1 / FASTER]
    realized = [j for j in predicted if j["speedup"] >= FASTER]
    return {
        "queries": len(records),
        "recommendations": len(made),
        "proven": len(proven),
        "unproven": len(unproven),
        "unproven_judged": sum("outcome" in u and u["outcome"] != "baseline_error" for u in unproven),
        "judged": len(judged),
        "unjudged": len(outcomes) - len(judged),
        "same_rows": len(same),
        "different_rows": sum(j["outcome"] == "different_rows" for j in judged),
        "different_schema": sum(j["outcome"] == "different_schema" for j in judged),
        "rewrite_errors": sum(j["outcome"] == "rewrite_error" for j in judged),
        "baseline_errors": len(outcomes) - len(judged),
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
                rec["assumptions"] = [*PROOF_ASSUMPTIONS, *([OPTIMIZER_ACCEPTANCE] if source == "optimizer" else [])]
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
    wrong = sum(s["different_rows"] + s["different_schema"] + s["rewrite_errors"] for s in summary.values())
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
