"""Measure and score the STATS-CEB materialization runtime proxy.

The source corpus is downloaded separately and never stored in this repository. The
command keeps resumable query/view measurements and aggregate scores under
KUMOSQL_BENCH_DATA; a diagnostic sample is never recorded as a full-suite score.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

SOURCE_COMMIT = "670cb8d4bf4cbfa32f94fdf17f33973d3fd67d1b"
SAMPLE_ROWS = 10_000


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def source_revision(repo: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, stderr=subprocess.STDOUT
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"cannot read the STATS-CEB source revision from {repo}: {error}") from error


def _dev_pairs(proofs: dict[str, dict]) -> dict[str, dict]:
    return {key: value for key, value in proofs.items() if value.get("sql") is not None}


def sampled_workload(work, sample: int | None):
    """Choose a stable query-name sample without consulting labels or result measurements."""

    if sample is None or len(work.queries) <= sample:
        return work
    names = sorted(work.queries, key=lambda name: hashlib.sha256(("sample:" + name).encode()).digest())[:sample]
    from kumosql.joinorder.bench.materialize import Workload

    return Workload({name: work.queries[name] for name in sorted(names)}, work.schema,
                    {name: work.cards[name] for name in names if name in work.cards})


def _measure_baseline(con, work, query: str, *, timeout: float, reps: int, allow_held_out: bool) -> dict:
    from kumosql.joinorder.bench.materialize import measure_baselines

    try:
        return measure_baselines(con, work, timeout=timeout, reps=reps, names=[query], known={},
                                 allow_held_out=allow_held_out)
    except Exception as error:  # a single unsupported or resource-limited query does not end the run
        return {query: {"time": None, "result": None, "plan": None, "plan_estimate": None,
                        "error": f"{type(error).__name__}: {error}"[:200]}}


def _measure_view(con, view, readers, baselines, work, *, reps: int, previous: dict | None,
                  allow_held_out: bool) -> dict:
    from kumosql.joinorder.bench.materialize import measure_view

    try:
        return measure_view(con, view, readers, baselines, work, reps=reps, previous=previous,
                            allow_held_out=allow_held_out)
    except Exception as error:  # preserve the rest of the corpus when one stored view fails
        record = previous or {"id": view.id, "build": None, "rows": None, "readers": {}}
        detail = f"{type(error).__name__}: {error}"[:200]
        for query in readers:
            record["readers"].setdefault(query, {"time": None, "censored": False, "same": None,
                                                 "wrong": False, "result": None, "error": detail})
        return record


def run(repo: Path, *, workers: int = 4, timeout: float = 60.0, reps: int = 3,
        proof_timeout_ms: int = 10_000, resamples: int = 400, sample: int | None = None,
        threads: int = 1, memory_limit: str = "1GB") -> dict:
    """Resume the full query/view measurement, then score only the frozen held-out queries."""

    import duckdb

    from kumosql.joinorder.bench.common import data_dir
    from kumosql.joinorder.bench.materialize import (
        Measurements,
        View,
        cache_dir,
        load_workload,
        mine_views,
        prove_readers,
    )
    from kumosql.joinorder.bench.materialize_model import KumoSQLWork, evaluate_pairs, make_pairs
    from kumosql.joinorder.bench.stats_ceb import TABLES, build_database, join_pairs
    from kumosql.joinorder.stats import Statistics, collect_statistics

    repo = repo.resolve()
    revision = source_revision(repo)
    if revision != SOURCE_COMMIT:
        raise SystemExit(f"expected STATS-CEB source {SOURCE_COMMIT}, got {revision}")

    work = sampled_workload(load_workload(str(repo)), sample)
    root = Path(data_dir())
    cache = Path(cache_dir())
    db_path = root / f"stats-{SOURCE_COMMIT[:12]}.duckdb"
    manifest_path = cache / "manifest.json"
    manifest = {
        "source_commit": SOURCE_COMMIT,
        "queries": len(work.queries),
        "development_queries": len(work.dev),
        "held_out_queries": len(work.held_out),
        "row_cap": 30_000_000,
        "max_view_tables": 4,
        "min_view_support": 2,
        "statistics_sample_rows": SAMPLE_ROWS,
        "measurement_timeout_seconds": timeout,
        "measurement_repetitions": reps,
        "duckdb_threads": threads,
        "duckdb_memory_limit": memory_limit,
        "sample_queries": sample,
        "proof_timeout_ms": proof_timeout_ms,
    }
    previous_manifest = read_json(manifest_path, None)
    if previous_manifest is not None and previous_manifest != manifest:
        raise SystemExit(f"benchmark cache settings differ from {manifest_path}; use a fresh KUMOSQL_BENCH_DATA directory")
    atomic_json(manifest_path, manifest)

    views_path = cache / "views.json"
    if views_path.exists():
        views = [View.from_json(item) for item in read_json(views_path, [])]
    else:
        print("building the STATS-CEB database and mining development views", flush=True)
        build_database(str(repo), str(db_path))
        con = duckdb.connect(str(db_path))
        try:
            views = mine_views(work, con, row_cap=manifest["row_cap"], max_tables=manifest["max_view_tables"],
                               min_support=manifest["min_view_support"])
        finally:
            con.close()
        atomic_json(views_path, [view.to_json() for view in views])
    print(f"frozen {len(views)} views from {len(work.dev)} development queries; {len(work.held_out)} held-out queries", flush=True)

    build_database(str(repo), str(db_path))
    con = duckdb.connect(str(db_path))
    con.execute(f"SET threads={threads}")
    con.execute(f"SET memory_limit='{memory_limit}'")
    try:
        proofs = read_json(cache / "proofs.json", {"development": {}, "held_out": {}})
        if not proofs["development"]:
            print("proving development readers", flush=True)
            proofs["development"] = prove_readers(work, views, timeout_ms=proof_timeout_ms, workers=workers,
                                                   queries=work.dev, allow_held_out=False)
            atomic_json(cache / "proofs.json", proofs)
        if not proofs["held_out"]:
            print("proving held-out readers against the frozen view list", flush=True)
            proofs["held_out"] = prove_readers(work, views, timeout_ms=proof_timeout_ms, workers=workers,
                                                queries=work.held_out, allow_held_out=True)
            atomic_json(cache / "proofs.json", proofs)

        baselines = read_json(cache / "baselines.json", {"development": {}, "held_out": {}})
        if set(baselines["development"]) != set(work.dev):
            missing = sorted(set(work.dev).difference(baselines["development"]))
            print(f"measuring {len(missing)} development baselines", flush=True)
            for index, query in enumerate(missing, 1):
                baselines["development"].update(
                    _measure_baseline(con, work, query, timeout=timeout, reps=reps, allow_held_out=False)
                )
                atomic_json(cache / "baselines.json", baselines)
                if index % 5 == 0 or index == len(missing):
                    print(f"development baselines {index}/{len(missing)}", flush=True)
        if set(baselines["held_out"]) != set(work.held_out):
            missing = sorted(set(work.held_out).difference(baselines["held_out"]))
            print(f"measuring {len(missing)} held-out baselines after freezing development choices", flush=True)
            for index, query in enumerate(missing, 1):
                baselines["held_out"].update(
                    _measure_baseline(con, work, query, timeout=timeout, reps=reps, allow_held_out=True)
                )
                atomic_json(cache / "baselines.json", baselines)
                if index % 5 == 0 or index == len(missing):
                    print(f"held-out baselines {index}/{len(missing)}", flush=True)

        dev_proven = _dev_pairs(proofs["development"])
        held_proven = _dev_pairs(proofs["held_out"])
        all_baselines = {**baselines["development"], **baselines["held_out"]}
        for index, view in enumerate(views, 1):
            record_path = cache / f"view-{view.id}.json"
            record = read_json(record_path, None)
            dev_readers = {
                key.split("|", 1)[1]: value["sql"]
                for key, value in dev_proven.items()
                if key.startswith(view.id + "|")
            }
            held_readers = {
                key.split("|", 1)[1]: value["sql"]
                for key, value in held_proven.items()
                if key.startswith(view.id + "|")
            }
            if record is None or any(q not in record.get("readers", {}) for q in dev_readers):
                record = _measure_view(con, view, dev_readers, baselines["development"], work,
                                       reps=reps, previous=record, allow_held_out=False)
                atomic_json(record_path, record)
            if any(q not in record.get("readers", {}) for q in held_readers):
                record = _measure_view(con, view, held_readers, all_baselines, work,
                                       reps=reps, previous=record, allow_held_out=True)
                atomic_json(record_path, record)
            if index % 10 == 0 or index == len(views):
                print(f"measured views {index}/{len(views)}", flush=True)

        records = {view.id: read_json(cache / f"view-{view.id}.json", {"readers": {}}) for view in views}
        measurements = Measurements(work, views, proofs, all_baselines, records)

        stats_path = cache / f"joinorder-stats-{SAMPLE_ROWS}.json.gz"
        if not stats_path.exists():
            print("collecting plan-model statistics from development join keys", flush=True)
            join_queries = [work.queries[q] for q in work.dev]
            stats = collect_statistics(con, TABLES, join_pairs(join_queries), sample_rows=SAMPLE_ROWS)
            stats.save(str(stats_path))
        stats = Statistics.load(str(stats_path))

        dev_work = KumoSQLWork(measurements, stats, queries=sorted(work.dev), allow_held_out=False)
        held_work = KumoSQLWork(measurements, stats, queries=sorted(work.held_out), allow_held_out=True)
        dev_pairs = make_pairs(measurements.pairs(held_out=False), dev_work)
        held_pairs = make_pairs(measurements.pairs(held_out=True), held_work)
        print(f"measured proven reader pairs: development={len(dev_pairs)} held_out={len(held_pairs)}", flush=True)
        if not dev_pairs or not held_pairs:
            raise SystemExit("not enough proven measured pairs on both sides of the fixed query split")
        scores = evaluate_pairs(
            held_pairs,
            held_work,
            {query: value["time"] for query, value in all_baselines.items() if value.get("time") is not None},
            training_pairs=dev_pairs,
            training_kw=dev_work,
            resamples=resamples,
        )
    finally:
        con.close()

    output = {
        "suite": "STATS-CEB materialization runtime prediction",
        "source_commit": SOURCE_COMMIT,
        "query_split": {"development": len(work.dev), "held_out": len(work.held_out)},
        "view_count": len(views),
        "development_pairs": len(dev_pairs),
        "held_out_pairs": len(held_pairs),
        "held_out_wrong_pairs": sum(bool(pair.get("wrong")) for pair in measurements.pairs(held_out=True)),
        "scores": scores,
        "measurement": {
            "timeout_seconds": timeout,
            "repetitions": reps,
            "resamples": resamples,
            "censored_development_readers": sum(bool(pair.get("censored")) for pair in measurements.pairs(held_out=False)),
            "censored_held_out_readers": sum(bool(pair.get("censored")) for pair in measurements.pairs(held_out=True)),
        },
    }
    result_path = cache / "scores.json"
    atomic_json(result_path, output)
    print(f"wrote held-out runtime scores to {result_path}", flush=True)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path, help="local checkout of the pinned STATS-CEB source")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=60.0, help="per baseline-query timeout in seconds")
    parser.add_argument("--reps", type=int, default=3, help="median runtime repetitions")
    parser.add_argument("--proof-timeout-ms", type=int, default=10_000)
    parser.add_argument("--resamples", type=int, default=400, help="group-clustered bootstrap resamples")
    parser.add_argument("--sample", type=int, help="stable SHA-256 subset of queries, for a diagnostic run")
    parser.add_argument("--threads", type=int, default=1, help="DuckDB worker threads per measured query")
    parser.add_argument("--memory-limit", default="1GB", help="DuckDB memory limit, e.g. 1GB or 768MB")
    args = parser.parse_args(argv)
    if (args.workers < 1 or args.timeout <= 0 or args.reps < 1 or args.proof_timeout_ms < 1 or
            args.resamples < 0 or args.threads < 1 or (args.sample is not None and args.sample < 1) or
            not re.fullmatch(r"[1-9][0-9]*(?:MB|GB)", args.memory_limit.upper())):
        parser.error("numeric settings must be positive (resamples may be zero) and memory-limit must be MB or GB")
    run(args.repo, workers=args.workers, timeout=args.timeout, reps=args.reps,
        proof_timeout_ms=args.proof_timeout_ms, resamples=args.resamples, sample=args.sample,
        threads=args.threads, memory_limit=args.memory_limit.upper())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
