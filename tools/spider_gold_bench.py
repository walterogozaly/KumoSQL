"""Spider 1.0's gold queries as rewrite inputs: every KumoSQL rewrite is checked on databases that respect Spider's keys.

Spider (https://github.com/taoyds/spider, Apache-2.0 repository, CC BY-SA 4.0 data; Yu et al., "Spider: A
Large-Scale Human-Labeled Dataset for Complex and Cross-Domain Semantic Parsing and Text-to-SQL Task", EMNLP
2018) publishes a gold SQLite query for each of its questions: 1,034 dev and 7,000 train questions that
share 564 and 3,981 distinct (database, query) pairs, on 20 and 140 databases. No model is called.

KumoSQL's rules read BigQuery, so a query goes SQLite -> BigQuery (sqlglot, identifiers lower-cased, as
SQLite ignores their case) -> each registered rule on its own, the canonical pipeline and the proof-gated
``query_optimizer`` -> back to SQLite. Each output that is not the input once re-rendered is an **applied
rewrite**; it is **trusted** when KumoSQL would apply it (the rule's own verification passed, or the
optimizer returned it). Two checks follow, neither of which reads the rewrite's label:

* **checked**: SQLite returns the same result for the query and its rewrite on 1,000 random databases that
  respect the schema (``tools/spider_check.py``: the listed primary keys unique and not NULL, foreign keys
  pointing at existing rows, results compared as Spider does, tie-safe), and on the targeted and z3 bounded
  databases for an unordered query;
* **proved**: the algebraic prover (SQLite dialect, no keys) proves the pair.

A trusted rewrite whose result changes on a valid database is **wrong**. An untrusted rewrite that changes
a result is a **caught** one (the gate worked). The round trip through BigQuery is sqlglot's; it is checked
on 200 databases and reported apart (``translation``), and the rewrite is always compared with the
round-tripped input, never with Spider's text.

Keys. ``tables.json`` lists only the first column of a composite primary key, so the prover and the
optimizer are given no key and no NOT NULL (databases are built with the listed keys, unique in every
Spider database). A rewrite that needs a key is therefore never applied here.

The data is downloaded at run time from a pinned commit and never committed (``tools/spider_data.py``).
Spider's databases are on blocked hosts, so every database is one KumoSQL builds from ``tables.json``.
One database in five, by the SHA-1 of its name, is held out (every query on it); a run reads only the
development databases unless ``--split all`` or ``--split held-out`` is given.

    python tools/spider_gold_bench.py --set dev --jobs 2           # the development databases
    python tools/spider_gold_bench.py --set both --write-results   # every distinct query of dev and train
    python tools/spider_gold_bench.py --show wrong,caught,semantic
    python tools/spider_gold_bench.py --check-sources
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import multiprocessing
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import sqlglot
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

import llm_sql_solver_bench as solver
import spider_check as check
import spider_data

QUERY_TIMEOUT = 600  # seconds for one query through every transformation (one z3 call can ignore its own limit)
OPTIMIZER_BUDGET_S = 20.0
BQ_TYPES = {"INTEGER": "int64", "TEXT": "string"}
# Rewrites that change a query beyond layout and are trusted must never change a result: kept as regressions
WRONG: dict[str, str] = {}


def normalize(sql: str) -> str:
    return " ".join(sql.strip().rstrip(";").split())


@dataclass
class Gold:
    source: str  # "dev" or "train"
    index: int
    database: str
    sql: str
    questions: int = 1  # questions of the file that share this query

    @property
    def id(self) -> str:
        return f"{self.source}-{self.index:04d}"

    @property
    def held_out(self) -> bool:
        return database_held_out(self.database)


def database_held_out(database: str) -> bool:
    return int(hashlib.sha1(f"spider-gold\n{database}".encode()).hexdigest(), 16) % 5 == 0


def load_gold(source: str, path: Path | None = None) -> list[Gold]:
    """The distinct (database, query) pairs of ``dev.json`` or ``train_spider.json``, in file order."""

    queries: dict[tuple[str, str], Gold] = {}
    for example in json.loads((path or spider_data.fetch("dev.json" if source == "dev" else "train_spider.json")).read_text(encoding="utf-8")):
        key = (example["db_id"], normalize(example["query"]))
        if key in queries:
            queries[key].questions += 1
        else:
            queries[key] = Gold(source, len(queries), example["db_id"], normalize(example["query"]))
    return list(queries.values())


def to_bigquery(sqlite_sql: str) -> str:
    """SQLite to BigQuery with sqlglot, identifiers lower-cased."""

    tree = sqlglot.parse_one(sqlite_sql, read="sqlite")
    for identifier in tree.find_all(exp.Identifier):
        identifier.set("this", identifier.this.lower())
    return tree.sql(dialect="bigquery")


def to_sqlite(bigquery_sql: str) -> str:
    return sqlglot.parse_one(bigquery_sql, read="bigquery").sql(dialect="sqlite")


def catalog(schema: spider_data.Schema):
    """The optimizer's catalog: columns and types only, no key and no NOT NULL (see the module docstring)."""

    from kumosql.query_optimizer import Catalog

    return Catalog(
        columns={t: list(cols) for t, cols in schema.tables.items()},
        types={t: {c: BQ_TYPES.get(k, "string") for c, k in cols.items()} for t, cols in schema.tables.items()},
    )


def transformations() -> list[str]:
    from kumosql import engine

    return [*engine.available_rules(), "pipeline", "optimize"]


def apply(name: str, bigquery: str, schema: spider_data.Schema) -> tuple[str | None, bool, str]:
    """(output, trusted, label): a trusted output is one KumoSQL would apply."""

    from kumosql import query_optimizer, rewrite

    if name == "optimize":
        outcome = query_optimizer.optimize(
            bigquery, catalog(schema), dialect="bigquery", budget_s=OPTIMIZER_BUDGET_S, deletion_budget_s=OPTIMIZER_BUDGET_S / 2,
        )
        return outcome.sql, outcome.sql is not None, "proven" if outcome.sql else "no rewrite"
    result = rewrite.apply_rules(rewrite.canonical_rule_order(), bigquery) if name == "pipeline" else rewrite.apply_rule(name, bigquery)
    return result.sql, result.success, result.verification.status.value


def check_rewrite(schema: spider_data.Schema, original: str, rewritten: str) -> dict:
    """What happens to the result when ``original`` becomes ``rewritten``: same text, agree, differs or error."""

    if solver._tree(rewritten) is not None and solver.same_query(original, rewritten):
        return {"check": "same text"}  # layout only: the same query once re-rendered
    witness: dict = {}
    how = check.refute(schema, original, rewritten, witness)
    if how == "error":
        return {"check": "error"}
    if how == "agree":
        return {"check": "agree", "proved": check.prove(schema, original, rewritten)}
    return {"check": "differs", "how": how, "witness": check.shrink(schema, original, rewritten, witness) if witness else None}


def rewrite_query(task: tuple[Gold, spider_data.Schema]) -> dict:
    gold, schema = task
    started = time.time()
    out: dict = {"id": gold.id, "source": gold.source, "database": gold.database, "held_out": gold.held_out, "questions": gold.questions, "rewrites": {}}
    original_sql = solver.adapt(gold.sql, schema.tables)
    if check.differs(schema, original_sql, original_sql, trials=1) == "error":
        out.update(translation="unsupported", detail="SQLite rejects the gold query on the Spider schema")
        return out
    try:
        bigquery = to_bigquery(original_sql)
        original = to_sqlite(bigquery)
    except sqlglot.errors.SqlglotError as error:
        out.update(translation="unsupported", detail=f"sqlglot: {str(error)[:100]}")
        return out
    # the round trip is sqlglot's, not a KumoSQL rewrite: reported apart, and every rewrite is compared with its result
    out["translation"] = {"agree": "same", "differs": "differs", "error": "error"}[check.differs(schema, original_sql, original, trials=200)]
    for name in transformations():
        began = time.time()
        try:
            new, trusted, label = apply(name, bigquery, schema)
        except Exception as error:  # a crash is reported, never counted as a rewrite
            out["rewrites"][name] = {"changed": False, "crash": f"{type(error).__name__}: {str(error)[:100]}"}
            continue
        record: dict = {"changed": bool(new) and normalize(new) != normalize(bigquery), "trusted": trusted, "label": label}
        if record["changed"]:
            try:
                rewritten = to_sqlite(new)
            except sqlglot.errors.SqlglotError:
                rewritten = None
            if rewritten is None:
                record["check"] = "error"
            else:
                record.update(check_rewrite(schema, original, rewritten))
                if record["check"] == "differs":
                    record["sql"] = new
            record["wrong"] = trusted and record["check"] == "differs"
        record["seconds"] = round(time.time() - began, 2)
        out["rewrites"][name] = record
    out["seconds"] = round(time.time() - started, 2)
    return out


def _worker(task, connection) -> None:
    try:
        connection.send(rewrite_query(task))
    finally:
        connection.close()


def rewrite_guarded(task: tuple[Gold, spider_data.Schema], timeout: float = QUERY_TIMEOUT) -> dict:
    """``rewrite_query`` in its own process: a query that runs past ``timeout`` seconds, or crashes z3, is unknown."""

    gold = task[0]
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(task, sender), daemon=True)
    started = time.time()
    process.start()
    sender.close()
    result, how = None, "timeout"
    if receiver.poll(timeout):
        try:
            result = receiver.recv()
        except EOFError:
            how = "crash"
    process.kill()
    process.join()
    if result is not None:
        return result
    return {
        "id": gold.id, "source": gold.source, "database": gold.database, "held_out": gold.held_out, "questions": gold.questions,
        "translation": how, "rewrites": {}, "seconds": round(time.time() - started, 2),
    }


def run(queries: list[Gold], schemas: dict[str, spider_data.Schema], jobs: int = 1) -> list[dict]:
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        return list(pool.map(rewrite_guarded, [(q, schemas[q.database]) for q in queries]))


def applied(record: dict) -> bool:
    """A rewrite that changes the query beyond layout."""

    return bool(record.get("changed")) and record.get("check") != "same text"


def summarize(results: list[dict]) -> dict:
    out: dict = {"queries": len(results), "translation": dict(Counter(r.get("translation") for r in results))}
    out["translated"] = sum(r.get("translation") in ("same", "differs", "error") for r in results)
    rules: dict[str, Counter] = {}
    for result in results:
        for name, record in result["rewrites"].items():
            row = rules.setdefault(name, Counter())
            row["crash"] += "crash" in record
            if not record.get("changed"):
                continue
            row["changed"] += 1
            row["trusted"] += record["trusted"]
            row[record["check"]] += 1
            row["wrong"] += record["wrong"]
            row["caught"] += (not record["trusted"]) and record["check"] == "differs"
            row["proved"] += bool(record.get("proved"))
    out["rules"] = {name: dict(counts) for name, counts in rules.items()}
    records = [rec for r in results for rec in r["rewrites"].values()]
    out["layout_only"] = sum(bool(rec.get("changed")) and rec["check"] == "same text" for rec in records)
    out["applied"] = sum(applied(rec) for rec in records)
    out["applied_trusted"] = sum(applied(rec) and rec["trusted"] for rec in records)
    out["applied_trusted_agree"] = sum(applied(rec) and rec["trusted"] and rec["check"] == "agree" for rec in records)
    out["applied_trusted_proved"] = sum(applied(rec) and rec["trusted"] and bool(rec.get("proved")) for rec in records)
    out["caught"] = sum(applied(rec) and not rec["trusted"] and rec["check"] == "differs" for rec in records)
    out["queries_rewritten"] = sum(any(applied(rec) and rec["trusted"] for rec in r["rewrites"].values()) for r in results)
    out["wrong"] = sum(bool(rec.get("wrong")) for rec in records)
    return out


def outcome(result: dict) -> str:
    """The scoreboard outcome of one query: proven (a trusted rewrite beyond layout), unsupported, timeout/error or unknown (nothing to rewrite)."""

    translation = result.get("translation")
    if translation == "unsupported":
        return "unsupported"
    if translation in ("timeout", "crash"):
        return "timeout" if translation == "timeout" else "error"
    if any(applied(rec) and rec["trusted"] for rec in result["rewrites"].values()):
        return "proven"
    return "unknown"


def results_row(results: list[dict], sources: str) -> dict:
    from bench_common import today

    summary = summarize(results)
    held = summarize([r for r in results if r["held_out"]])
    coverage = Counter(outcome(r) for r in results)
    return {
        "suite": "Spider gold queries as rewrite inputs",
        "order": 36,
        "size": len(results),
        "score": (
            f"{summary['queries_rewritten']}/{len(results)} queries rewritten beyond layout, {summary['applied_trusted']} trusted rewrites "
            f"({summary['applied_trusted_agree']} agree on generated databases), {summary['wrong']} wrong"
        ),
        "metric": f"Distinct gold queries of Spider's {sources}, translated to BigQuery and run through every KumoSQL rule, the cleanup pipeline and the proof-gated optimizer; a rewrite that is not layout only is run against its input on databases that respect Spider's listed keys and foreign keys.",
        "evidence": "executed",
        "correctness": "Wrong is a trusted rewrite (verified by the rule, or returned by the optimizer) whose result differs from its input on a generated database that respects the listed keys and foreign keys (1,000 random databases, then the targeted and bounded searches for unordered queries). Rewrites the gate refused that do change a result are counted as caught.",
        "coverage": {k: coverage[k] for k in ("proven", "unknown", "unsupported", "timeout", "error") if coverage[k]},
        "held_out": f"{held['queries_rewritten']}/{held['queries']} queries rewritten, {held['applied_trusted']} trusted rewrites, {held['wrong']} wrong",
        "docs": "docs/evals/spider-gold.md",
        "command": "python tools/spider_gold_bench.py --set both --write-results",
        "date": today(),
        "caveats": (
            f"Downloaded at run time from a pinned commit (CC BY-SA 4.0 data) and never committed. Spider's databases are blocked here, so checks run on databases KumoSQL builds from tables.json. "
            f"The prover and optimizer get no key (tables.json lists only the first column of a composite key), so key-dependent rewrites are never applied. Queries go through BigQuery and back (sqlglot); "
            f"the round trip changed a result on {summary['translation'].get('differs', 0)} queries, and rewrites are compared with the round-tripped query. "
            f"{summary['layout_only']} outputs that only re-render the query are not counted. {summary['caught']} untrusted rewrites changed a result and were refused by the gate. "
            f"The held-out databases ({sum(database_held_out(d) for d in {r['database'] for r in results})} of {len({r['database'] for r in results})}) were scored once, in the final run."
        ),
    }


def check_sources() -> list[str]:
    problems = spider_data.check_sources(("tables.json", "dev.json", "train_spider.json"))
    if not problems:
        dev, train = load_gold("dev"), load_gold("train")
        if (len(dev), len(train)) != (564, 3979):
            problems.append(f"expected 564 dev and 3979 train distinct queries, found {len(dev)} and {len(train)}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--set", choices=("dev", "train", "both"), default="dev", help="which of Spider's files to read (default dev)")
    parser.add_argument("--split", choices=solver.SPLITS, default=None, help="dev databases (default), held-out databases (final scoring only) or all")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--limit", type=int, default=0, help="the first N queries only (a smoke run)")
    parser.add_argument("--show", default="", help="comma-separated: wrong, caught (untrusted rewrites that change a result), semantic (every rewrite beyond layout)")
    parser.add_argument("--json", help="write every result to this file")
    parser.add_argument("--check-sources", action="store_true", help="download the pinned files and check their digests and counts")
    parser.add_argument("--write-results", action="store_true", help="update benchmarks/results/spider-gold.json (scores every query: --set both --split all)")
    args = parser.parse_args(argv)
    if args.check_sources:
        problems = check_sources()
        print("\n".join(problems) or "the pinned files match")
        return 1 if problems else 0
    if args.write_results and (args.set != "both" or args.limit):
        parser.error("--write-results scores every query: use --set both without --limit")
    try:
        split = solver.choose_split(args.split, args.write_results)
    except ValueError as error:
        parser.error(str(error))
    from bench_common import quiet, write_results

    quiet()
    schemas = spider_data.load_schemas()
    queries = [q for source in (("dev", "train") if args.set == "both" else (args.set,)) for q in load_gold(source)]
    queries = [q for q in queries if split == "all" or q.held_out == (split == "held-out")]
    if args.limit:
        queries = queries[: args.limit]
    started = time.time()
    results = run(queries, schemas, args.jobs)
    for part in ("dev", "held-out") + (("all",) if split == "all" else ()):
        rows = results if part == "all" else [r for r in results if r["held_out"] == (part == "held-out")]
        if rows:
            summary = summarize(rows)
            summary.pop("rules")
            print(f"{part:9} " + json.dumps(summary))
    summary = summarize(results)
    for name, counts in summary["rules"].items():
        print(f"{name:26} " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()) if v))
    print(f"{len(results)} queries ({split}) in {time.time() - started:.0f}s")
    show = {s.strip() for s in args.show.split(",") if s.strip()}
    by_id = {q.id: q for q in queries}
    for result in results:
        for name, record in result["rewrites"].items():
            bad = record.get("wrong") or (record.get("check") == "differs" and "caught" in show) or (applied(record) and "semantic" in show)
            if bad:
                print(f"\n{result['id']} [{result['database']}] {name} {record['label']} {record['check']}{' WRONG' if record.get('wrong') else ''}")
                if result["held_out"] and split != "held-out":
                    print("  held out: rerun with --split held-out to see the SQL")
                else:
                    print(f"  gold: {by_id[result['id']].sql}\n  rewrite: {record.get('sql', '')}")
                    if record.get("witness") is not None:
                        print(f"  database: {json.dumps(record['witness'])}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1), encoding="utf-8")
    if args.write_results:
        write_results("spider-gold", results_row(results, "dev and train files"))
    return 1 if summary["wrong"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
