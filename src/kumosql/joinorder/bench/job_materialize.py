"""Measure proof-gated materialized joins on JOB, with a family holdout.

Candidates, sampled views, cost fitting and view selection use only JOB families
1--16. Families 17--33 are touched after selection is frozen and are reported as
holdout results. The harness is deliberately bounded: only the highest-ranked
development candidates under ``row_cap`` are materialized.
"""

from __future__ import annotations

from collections import Counter
import json
import math
import os
import subprocess
import time
from typing import Iterable, Mapping, Sequence

from .common import data_dir
from .job import TUNING_FAMILIES, build_database, family, query_files

FEATURES = ("scan", "join", "write", "query", "rows", "build")


def split_families(files: Sequence[tuple[str, str]]) -> tuple[dict[str, str], dict[str, str]]:
    """Return the JOB development and held-out queries without mixing families."""

    development = {name: sql for name, sql in files if family(name) in TUNING_FAMILIES}
    held_out = {name: sql for name, sql in files if family(name) not in TUNING_FAMILIES}
    if not development or not held_out:
        raise ValueError("JOB workload must contain both tuning families 1--16 and held-out families 17--33")
    return development, held_out


def _same_bag(left: Iterable[tuple], right: Iterable[tuple]) -> bool:
    """Compare result bags; JOB queries may return rows without an ORDER BY."""

    return Counter(left) == Counter(right)


def _duckdb_sql(sql: str) -> str:
    import sqlglot

    return sqlglot.transpile(sql.strip().rstrip(";"), read="postgres", write="duckdb", identify=True)[0]


def _timed(con, sql: str, repeats: int, timeout: float) -> tuple[float, list[tuple] | None]:
    from .endtoend import run_sql

    samples: list[float] = []
    first: list[tuple] | None = None
    for _ in range(repeats):
        elapsed, rows = run_sql(con, sql, timeout)
        if elapsed == float("inf") or rows is None:
            return float("inf"), None
        if first is None:
            first = rows
        samples.append(elapsed)
    return min(samples), first


def _checked_pair(con, original: str, rewritten: str, baseline_rows: list[tuple], rewritten_rows: list[tuple]) -> str:
    """Classify an executed proof recheck, discounting optimizer-only disagreements."""

    from kumosql.duckdb_load import run_unoptimized

    if _same_bag(baseline_rows, rewritten_rows):
        return "same"
    before, after = run_unoptimized(con, original, rewritten)
    if _same_bag(before, after):
        return "optimizer_disagreement"
    return "wrong"


def _table_schema(con) -> dict[str, list[str]]:
    schema: dict[str, list[str]] = {}
    for (table,) in con.execute("SHOW TABLES").fetchall():
        schema[table] = [row[0] for row in con.execute(f'DESCRIBE "{table}"').fetchall()]
    return schema


def _fallback_query_features(query, rows: Mapping[str, float]) -> dict[str, float]:
    """Scan-only feature for an unseen held-out join key; never refit on its join."""

    from kumosql.joinorder.view_cost import columns_used

    scan = sum(rows[table] * max(len(columns_used(query, alias)), 1)
               for alias, table in query.tables.items())
    return {"scan": float(scan), "join": 0.0, "query": 1.0}


def _fallback_view_features(query, group: frozenset[str], rows: Mapping[str, float], view_rows: float) -> dict[str, float]:
    """Scan-only feature when a held-out join needs a key absent from dev statistics."""

    from kumosql.joinorder.view_cost import columns_used

    def width(alias: str) -> int:
        return max(len(columns_used(query, alias)), 1)

    scan = sum(rows[table] * width(alias) for alias, table in query.tables.items() if alias not in group)
    scan += view_rows * sum(width(alias) for alias in group)
    return {"scan": float(scan), "join": 0.0, "query": 1.0}


def run(
    repo: str,
    *,
    db: str | None = None,
    max_views: int = 10,
    max_candidates: int = 40,
    row_cap: int = 30_000_000,
    sample_rows: int = 10_000,
    query_runs_per_day: float = 1.0,
    proof_timeout_ms: int = 1_000,
    execution_timeout: float = 60.0,
    repeats: int = 2,
    log=print,
) -> dict:
    """Run a bounded JOB materialization backtest and preserve its tuning split."""

    import duckdb
    from kumosql import model_reuse, view_candidates
    from kumosql.backtest import score as backtest_score
    from kumosql.cost_model import error_summary, fit
    from kumosql.materialization import Candidate, Evidence, Option, Problem, Template, select
    from kumosql.joinorder.estimator import FactorEstimator
    from kumosql.joinorder.query import parse_join_query
    from kumosql.joinorder.stats import Statistics, collect_statistics
    from kumosql.joinorder.view_cost import build_features, query_features, query_over_view_features
    from .job import query_files

    if (max_views < 1 or max_candidates < max_views or row_cap < 1 or repeats < 1
            or not math.isfinite(query_runs_per_day) or query_runs_per_day <= 0):
        raise ValueError("view/candidate limits, row_cap, repeats and daily query frequency must be positive")
    base = data_dir()
    db = db or os.path.join(base, "job.duckdb")
    build_database(repo, db)
    con = duckdb.connect(db)
    con.execute("SET threads=4")
    temp_dir = os.path.join(base, "duckdb-tmp")
    os.makedirs(temp_dir, exist_ok=True)
    escaped_temp_dir = temp_dir.replace("\\", "/").replace("'", "''")
    con.execute(f"SET temp_directory='{escaped_temp_dir}'")
    con.execute("SET memory_limit='8GB'")
    files = query_files(repo)
    dev, held_out = split_families(files)
    dev_queries = {name: parse_join_query(sql) for name, sql in dev.items()}
    schema = _table_schema(con)

    stats_path = os.path.join(base, f"job_materialize_stats_{sample_rows}_dev.json.gz")
    if not os.path.exists(stats_path):
        pairs = sorted({((q.tables[e.left], e.left_col), (q.tables[e.right], e.right_col))
                        for q in dev_queries.values() for e in q.edges})
        # The table samples are data features, independent of the held-out SQL. Join pairs
        # remain limited to development queries so held-out workload structure is not fitted.
        tables = sorted(schema)
        collect_statistics(con, tables, pairs, sample_rows=sample_rows).save(stats_path)
    stats = Statistics.load(stats_path)
    estimator = FactorEstimator(stats)
    rows = {name: float(table.rows) for name, table in stats.tables.items()}

    candidates = view_candidates.mine(dev, schema, min_support=2, max_tables=4, dialect="postgres")
    log(f"JOB dev queries={len(dev)} heldout={len(held_out)} candidates={len(candidates)}")
    training: list[tuple[dict, float]] = []
    baseline_dev: dict[str, tuple[float, list[tuple], str, dict]] = {}
    from .endtoend import DEFAULT_SETTINGS, run_sql
    con.execute(DEFAULT_SETTINGS)
    for name, sql in dev.items():
        duck_sql = _duckdb_sql(sql)
        seconds, result = _timed(con, duck_sql, repeats, execution_timeout)
        if result is not None and seconds != float("inf"):
            work = query_features(dev_queries[name], estimator, rows)
            baseline_dev[name] = (seconds, result, duck_sql, work)
            training.append((work, seconds))
    if len(baseline_dev) != len(dev):
        log(f"baseline timeouts: {len(dev) - len(baseline_dev)} development queries")

    measured_views: list[dict] = []
    screened = proof_count = execution_pairs = optimizer_disagreements = 0
    for candidate in candidates[:max_candidates]:
        if len(measured_views) >= max_views:
            break
        screened += 1
        candidate_sql = candidate.sql()
        capped_count = int(con.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM ({candidate_sql}) AS capped LIMIT {row_cap + 1}) AS limited"
        ).fetchone()[0])
        if capped_count > row_cap:
            continue
        # Run the proof gate before spending time building a stored table.
        proof_results = {}
        for name in sorted(candidate.queries):
            result = model_reuse.rewrite_over_model(
                dev[name], candidate_sql, schema=schema, model_name="mv_job_candidate",
                dialect="postgres", timeout_ms=proof_timeout_ms,
            )
            if result.rewritten and result.sql:
                proof_results[name] = result.sql
        proof_count += len(proof_results)
        if not proof_results:
            continue

        view_query = parse_join_query(candidate_sql)
        build_work = build_features(view_query, estimator, rows)
        build_work["query"] = 0.0
        build_work["build"] = 1.0
        con.execute(DEFAULT_SETTINGS)
        # Candidate proof SQL is generated against this reusable one-at-a-time table name.
        table_name = "mv_job_candidate"
        build_start = time.perf_counter()
        con.execute(f'CREATE OR REPLACE TEMP TABLE "{table_name}" AS {candidate_sql}')
        build_seconds = time.perf_counter() - build_start
        actual_rows = int(con.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0])
        if actual_rows > row_cap or actual_rows != capped_count:
            con.execute(f'DROP TABLE IF EXISTS "{table_name}"')
            raise AssertionError(f"row-cap check disagreed for {table_name}: {capped_count} vs {actual_rows}")

        pairs_for_view = []
        for name, rewrite_sql in proof_results.items():
            baseline = baseline_dev.get(name)
            if baseline is None:
                continue
            original_sql = baseline[2]
            rewritten_sql = _duckdb_sql(rewrite_sql)
            read_seconds, read_rows = _timed(con, rewritten_sql, repeats, execution_timeout)
            if read_rows is None or read_seconds == float("inf"):
                continue
            correctness = _checked_pair(con, original_sql, rewritten_sql, baseline[1], read_rows)
            if correctness == "wrong":
                raise AssertionError(f"prover/runtime disagreement on JOB {name} over {table_name}")
            if correctness == "optimizer_disagreement":
                optimizer_disagreements += 1
                continue
            graph = view_candidates.graph_of(dev[name], schema, "postgres")
            aliases = view_candidates.shapes_of(graph).get(candidate.shape) if graph else None
            if not aliases:
                continue
            read_work = query_over_view_features(
                dev_queries[name], frozenset(aliases), estimator, rows, float(actual_rows),
            )
            execution_pairs += 1
            training.append((read_work, read_seconds))
            pairs_for_view.append({"query": name, "features": read_work, "measured_seconds": read_seconds})
        con.execute(f'DROP TABLE IF EXISTS "{table_name}"')
        if not pairs_for_view:
            continue
        training.append((build_work, build_seconds))
        measured_views.append({
            "id": f"job_view_{len(measured_views):02d}",
            "sql": candidate_sql,
            "shape": {"tables": list(candidate.shape.tables), "edges": [list(e) for e in candidate.shape.edges]},
            "support_queries": sorted(candidate.queries),
            "rows": actual_rows,
            "build_features": build_work,
            "build_seconds": build_seconds,
            "readers": pairs_for_view,
        })
        log(f"measured view {len(measured_views)}/{max_views}: support={len(candidate.queries)} rows={actual_rows} pairs={len(pairs_for_view)} build={build_seconds:.3f}s")

    if not measured_views:
        con.close()
        raise RuntimeError("no proof-valid, size-capped JOB candidate could be measured")
    model = fit(training, FEATURES, unit="seconds")
    training_errors = error_summary([model.predict(features) for features, _ in training],
                                    [seconds for _, seconds in training])
    reader_predicted_savings: list[float] = []
    reader_measured_savings: list[float] = []
    measured_view_summary = []
    for view in measured_views:
        raw_predicted = raw_measured = 0.0
        faster = 0
        for pair in view["readers"]:
            base_seconds, _result, _sql, base_features = baseline_dev[pair["query"]]
            predicted = query_runs_per_day * (model.predict(base_features) - model.predict(pair["features"]))
            measured = query_runs_per_day * (base_seconds - pair["measured_seconds"])
            reader_predicted_savings.append(predicted)
            reader_measured_savings.append(measured)
            raw_predicted += predicted
            raw_measured += measured
            faster += pair["measured_seconds"] < base_seconds
        measured_view_summary.append({
            "id": view["id"], "rows": view["rows"], "development_readers": len(view["readers"]),
            "readers_faster_than_base": faster,
            "build_seconds_measured": view["build_seconds"],
            "build_seconds_predicted": model.predict(view["build_features"]),
            "reader_savings_predicted_seconds_per_day_excluding_build": raw_predicted,
            "reader_savings_measured_seconds_per_day_excluding_build": raw_measured,
        })
    reader_savings_backtest = backtest_score(
        reader_predicted_savings, reader_measured_savings,
        k=min(10, len(reader_predicted_savings)), resamples=1000, seed=11,
    ) if reader_predicted_savings else {}

    templates = []
    for name, (_actual, _result, _sql, features) in baseline_dev.items():
        options = [Option(max(model.predict(features), 1e-9), label="base")]
        for view in measured_views:
            pair = next((p for p in view["readers"] if p["query"] == name), None)
            if pair:
                options.append(Option(max(model.predict(pair["features"]), 1e-9),
                                      requires=frozenset({view["id"]}), label=view["id"]))
        templates.append(Template(name, query_runs_per_day, tuple(options)))
    advisor_candidates = [Candidate(
        id=view["id"], title=f"Store {len(view['shape']['tables'])}-table JOB join",
        kind="store_view", node=view["id"], refresh_per_day=1.0,
        refresh_cost=max(model.predict(view["build_features"]), 1e-9), storage_bytes=None,
        evidence=Evidence("proven", "The view readers were algebraically proved and rechecked in DuckDB."),
        detail={"rows": view["rows"], "support": len(view["support_queries"])},
    ) for view in measured_views]
    problem = Problem(templates, advisor_candidates, "seconds")
    selection = select(problem, exact_limit=min(12, max_views))
    selected = [view for view in measured_views if view["id"] in set(selection.chosen)]

    def predicted_reader(base_work: dict, alts: dict[str, dict], allowed_ids: set[str]) -> str | None:
        best_id, best_cost = None, model.predict(base_work)
        for cid, features in alts.items():
            if cid not in allowed_ids:
                continue
            cost = model.predict(features)
            if cost < best_cost:
                best_id, best_cost = cid, cost
        return best_id

    dev_assignments = {name: predicted_reader(
        item[3], {view["id"]: p["features"] for view in measured_views
                  for p in view["readers"] if p["query"] == name}, set(selection.chosen),
    ) for name, item in baseline_dev.items()}
    dev_actual = query_runs_per_day * sum(item[0] if dev_assignments[name] is None else next(
        p["measured_seconds"] for v in measured_views if v["id"] == dev_assignments[name]
        for p in v["readers"] if p["query"] == name
    ) for name, item in baseline_dev.items()) + sum(v["build_seconds"] for v in selected)
    dev_baseline = query_runs_per_day * sum(item[0] for item in baseline_dev.values())

    # Selection is now frozen. Held-out query SQL is used only for proof/execution evaluation.
    held_queries = {name: parse_join_query(sql) for name, sql in held_out.items()}
    held_stats: dict[str, tuple[float, list[tuple], str, dict]] = {}
    held_feature_fallbacks = {"baseline": 0, "reader": 0}
    con.execute(DEFAULT_SETTINGS)
    for name, sql in held_out.items():
        duck_sql = _duckdb_sql(sql)
        seconds, result = _timed(con, duck_sql, repeats, execution_timeout)
        if result is not None and seconds != float("inf"):
            try:
                work = query_features(held_queries[name], estimator, rows)
            except KeyError:
                held_feature_fallbacks["baseline"] += 1
                work = _fallback_query_features(held_queries[name], rows)
            held_stats[name] = (seconds, result, duck_sql, work)
    log(f"held-out baseline queries measured={len(held_stats)}/{len(held_out)}; frozen views={len(selected)}")

    held_tables: dict[str, float] = {}
    for view in selected:
        table_name = view["id"]
        start = time.perf_counter()
        con.execute(f'CREATE OR REPLACE TEMP TABLE "{table_name}" AS {view["sql"]}')
        held_tables[table_name] = time.perf_counter() - start

    held_runtime = 0.0
    held_base_runtime = query_runs_per_day * sum(item[0] for item in held_stats.values())
    held_wrong = held_optimizer_disagreements = held_proofs = 0
    held_choice_errors: list[tuple[float, float]] = []
    held_detail = {}
    for name, (baseline_seconds, baseline_rows, original_sql, base_work) in held_stats.items():
        alternatives: dict[str, tuple[float, list[tuple], dict]] = {}
        for view in selected:
            graph = view_candidates.graph_of(held_out[name], schema, "postgres")
            aliases = view_candidates.shapes_of(graph).get(
                view_candidates.Shape(tuple(view["shape"]["tables"]),
                                      tuple(tuple(e) for e in view["shape"]["edges"]))) if graph else None
            if not aliases:
                continue
            result = model_reuse.rewrite_over_model(
                held_out[name], view["sql"], schema=schema, model_name=view["id"],
                dialect="postgres", timeout_ms=proof_timeout_ms,
            )
            if not result.rewritten or not result.sql:
                continue
            held_proofs += 1
            rewritten_sql = _duckdb_sql(result.sql)
            seconds, rows_result = _timed(con, rewritten_sql, repeats, execution_timeout)
            if rows_result is None or seconds == float("inf"):
                continue
            correctness = _checked_pair(con, original_sql, rewritten_sql, baseline_rows, rows_result)
            if correctness == "wrong":
                held_wrong += 1
                continue
            if correctness == "optimizer_disagreement":
                held_optimizer_disagreements += 1
                continue
            query = held_queries[name]
            view_rows = float(con.execute(f'SELECT COUNT(*) FROM "{view["id"]}"').fetchone()[0])
            try:
                read_work = query_over_view_features(query, frozenset(aliases), estimator, rows, view_rows)
            except KeyError:
                held_feature_fallbacks["reader"] += 1
                read_work = _fallback_view_features(query, frozenset(aliases), rows, view_rows)
            alternatives[view["id"]] = (seconds, rows_result, read_work)
        predicted_alts = {cid: item[2] for cid, item in alternatives.items()}
        choice = predicted_reader(base_work, predicted_alts, set(selection.chosen))
        if choice is None:
            actual = baseline_seconds
            predicted_choice = model.predict(base_work)
        else:
            actual = alternatives[choice][0]
            predicted_choice = model.predict(alternatives[choice][2])
        held_choice_errors.append((predicted_choice, actual))
        held_runtime += actual * query_runs_per_day
        held_detail[name] = {"choice": choice or "base", "baseline_seconds": baseline_seconds,
                             "advisor_seconds": actual, "predicted_seconds": predicted_choice}
        if len(held_detail) % 10 == 0 or len(held_detail) == len(held_stats):
            log(f"held-out routes scored={len(held_detail)}/{len(held_stats)}")

    held_runtime += sum(held_tables.values())
    for view in selected:
        con.execute(f'DROP TABLE IF EXISTS "{view["id"]}"')
    con.close()

    out = {
        "dataset": "danolivo/jo-bench",
        "dataset_commit": subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                                          capture_output=True, text=True, check=True).stdout.strip(),
        "duckdb_version": duckdb.__version__,
        "split": {"development_families": "1-16", "development_queries": len(dev),
                  "held_out_families": "17-33", "held_out_queries": len(held_out)},
        "workload_assumption": {"runs_per_query_per_day": query_runs_per_day,
                                 "view_refreshes_per_day": 1.0,
                                 "storage_cost_priced": False},
        "candidate_mining": {"min_support": 2, "max_tables": 4,
                              "mined": len(candidates), "size_screened": screened,
                              "row_cap": row_cap, "max_views": max_views,
                              "measured": len(measured_views), "proofs": proof_count,
                              "development_execution_pairs": execution_pairs,
                              "optimizer_disagreements_excluded": optimizer_disagreements},
        "cost_model": {"features": list(FEATURES), "samples": len(training),
                       "development_error": training_errors, "weights": model.to_json()["weights"],
                       "development_reader_savings_backtest": reader_savings_backtest,
                       "reader_savings_note": ("In-sample development ranking; pair-level bootstrap does not cluster repeated queries; "
                                                "excludes per-view build cost, which the selector adds once.")},
        "measured_views": measured_view_summary,
        "selection": selection.to_json(),
        "selected_views": [{k: v for k, v in view.items() if k not in {"sql", "readers", "build_features"}}
                           for view in selected],
        "development_runtime_seconds": {"queries_measured": len(baseline_dev),
                                         "baseline": dev_baseline, "advisor_with_builds": dev_actual,
                                         "saving": dev_baseline - dev_actual},
        "held_out": {"queries_measured": len(held_stats), "proofs": held_proofs,
                     "wrong": held_wrong, "optimizer_disagreements_excluded": held_optimizer_disagreements,
                     "scan_only_feature_fallbacks": held_feature_fallbacks,
                     "baseline_seconds": held_base_runtime, "advisor_with_builds_seconds": held_runtime,
                     "saving_seconds": held_base_runtime - held_runtime,
                     "selected_view_build_seconds": held_tables,
                     "advisor_cost_error": error_summary([p for p, _ in held_choice_errors],
                                                         [m for _, m in held_choice_errors]),
                     "queries": held_detail},
    }
    result_path = os.path.join(base, "job-materialization-results.json")
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return out
