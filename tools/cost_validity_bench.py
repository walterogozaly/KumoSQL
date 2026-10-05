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

``--advisor`` adds a third source that needs no database server and no network: the materialization
advisor's ``store_view`` and ``unstore_table`` recommendations on synthetic Dataform projects
(``tools/cost_validity_projects.py``). A recommendation there changes where a model is computed, not any
SQL, so the proof is the advisor's evidence label and the check is an executed comparison in DuckDB: the
project runs for a few simulated days with and without the change (sources grow, stored models refresh in
one run, and every query and every model is read between runs and after the refresh) and the two worlds
must return the same rows. A change that is not proven (``conditional``, ``unknown``) is listed as needing
proof and may never be counted as a saving or be chosen; a ``changes_results`` change must be refused.

    python tools/cost_validity_bench.py --workload tpcds=queries/tpcds --workload dsb=queries/dsb --out results.json
    python tools/cost_validity_bench.py --advisor --out advisor.json
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import sqlglot  # noqa: E402

import cost_validity_projects as projects  # noqa: E402
from kumosql import query_optimizer as qo  # noqa: E402
from kumosql.advisor import STORED_KINDS, advise  # noqa: E402
from kumosql.cost_model import Pricing  # noqa: E402
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


# ------------------------------------------------------------------ advisor: store_view and unstore_table

#: What an advisor label and its executed comparison mean. Copied into the saved records.
ADVISOR_ASSUMPTIONS = (
    "the data is a few dozen synthetic rows per table that grow every simulated day; sources change only between runs",
    "stored models refresh in one run in dependency order, once a day, and are read outside that run",
    "BigQuery SQL is transpiled to DuckDB for both sides alike; the transpiler is not under test",
    "a clock function is replaced by the simulated day's date or timestamp; RAND and UUID are DuckDB's, seeded and single-threaded",
    "a difference counts only when DuckDB with its optimizer off returns the same difference",
)
CLOCK_TYPES = (exp.CurrentTimestamp, exp.CurrentDatetime)


def _flat(key: str) -> str:
    return key.replace(".", "__")


def duckdb_sql(pipeline, sql: str, clock) -> str:
    """BigQuery ``sql`` over the project's nodes as DuckDB SQL, with the clock set to ``clock``."""

    def swap(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table):
            key = pipeline.resolve(node)
            if key:
                new = exp.table_(_flat(key))
                if node.args.get("alias") is not None:
                    new.set("alias", node.args["alias"].copy())
                return new
        if isinstance(node, exp.CurrentDate):
            return exp.cast(exp.Literal.string(clock.date().isoformat()), "date")
        if isinstance(node, CLOCK_TYPES):
            return exp.cast(exp.Literal.string(clock.strftime("%Y-%m-%d %H:%M:%S")), "timestamp")
        return node

    return sqlglot.parse_one(sql, read="bigquery").transform(swap).sql(dialect="duckdb")


class World:
    """The project in DuckDB with a given set of models stored as tables and the rest as views."""

    def __init__(self, case: "projects.Case", stored: frozenset[str]):
        import duckdb

        self.pipeline = case.pipeline()
        self.stored = stored
        self.order = [k for k in self.pipeline.topological_order() if k in self.pipeline.models]
        self.sql = {key: sql for key, (_, sql) in case.models.items()}
        self.con = duckdb.connect()
        self.con.execute("PRAGMA threads=1")
        self.con.execute("SELECT setseed(0.5)")
        for key, source in case.sources.items():
            columns = ", ".join(f"{name} {projects.DUCKDB_TYPES[kind]}" for name, kind in source.schema.items())
            self.con.execute(f"CREATE TABLE {_flat(key)} ({columns})")
            self.add_rows(key, source.initial())
        self.build(projects.START, tables=True)

    def add_rows(self, key: str, rows: list[tuple]) -> None:
        width = len(rows[0])
        self.con.executemany(f"INSERT INTO {_flat(key)} VALUES ({', '.join('?' * width)})", rows)

    def build(self, clock, *, tables: bool) -> None:
        """Define every view as of ``clock``; with ``tables``, also refresh every stored model, in dependency order."""

        for key in self.order:
            text = duckdb_sql(self.pipeline, self.sql[key], clock)
            if key in self.stored:
                if tables:
                    self.con.execute(f"CREATE OR REPLACE TABLE {_flat(key)} AS {text}")
            else:
                self.con.execute(f"CREATE OR REPLACE VIEW {_flat(key)} AS {text}")

    def read(self, sql: str, clock, *, unoptimized: bool = False) -> tuple[list[tuple], list[tuple]]:
        from kumosql.duckdb_load import run_unoptimized

        text = duckdb_sql(self.pipeline, sql, clock)
        if unoptimized:
            return _rows(_jsonable(run_unoptimized(self.con, text)[0])), []
        cursor = self.con.execute(text)
        return _rows(_jsonable(cursor.fetchall())), [(d[0], str(d[1])) for d in cursor.description]


def read_queries(case: "projects.Case") -> list[str]:
    """Every query the project's readers run, and a full read of every model."""

    return [r.sql for r in case.readers] + [f"SELECT * FROM `{key}`" for key in case.models]


def execute_change(case: "projects.Case", store: frozenset[str] = frozenset(), unstore: frozenset[str] = frozenset()) -> dict:
    """Run the project with and without a change for the case's simulated days and compare every read.

    Each day the sources grow, then every query and model is read before the refresh (between runs) and
    after it. ``outcome`` is ``same_rows``, ``different_rows`` (rows differ and DuckDB with its optimizer
    off returns the same difference), ``different_schema``, ``rewrite_error``, ``baseline_error`` or
    ``optimizer_disagrees`` (a difference that disappears with the optimizer off, not counted either way).
    """

    baseline = frozenset(k for k, (kind, _) in case.models.items() if kind in STORED_KINDS)
    before, after = World(case, baseline), World(case, (baseline | store) - unstore)
    queries = read_queries(case)
    found: list[dict] = []
    reads = 0
    errors: dict[str, str] = {}
    for day in range(1, case.sim_days + 1):
        clock = projects.START + timedelta(days=day)
        for world in (before, after):
            for key, source in case.sources.items():
                world.add_rows(key, source.daily(day))
        for phase in ("between runs", "after the refresh"):
            for world in (before, after):
                world.build(clock, tables=phase == "after the refresh")
            for sql in queries:
                try:
                    a = before.read(sql, clock)
                except Exception as error:  # noqa: BLE001 - a database error is a result
                    errors.setdefault("baseline_error", f"{type(error).__name__}: {str(error)[:120]}")
                    continue
                try:
                    b = after.read(sql, clock)
                except Exception as error:  # noqa: BLE001
                    errors.setdefault("rewrite_error", f"{type(error).__name__}: {str(error)[:120]}")
                    continue
                reads += 1
                if a[1] != b[1]:
                    found.append({"kind": "schema", "phase": phase, "day": day, "query": sql})
                elif Counter(a[0]) != Counter(b[0]):
                    again = before.read(sql, clock, unoptimized=True)[0], after.read(sql, clock, unoptimized=True)[0]
                    confirmed = Counter(again[0]) != Counter(again[1])
                    found.append({"kind": "rows" if confirmed else "optimizer", "phase": phase, "day": day, "query": sql})
    kinds = {f["kind"] for f in found}
    if "rewrite_error" in errors:
        outcome = "rewrite_error"
    elif "rows" in kinds:
        outcome = "different_rows"
    elif "schema" in kinds:
        outcome = "different_schema"
    elif "optimizer" in kinds:
        outcome = "optimizer_disagrees"
    elif "baseline_error" in errors:
        outcome = "baseline_error"
    else:
        outcome = "same_rows"
    record: dict = {"outcome": outcome, "reads_compared": reads, "differences": len(found)}
    if found:
        record["first_difference"] = found[0]
    if errors:
        record["errors"] = errors
    return record


def advisor_records(case: "projects.Case") -> list[dict]:
    """The advisor's candidates for one case, each executed on its own, and the chosen set executed together."""

    advice = advise(case.pipeline(), case.jobs(), sizes=case.sizes, pricing=Pricing(), schedules=case.schedules)
    payload = advice.to_json()
    recommended = {c["id"] for c in payload["recommendations"]}
    records = []
    for c in advice.candidates:
        change = {"store": frozenset([c["node"]])} if c["kind"] == "store_view" else {"unstore": frozenset([c["node"]])}
        record = {
            "case": case.name, "id": c["id"], "kind": c["kind"], "label": c["evidence"]["label"],
            "reason": c["evidence"]["reason"], "recommended": c["id"] in recommended, "chosen": c["chosen"],
            "saving_per_day": c["saving_per_day"], "saving_basis": c["saving_basis"],
            "assumptions": list(ADVISOR_ASSUMPTIONS), **execute_change(case, **change),
        }
        if c["evidence"].get("conditions"):
            record["conditions"] = c["evidence"]["conditions"]
        records.append(record)
    chosen = [c for c in advice.candidates if c["chosen"]]
    selection = {
        "case": case.name, "id": "selection", "kind": "selection", "members": [c["id"] for c in chosen],
        "saving_per_day": payload["selection"]["saving_per_day"],
        "listed_as_needs_proof": [c["id"] for c in payload["needs_proof"]],
        # anything the advisor counts as a saving (recommended or chosen) whose evidence is not proven
        "counted_unproven": sorted({c["id"] for c in [*payload["recommendations"], *chosen] if c["evidence"]["label"] != "proven"}),
    }
    if chosen:
        selection.update(execute_change(
            case,
            store=frozenset(c["node"] for c in chosen if c["kind"] == "store_view"),
            unstore=frozenset(c["node"] for c in chosen if c["kind"] == "unstore_table"),
        ))
    else:
        selection.update({"outcome": "same_rows", "reads_compared": 0, "differences": 0})
    records.append(selection)
    return records


WRONG_OUTCOMES = ("different_rows", "different_schema", "rewrite_error")


def summarize_advisor(records: list[dict]) -> dict:
    """Counts for the advisor records. Proof label and executed agreement are counted apart.

    ``wrong`` is a proven change that executed differently, a chosen set that executed differently, or an
    unproven change counted as a saving (recommended or chosen). A change that is not proven is reported by
    whether its refusal was warranted (``difference_seen``); that never counts as wrong.
    """

    singles = [r for r in records if r["kind"] != "selection"]
    selections = [r for r in records if r["kind"] == "selection"]
    by_label = Counter(r["label"] for r in singles)
    proven = [r for r in singles if r["label"] == "proven"]
    recommended = [r for r in singles if r["recommended"]]
    needs_proof = [r for r in singles if r["label"] in ("conditional", "unknown")]
    refused = [r for r in singles if r["label"] == "changes_results"]
    chosen_sets = [r for r in selections if r["members"]]
    counted_unproven = sum(len(r["counted_unproven"]) for r in selections)

    def differing(rows: list[dict]) -> int:
        return sum(r["outcome"] in WRONG_OUTCOMES for r in rows)

    return {
        "cases": len({r["case"] for r in records}),
        "candidates": len(singles),
        "store_view": sum(r["kind"] == "store_view" for r in singles),
        "unstore_table": sum(r["kind"] == "unstore_table" for r in singles),
        "proven": len(proven),
        "conditional": by_label["conditional"],
        "unknown": by_label["unknown"],
        "changes_results": len(refused),
        "recommended": len(recommended),
        "recommended_same_rows": sum(r["outcome"] == "same_rows" for r in recommended),
        "proven_same_rows": sum(r["outcome"] == "same_rows" for r in proven),
        "proven_different": differing(proven),
        "chosen_sets": len(chosen_sets),
        "chosen_sets_same_rows": sum(r["outcome"] == "same_rows" for r in chosen_sets),
        "chosen_sets_different": differing(chosen_sets),
        "needs_proof": len(needs_proof),
        "needs_proof_difference_seen": differing(needs_proof),
        "refused": len(refused),
        "refused_difference_seen": differing(refused),
        "unproven_counted_as_saving": counted_unproven,
        "not_judged": sum(r["outcome"] in ("baseline_error", "optimizer_disagrees") for r in records),
        "reads_compared": sum(r["reads_compared"] for r in records),
        "wrong": differing(proven) + differing(chosen_sets) + counted_unproven,
    }


def run_advisor(cases: list["projects.Case"] | None = None) -> tuple[dict, list[dict]]:
    records = [r for case in (cases or projects.cases()) for r in advisor_records(case)]
    return summarize_advisor(records), records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workload", action="append", help="DB=DIR of .sql files, run on database DB")
    parser.add_argument("--advisor", action="store_true", help="also judge the materialization advisor's store_view and unstore_table recommendations (offline, DuckDB)")
    parser.add_argument("--host", default="/tmp")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds per statement")
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--only", nargs="*", help="query names, or prefixes ending in /")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--rejudge", type=Path, help="reuse the recommendations of an earlier --out file and only execute them again")
    args = parser.parse_args(argv)
    if not args.workload and not args.advisor:
        parser.error("give --workload DB=DIR, --advisor, or both")

    advisor = None
    if args.advisor:
        advisor = run_advisor()
        print("advisor", json.dumps(advisor[0]))
        for r in advisor[1]:
            print(f"{r['case']:<10} {r['id']:<40} {r.get('label', ''):<16} {r['outcome']}", flush=True)
    if not args.workload:
        if args.out:
            args.out.write_text(json.dumps({"summary": {"advisor": advisor[0]}, "advisor": advisor[1]}, indent=1, default=str))
        return 1 if advisor[0]["wrong"] else 0

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
        extra = {"advisor": advisor[1]} if advisor else {}
        everything = {**summary, **({"advisor": advisor[0]} if advisor else {})}
        args.out.write_text(json.dumps({"summary": everything, "queries": records, **extra}, indent=1, default=str))
    wrong = sum(s["different_rows"] + s["different_schema"] + s["rewrite_errors"] for s in summary.values())
    wrong += advisor[0]["wrong"] if advisor else 0
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
