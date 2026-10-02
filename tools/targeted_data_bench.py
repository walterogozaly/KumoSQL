"""Targeted test data, multi-database checking and counterexample minimization.

For every original query this builds faulty variants (mutants, ``kumosql.query_mutants``)
and asks which checking strategy exposes each one as different from the original:

* ``single_seed``  one random database (seed 1), the cheapest check;
* ``random_8``     the engine's default eight random databases (seed 0 is empty);
* ``targeted``     databases built around the query's constants, joins and groups;
* ``suite``        corner cases, targeted and four random databases (the multi-database suite).

A mutant is *killed* by a strategy when the original and the mutant return
different result bags on a database that respects the declared NOT NULL columns
and keys. A mutant that no strategy kills is classified by the equivalence
prover: *proven equivalent* mutants are not mistakes and leave the denominator;
the rest stay as *survived, unclassified* and count against every strategy.
A stress run of 300 further random databases is a second look for survivors.

Killed mutants are then minimized (``kumosql.minimize``) and replayed from their
JSON form on a new engine.

    python tools/targeted_data_bench.py                    # development split, scores
    python tools/targeted_data_bench.py --split heldout    # the held-out split (run once)
    python tools/targeted_data_bench.py --write-results    # also update benchmarks/results and the README

Originals come from the SQLSolver benchmark queries in tests/fixtures/sqlsolver
(Apache-2.0) and a small university-schema set written for this eval
(``UNIVERSITY`` below). Calcite and Spark queries are the development split;
TPC-H and TPC-C queries are held out and are not used while building the
strategies. No LLM is involved at any point.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import logging
import multiprocessing
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import sqlglot  # noqa: E402

import sqlsolver_bench as ssb  # noqa: E402
from kumosql.minimize import minimize_failure, replay, to_json  # noqa: E402
from kumosql.query_mutants import Mutant, mutate  # noqa: E402
from kumosql.result_equivalence import (  # noqa: E402
    DataRules,
    DatasetRunner,
    ExecutionError,
    QueryTimeout,
    compare_outputs,
    generate_synthetic_dataset,
)
from kumosql.targeted_data import (  # noqa: E402
    LabeledDataset,
    database_suite,
    random_datasets,
    targeted_datasets,
)

logging.getLogger("sqlglot").setLevel(logging.ERROR)

DETAIL_DIR = ROOT / "benchmarks" / "targeted_data"
CONFIGS = ("single_seed", "random_8", "targeted", "suite")
STRESS_SEEDS = range(1000, 1150)
QUERY_TIMEOUT = 5.0
QUERY_BUDGET = 90.0

UNIVERSITY_SCHEMA = """
CREATE TABLE department (dept_name varchar(20) primary key not null, building varchar(15), budget int);
CREATE TABLE instructor (id int primary key not null, name varchar(20) not null, dept_name varchar(20), salary int);
CREATE TABLE course (course_id int primary key not null, title varchar(30), dept_name varchar(20), credits int);
CREATE TABLE student (id int primary key not null, name varchar(20) not null, dept_name varchar(20), tot_cred int);
CREATE TABLE takes (id int not null, course_id int not null, grade varchar(2), year int);
"""

UNIVERSITY = [
    "SELECT name FROM instructor WHERE salary > 80000",
    "SELECT name, salary FROM instructor WHERE dept_name = 'Physics' AND salary >= 70000",
    "SELECT i.name, d.building FROM instructor i JOIN department d ON i.dept_name = d.dept_name WHERE d.budget > 50000",
    "SELECT d.dept_name, COUNT(*) AS n FROM department d JOIN instructor i ON i.dept_name = d.dept_name GROUP BY d.dept_name HAVING COUNT(*) > 1",
    "SELECT dept_name, AVG(salary) AS avg_salary FROM instructor GROUP BY dept_name",
    "SELECT dept_name, MAX(salary) AS top FROM instructor WHERE salary < 120000 GROUP BY dept_name",
    "SELECT s.name FROM student s WHERE s.id NOT IN (SELECT id FROM takes WHERE year = 2020)",
    "SELECT s.name FROM student s WHERE EXISTS (SELECT 1 FROM takes t WHERE t.id = s.id AND t.grade = 'A')",
    "SELECT DISTINCT dept_name FROM course WHERE credits BETWEEN 3 AND 4",
    "SELECT c.title, COUNT(t.id) AS enrolled FROM course c LEFT JOIN takes t ON t.course_id = c.course_id GROUP BY c.title",
    "SELECT name FROM instructor WHERE dept_name = 'Physics' UNION SELECT name FROM student WHERE dept_name = 'Physics'",
    "SELECT name FROM student WHERE tot_cred > 60 OR dept_name = 'Music'",
    "SELECT i.name FROM instructor i WHERE i.salary > (SELECT AVG(salary) FROM instructor)",
    "SELECT dept_name, SUM(credits) AS credits FROM course GROUP BY dept_name HAVING SUM(credits) >= 8",
    "SELECT s.name, c.title FROM student s JOIN takes t ON s.id = t.id JOIN course c ON c.course_id = t.course_id WHERE t.year >= 2019 AND t.grade IS NOT NULL",
    "SELECT name, salary * 12 AS yearly FROM instructor WHERE dept_name IS NOT NULL",
]


# -- corpus ---------------------------------------------------------------------


def _schema_and_rules(tables):
    kinds = {"VARCHAR": "STRING", "CHAR": "STRING", "TEXT": "STRING", "DECIMAL": "FLOAT64", "DOUBLE": "FLOAT64", "FLOAT": "FLOAT64", "NUMERIC": "FLOAT64", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "DATETIME": "TIMESTAMP"}
    schema, rules = {}, {}
    for table in tables.values():
        schema[table.name] = {c.name: kinds.get(c.type.split("(")[0], "INT64") for c in table.columns}
        keys = tuple(k for k in ([table.primary_key] if table.primary_key else []) + list(table.unique))
        rules[table.name] = DataRules(frozenset(c.name for c in table.columns if c.not_null), keys)
    return schema, rules


def _constraints(tables):
    from kumosql.smt_equivalence import TableConstraints

    return {
        t.name: TableConstraints(
            not_null=frozenset(c.name for c in t.columns if c.not_null),
            keys=tuple(k for k in ([t.primary_key] if t.primary_key else []) + list(t.unique)),
        )
        for t in tables.values()
    }


UNSAFE_CASES = ROOT / "tests" / "fixtures" / "unsafe_rewrite_cases.jsonl"


def _unsafe_items(split: str) -> tuple[list[dict], dict]:
    """Pairs from the unsafe-rewrite fuzzing suite that may differ (``either``/``different``): the left query is
    the original and the right one the faulty variant. Equivalent-by-construction pairs are not faults."""

    import unsafe_fuzz

    schema = unsafe_fuzz.SCHEMA
    suite = {"schema": schema, "rules": {}, "constraints": {}, "tables": {}}
    items = []
    for line in UNSAFE_CASES.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        held = bool(case["heldout"])
        if case["expect"] == "equivalent" or split not in ("all", "heldout" if held else "dev"):
            continue
        items.append({"suite": "unsafe", "index": case["id"], "source": case["left"], "variant": case["right"], "family": case["family"], "split": "heldout" if held else "dev"})
    return items, suite


def build_corpus(split: str) -> tuple[list[dict], dict]:
    """Original queries (BigQuery SQL) with their suite name and split, plus each suite's schema."""

    suites = {}
    items: list[dict] = []
    wanted = {"dev": ("calcite", "spark", "university"), "heldout": ("tpch", "tpcc"), "all": ("calcite", "spark", "university", "tpch", "tpcc")}[split]
    for name in wanted:
        if name == "university":
            tables = ssb_tables_from_text(UNIVERSITY_SCHEMA)
            texts = UNIVERSITY
            read = "mysql"
        else:
            pairs_file, schema_file = ssb.SUITES[name]
            tables = ssb.load_schema(ssb.FIXTURES / schema_file)
            lines = [l.strip() for l in (ssb.FIXTURES / pairs_file).read_text(encoding="utf-8").splitlines() if l.strip()]
            texts = list(dict.fromkeys(lines))
            read = "mysql"
        schema, rules = _schema_and_rules(tables)
        suites[name] = {"schema": schema, "rules": rules, "constraints": _constraints(tables), "tables": tables}
        for index, text in enumerate(texts):
            items.append({"suite": name, "index": index, "source": text, "split": "heldout" if name in ("tpch", "tpcc") else "dev"})
    return items, suites


def ssb_tables_from_text(text: str):
    # A private temporary file: parallel test workers import this module at the same time.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "university.sql"
        path.write_text(text, encoding="utf-8")
        return ssb.load_schema(path)


# -- per-query work --------------------------------------------------------------


def _confirmed(runner, original, mutant, labeled) -> bool:
    """Both sides give the same answer twice on the killing database (no nondeterminism)."""

    try:
        a1, a2 = runner.run(original, labeled.dataset), runner.run(original, labeled.dataset)
        b1, b2 = runner.run(mutant, labeled.dataset), runner.run(mutant, labeled.dataset)
    except ExecutionError:
        return False
    return compare_outputs(a1, a2, check_column_names=False)[0] and compare_outputs(b1, b2, check_column_names=False)[0]


def _prove_equivalent(original, mutant, suite) -> bool:
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    schema = {name: list(cols) for name, cols in suite["schema"].items()}
    types = {t.name: {c.name: c.type for c in t.columns} for t in suite["tables"].values()}
    try:
        result = prove_equivalent_algebraic(
            original, mutant, schema=schema, constraints=suite["constraints"], types=types,
            compare_names=False, dialect="bigquery", exact_arithmetic=True, timeout_ms=3000,
        )
    except Exception:
        return False
    return result.proven


def _sweep(runner, original, entries, datasets, name, deadline):
    """Record on each live entry the first of ``datasets`` that tells it from ``original``."""

    for labeled in datasets:
        live = [e for e in entries if e["outcome"] is None and name not in e["kills"]]
        if not live:
            return
        if time.monotonic() > deadline:
            for e in live:
                e["outcome"] = "timeout"
            return
        try:
            a = runner.run(original, labeled.dataset, timeout=QUERY_TIMEOUT)
        except QueryTimeout:
            for e in live:
                e["outcome"] = "timeout"
            return
        except ExecutionError:
            continue
        for entry in live:
            try:
                b = runner.run(entry["sql"], labeled.dataset, timeout=QUERY_TIMEOUT)
            except QueryTimeout:
                entry["outcome"] = "timeout"
                continue
            except ExecutionError:
                entry["outcome"] = "error"
                continue
            if compare_outputs(a, b, check_column_names=False)[0]:
                continue
            if not _confirmed(runner, original, entry["sql"], labeled):
                entry["outcome"] = "unknown"
                entry["note"] = "nondeterministic"
                continue
            entry["kills"][name] = {"label": labeled.label, "rows": labeled.rows}


def process(item_and_suite) -> dict:
    item, suite = item_and_suite
    record = {"suite": item["suite"], "index": item["index"], "split": item["split"], "mutants": [], "status": "ok"}
    start = time.monotonic()
    try:
        if item["suite"] == "unsafe":
            original = sqlglot.transpile(item["source"], read="bigquery", write="bigquery")[0]
            mutants = [Mutant(item["family"], 0, sqlglot.transpile(item["variant"], read="bigquery", write="bigquery")[0])]
        elif item["suite"] == "university":
            original = sqlglot.transpile(item["source"], read="mysql", write="bigquery")[0]
        else:
            original = ssb.to_dialect(item["source"], "bigquery")
        if item["suite"] != "unsafe":
            mutants = mutate(original)
    except Exception as error:
        record["status"] = "unsupported"
        record["reason"] = f"translate: {type(error).__name__}"
        return record
    schema, rules = suite["schema"], suite["rules"]
    record["original"] = original
    deadline = start + QUERY_BUDGET
    try:
        runner = DatasetRunner(schema)
        runner.run(original, random_datasets(schema, rules, [0])[0].dataset)
    except Exception as error:
        record["status"] = "unsupported"
        record["reason"] = f"original does not run: {str(error)[:80]}"
        return record
    try:
        t0 = time.monotonic()
        configs = {
            "single_seed": random_datasets(schema, rules, [1]),
            "random_8": random_datasets(schema, rules, range(8)),
        }
        t1 = time.monotonic()
        configs["targeted"] = targeted_datasets(original, schema, rules)
        configs["suite"] = database_suite(original, schema, rules)
        t2 = time.monotonic()
        record["generation_seconds"] = {"random_8": t1 - t0, "suite": t2 - t1}
        entries = [{"operator": m.operator, "sql": m.sql, "kills": {}, "outcome": None} for m in mutants]
        for entry in entries:  # a mutant that is not even a valid query is discarded
            try:
                runner.run(entry["sql"], configs["single_seed"][0].dataset)
            except QueryTimeout:
                entry["outcome"] = "timeout"
            except ExecutionError:
                entry["outcome"] = "error"
        seconds = {}
        for name in CONFIGS:
            t = time.monotonic()
            _sweep(runner, original, entries, configs[name], name, deadline)
            seconds[name] = time.monotonic() - t
        record["check_seconds"] = seconds
        survivors = [e for e in entries if e["outcome"] is None and not e["kills"]]
        _sweep(runner, original, survivors, random_datasets(schema, rules, STRESS_SEEDS), "stress", deadline)
        for entry in entries:
            if entry["outcome"] is not None:
                continue
            if any(k != "stress" for k in entry["kills"]):
                entry["outcome"] = "killed"
            elif entry["kills"]:
                entry["outcome"] = "killed_by_stress_only"
            elif _prove_equivalent(original, entry["sql"], suite):
                entry["outcome"] = "equivalent"
            else:
                entry["outcome"] = "survived"
        record["mutants"] = entries
        runner.close()
    except Exception as error:  # a crash is an error, never a score
        record["status"] = "error"
        record["reason"] = f"{type(error).__name__}: {str(error)[:80]}"
    record["seconds"] = time.monotonic() - start
    return record


def minimize_task(args) -> dict:
    original, mutant, suite, sample_key = args
    schema, rules = suite["schema"], suite["rules"]
    dataset = None
    for labeled in database_suite(original, schema, rules):
        with DatasetRunner(schema) as runner:
            try:
                a, b = runner.run(original, labeled.dataset), runner.run(mutant, labeled.dataset)
            except ExecutionError:
                continue
        if not compare_outputs(a, b, check_column_names=False)[0]:
            dataset = labeled.dataset
            break
    if dataset is None:
        return {"key": sample_key, "outcome": "no_failure"}
    try:
        case = minimize_failure(original, mutant, schema, dataset, rules, time_limit=30.0)
    except Exception as error:
        return {"key": sample_key, "outcome": "error", "reason": f"{type(error).__name__}: {str(error)[:80]}"}
    document = json.loads(json.dumps(to_json(case)))
    return {
        "key": sample_key,
        "outcome": "minimized" if replay(document) else "not_replayable",
        "before": case.before.__dict__,
        "after": case.after.__dict__,
        "seconds": case.seconds,
    }


# -- scoring ----------------------------------------------------------------------


def _median(values):
    return statistics.median(values) if values else 0


def score(records: list[dict]) -> dict:
    queries = Counter(r["status"] for r in records)
    mutants = [m for r in records if r["status"] == "ok" for m in r["mutants"]]
    outcomes = Counter(m["outcome"] for m in mutants)
    denominator = len(mutants) - outcomes["equivalent"] - outcomes["error"] - outcomes["timeout"] - outcomes["unknown"]
    per_config = {}
    for name in CONFIGS:
        killed = [m for m in mutants if name in m["kills"]]
        per_config[name] = {
            "killed": len(killed),
            "score": len(killed) / denominator if denominator else 0.0,
            "median_counterexample_rows": _median([m["kills"][name]["rows"] for m in killed]),
            "median_query_seconds": _median([r["check_seconds"][name] for r in records if r["status"] == "ok" and "check_seconds" in r]),
        }
    by_operator: dict[str, dict] = defaultdict(lambda: {"mutants": 0, **{c: 0 for c in CONFIGS}, "survived": 0, "equivalent": 0})
    for m in mutants:
        row = by_operator[m["operator"]]
        row["mutants"] += 1
        for name in CONFIGS:
            row[name] += name in m["kills"]
        row["survived"] += m["outcome"] == "survived"
        row["equivalent"] += m["outcome"] == "equivalent"
    escaped = [m for m in mutants if "suite" in m["kills"] and "single_seed" not in m["kills"]]
    escaped8 = [m for m in mutants if "suite" in m["kills"] and "random_8" not in m["kills"]]
    return {
        "queries": dict(queries),
        "mutants": len(mutants),
        "outcomes": dict(outcomes),
        "denominator": denominator,
        "configs": per_config,
        "escape_single_seed_caught_by_suite": len(escaped),
        "escape_random_8_caught_by_suite": len(escaped8),
        "by_operator": {k: by_operator[k] for k in sorted(by_operator)},
    }


def summarize_minimization(results: list[dict]) -> dict:
    ok = [r for r in results if r["outcome"] == "minimized"]
    return {
        "attempted": len(results),
        "outcomes": dict(Counter(r["outcome"] for r in results)),
        "rows_before_median": _median([r["before"]["rows"] for r in ok]),
        "rows_after_median": _median([r["after"]["rows"] for r in ok]),
        "rows_before_total": sum(r["before"]["rows"] for r in ok),
        "rows_after_total": sum(r["after"]["rows"] for r in ok),
        "nodes_before_total": sum(r["before"]["sql_nodes"] for r in ok),
        "nodes_after_total": sum(r["after"]["sql_nodes"] for r in ok),
        "chars_before_total": sum(r["before"]["sql_chars"] for r in ok),
        "chars_after_total": sum(r["after"]["sql_chars"] for r in ok),
        "median_seconds": _median([r["seconds"] for r in ok]),
        "max_seconds": max([r["seconds"] for r in ok] or [0]),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--split", choices=["dev", "heldout", "all"], default="dev")
    parser.add_argument("--unsafe-only", action="store_true", help="score only the unsafe-rewrite pairs")
    parser.add_argument("--unsafe", action="store_true", help="add the unsafe-rewrite fuzzing pairs as faulty variants")
    parser.add_argument("--limit", type=int, default=None, help="only the first N originals per suite")
    parser.add_argument("--jobs", type=int, default=max(1, multiprocessing.cpu_count() // 2))
    parser.add_argument("--minimize", type=int, default=150, help="how many killed mutants to minimize (0 skips)")
    parser.add_argument("--out", type=Path, default=DETAIL_DIR)
    args = parser.parse_args(argv)

    items, suites = build_corpus(args.split)
    if args.unsafe_only:
        items = []
    if args.unsafe or args.unsafe_only:
        unsafe_items, unsafe_suite = _unsafe_items(args.split)
        items += unsafe_items
        suites["unsafe"] = unsafe_suite
    if args.limit:
        counts: Counter = Counter()
        kept = []
        for item in items:
            counts[item["suite"]] += 1
            if counts[item["suite"]] <= args.limit:
                kept.append(item)
        items = kept
    start = time.time()
    work = [(item, suites[item["suite"]]) for item in items]
    if args.jobs > 1:
        with multiprocessing.Pool(args.jobs) as pool:
            records = pool.map(process, work, chunksize=2)
    else:
        records = [process(w) for w in work]
    elapsed = time.time() - start
    summary = {"split": args.split, "seconds": elapsed, **score(records)}
    suite_names = sorted({r["suite"] for r in records})
    summary["per_suite"] = {name: score([r for r in records if r["suite"] == name]) for name in suite_names}
    summary["unsupported_reasons"] = dict(Counter(r.get("reason", "")[:60] for r in records if r["status"] != "ok").most_common(8))

    if args.minimize:
        killed = [(r, m) for r in records if r["status"] == "ok" for m in r["mutants"] if m["outcome"] == "killed"]
        step = max(len(killed) // args.minimize, 1)
        sample = killed[::step][: args.minimize]
        tasks = [(r["original"], m["sql"], suites[r["suite"]], f"{r['suite']}:{r['index']}") for r, m in sample]
        if args.jobs > 1:
            with multiprocessing.Pool(args.jobs) as pool:
                minimized = pool.map(minimize_task, tasks, chunksize=1)
        else:
            minimized = [minimize_task(t) for t in tasks]
        summary["minimization"] = summarize_minimization(minimized)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"summary-{args.split}.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (args.out / f"detail-{args.split}.json").write_text(json.dumps(records), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("per_suite", "by_operator")}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
