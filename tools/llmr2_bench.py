"""Scale test of KumoSQL's rewrites on the LLM-R2 query sets (TPC-H, DSB, synthetic JOB).

LLM-R2 (Li et al., VLDB 2025; github.com/DAMO-NLP-SG/LLM-R2) ships large pools of queries over the
TPC-H, DSB (TPC-DS schema) and IMDB (JOB) databases, split into train and test files, for learning
which rewrite rules make a query faster. Its labels are PostgreSQL latencies, which are not used
here. Instead every query is run through KumoSQL's rewrites and each changed query is executed
against its original in DuckDB on the real data, as in ``tools/transformation_bench.py``:

* **correctness**: a rewrite KumoSQL marks *proven* that returns different rows is wrong;
* **coverage**: queries each transformation changed, proven, and verified by execution;
* **usefulness**: proven rewrites that DuckDB plans differently and that run at least 5% faster;
* **performance**: transformation time per query.

The transformations are the cleanup rules in their canonical order without the formatter (which
only changes layout; ``transformation_bench.py`` measures it) and ``lift_subqueries`` on its own.
The test files are the held-out split: develop on ``--split train`` and run ``--split test`` for the
final score. The query files have no licence, so they are fetched at a pinned commit and never
committed:

    python tools/benchmark_corpora.py fetch llm-r2 tpch-data tpcds-data job-data
    python tools/llmr2_bench.py --split train --log train.jsonl
    python tools/llmr2_bench.py --split test --log test.jsonl --json test.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import statistics
import sys
import time
from collections import Counter, defaultdict
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import sqlglot  # noqa: E402

import benchmark_corpora as corpora  # noqa: E402
import transformation_bench as tb  # noqa: E402
from kumosql import rewrite  # noqa: E402

DATASETS = {"tpch": "tpch", "dsb": "tpcds", "job_syn": "job"}  # LLM-R2 name -> schema and DuckDB file
SPLITS = ("train", "test")
MAX_ROWS = 200_000  # larger results are compared by an order-independent hash instead
QUERY_TIMEOUT_S = 300


def transformations() -> dict[str, tuple[str, ...]]:
    rules = tuple(r for r in rewrite.canonical_rule_order() if r != "format_sql")
    return {"rules": rules, "lift_subqueries": ("lift_subqueries",)}


def queries(dataset: str, split: str) -> list[tuple[str, str]]:
    """``(id, Postgres SQL)`` for one LLM-R2 file, ids like ``llm-r2/tpch/test/17``."""

    path = corpora.BENCH_DIR / "llm-r2" / "data" / "data_llmr2" / "queries" / f"queries_{dataset}_{split}.csv"
    csv.field_size_limit(1 << 30)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    header = rows[0]
    column = header.index("original_sql")
    return [(f"llm-r2/{dataset}/{split}/{row[0]}", row[column]) for row in rows[1:] if row and row[column].strip()]


def normalised(sql: str) -> str | None:
    try:
        return sqlglot.parse_one(sql, read="postgres").sql(dialect="postgres", normalize=True, pretty=False)
    except Exception:  # noqa: BLE001
        return None


def _rows_or_hash(con, sql: str):
    """All rows, or an order-independent fingerprint ``(count, hash sum)`` when there are too many."""

    cursor = con.execute(tb._duck(sql))
    rows = cursor.fetchmany(MAX_ROWS + 1)
    if len(rows) <= MAX_ROWS:
        return tb._bag(rows)
    return con.execute(f"SELECT count(*), sum(hash(q)::HUGEINT) FROM ({tb._duck(sql)}) AS q").fetchone()


def real_check(db: Path, original: str, transformed: str) -> dict:
    """Same rows on real data (bags, or hashes for huge results); timings only when the plan changed."""

    import duckdb

    con = duckdb.connect(str(db), read_only=True, config={"memory_limit": "3GB", "threads": 1})
    try:
        def run(sql):
            return tb._run_duckdb_value(con, lambda: _rows_or_hash(con, sql))

        try:
            base = run(original)
        except Exception as exc:  # noqa: BLE001
            return {"status": "original_fails", "detail": str(exc)[:120]}
        try:
            new = run(transformed)
        except Exception as exc:  # noqa: BLE001
            return {"status": "timeout" if "INTERRUPT" in str(exc).upper() else "error", "detail": str(exc)[:120]}
        same = base == new
        if not same and tb._shares_root_limit(original, transformed):
            try:
                same = run(tb._strip_root_limit(original)) == run(tb._strip_root_limit(transformed))
            except Exception:  # noqa: BLE001
                pass
        record = {"status": "same" if same else "different", "plan_changed": tb._plan(con, original) != tb._plan(con, transformed)}
        if same and record["plan_changed"]:
            times = {"original": [], "transformed": []}
            for _ in range(tb.RUNS):
                for key, sql in (("original", original), ("transformed", transformed)):
                    started = time.perf_counter()
                    tb._run_duckdb_value(con, lambda: con.execute(tb._duck(sql)).fetchall())
                    times[key].append(time.perf_counter() - started)
            record["original_s"] = statistics.median(times["original"])
            record["transformed_s"] = statistics.median(times["transformed"])
        return record
    finally:
        con.close()


def _alarm(signum, frame):  # noqa: ARG001
    raise TimeoutError("query took too long")


def run_query(item: tuple[str, str, str]) -> tuple[str, dict]:
    query_id, dataset, text = item
    workload = DATASETS[dataset]
    out: dict = {}
    try:
        sql = corpora.to_bigquery(text)
    except Exception as exc:  # noqa: BLE001
        return query_id, {"convert": str(exc)[:160]}
    schema = corpora.schema(workload)
    db = tb.real_database(workload)
    signal.signal(signal.SIGALRM, _alarm)
    for name, rules in transformations().items():
        started = time.perf_counter()
        signal.alarm(QUERY_TIMEOUT_S)
        try:
            result = rewrite.apply_rules(rules, sql)
            record = {
                "seconds": round(time.perf_counter() - started, 4),
                "changed": result.sql != sql,
                "verification": result.verification.status.value,
            }
            if record["changed"]:
                record["real"] = real_check(db, sql, result.sql) if db is not None else {"status": "no_data"}
                if record["real"]["status"] not in ("same", "different"):
                    record["synthetic"] = tb.synthetic_check(sql, result.sql, schema)
        except TimeoutError:
            record = {"status": "timeout", "seconds": round(time.perf_counter() - started, 4)}
        except Exception as exc:  # noqa: BLE001
            record = {"status": "crash", "detail": str(exc)[:160], "seconds": round(time.perf_counter() - started, 4)}
        finally:
            signal.alarm(0)
        out[name] = record
    return query_id, out


def run(datasets: list[str], split: str, limit: int = 0, jobs: int | None = None, log: Path | None = None) -> dict[str, dict]:
    """Run every query of the split; with ``log``, append each result as a JSON line and skip ones already there."""

    done: dict[str, dict] = {}
    if log is not None and log.exists():
        for line in log.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                done[record["id"]] = record["result"]
    items = [(qid, d, text) for d in datasets for qid, text in (queries(d, split)[:limit] if limit else queries(d, split))]
    results = {qid: done[qid] for qid, _, _ in items if qid in done}
    work = [item for item in items if item[0] not in done]
    sink = log.open("a") if log is not None else None
    try:
        with Pool(jobs or os.cpu_count()) as pool:
            for query_id, result in pool.imap_unordered(run_query, work, chunksize=1):
                results[query_id] = result
                if sink is not None:
                    sink.write(json.dumps({"id": query_id, "result": result}) + "\n")
                    sink.flush()
    finally:
        if sink is not None:
            sink.close()
    return results


def outcome(record: dict) -> str:
    """One outcome per transformation of a query.

    ``wrong`` (proven and different), ``caught`` (unproven and different), ``same`` (changed and same
    rows on real data, or on generated tables when the real run is impossible), ``unchanged``,
    ``unverified`` (changed, but no run could compare it), ``timeout`` or ``crash``.
    """

    if record.get("status") in ("timeout", "crash"):
        return record["status"]
    if not record.get("changed"):
        return "unchanged"
    real = record.get("real", {}).get("status")
    proven = record.get("verification") == "proven"
    if real == "different" or record.get("synthetic") == "different":
        return "wrong" if proven else "caught"
    if real == "same" or record.get("synthetic") == "same":
        return "same"
    return "unverified"


def summarise(results: dict[str, dict]) -> dict:
    by_dataset: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    seconds: dict[str, list[float]] = defaultdict(list)
    for query_id, result in results.items():
        dataset = query_id.split("/")[1]
        if "convert" in result:
            by_dataset[dataset]["queries"]["convert"] += 1
            continue
        by_dataset[dataset]["queries"]["converted"] += 1
        for name, record in result.items():
            c = by_dataset[dataset][name]
            c[outcome(record)] += 1
            seconds[name].append(record.get("seconds") or 0)
            if record.get("changed"):
                c["changed"] += 1
                c["proven" if record["verification"] == "proven" else "unproven"] += 1
                real = record.get("real") or {}
                if real.get("plan_changed"):
                    c["plan_changed"] += 1
                    if record["verification"] == "proven" and real.get("transformed_s") is not None and real["transformed_s"] <= tb.SPEEDUP * real["original_s"]:
                        c["useful"] += 1
    return {
        "datasets": {d: {k: dict(v) for k, v in groups.items()} for d, groups in sorted(by_dataset.items())},
        "transform_ms": {k: round(1000 * statistics.median(v), 1) for k, v in seconds.items() if v},
        "transform_ms_max": {k: round(1000 * max(v), 1) for k, v in seconds.items() if v},
    }


def overlap() -> dict:
    """Exact duplicates (after normalising) within and across the splits, and with the transformation workloads."""

    out: dict = {}
    for dataset in DATASETS:
        sets = {split: {n for _, q in queries(dataset, split) if (n := normalised(q))} for split in SPLITS}
        sizes = {split: len(queries(dataset, split)) for split in SPLITS}
        official = {n for _, q in tb.workload_queries(DATASETS[dataset]) if (n := normalised(sqlglot.transpile(q, read="bigquery", write="postgres")[0]))}
        out[dataset] = {
            "test": sizes["test"],
            "train": sizes["train"],
            "distinct_test": len(sets["test"]),
            "distinct_train": len(sets["train"]),
            "test_also_in_train": len(sets["test"] & sets["train"]),
            "test_in_transformation_workloads": len(sets["test"] & official),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("KUMOSQL_TIMING", "0")  # worker processes inherit it
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("datasets", nargs="*", help=f"any of {', '.join(DATASETS)} (default: all)")
    parser.add_argument("--split", choices=SPLITS, default="train", help="test is held out for the final score")
    parser.add_argument("--limit", type=int, default=0, help="first N queries per dataset (0 for all)")
    parser.add_argument("--jobs", type=int, default=None)
    parser.add_argument("--log", type=Path, help="resumable JSON-lines log of per-query results")
    parser.add_argument("--json", type=Path, help="write the summary (and overlap inventory) here")
    parser.add_argument("--overlap", action="store_true", help="print the overlap inventory and exit")
    args = parser.parse_args(argv)
    unknown = set(args.datasets) - set(DATASETS)
    if unknown:
        parser.error(f"unknown datasets: {', '.join(sorted(unknown))}")
    if args.overlap:
        print(json.dumps(overlap(), indent=1))
        return 0
    results = run(args.datasets or list(DATASETS), args.split, args.limit, args.jobs, args.log)
    summary = summarise(results)
    summary["split"] = args.split
    summary["source"] = corpora.source_versions().get("llm-r2")
    print(json.dumps(summary, indent=1))
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "queries": results}, indent=1))
    wrong = sum(c.get("wrong", 0) for groups in summary["datasets"].values() for c in groups.values())
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
