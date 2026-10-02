"""Join Order Benchmark (Leis et al., VLDB 2015) on the full IMDB data.

Data and the 113 queries come from https://github.com/danolivo/jo-bench
(BSD-2-Clause), which keeps the IMDB CSV files in git. Clone it and pass
``--repo``; the DuckDB database is built from those CSVs.

JOB has no published sub-plan sizes, so exact sizes of every connected sub-join
(about 20,000 in all) are computed here by variable elimination over each
query's pre-filtered relations, cached one file per query.
"""

from __future__ import annotations

import json
import math
import os
import re
import time

from .common import cached_json, data_dir, percentiles, psql_explain_rows, q_error

# Families 1-16 may be looked at while improving the estimator; 17-33 are held out.
TUNING_FAMILIES = range(1, 17)


def query_files(repo: str) -> list[tuple[str, str]]:
    qdir = os.path.join(repo, "queries")
    names = [f[:-4] for f in os.listdir(qdir) if re.fullmatch(r"\d+[a-z]\.sql", f)]
    names.sort(key=lambda n: (int(n[:-1]), n[-1]))
    return [(n, open(os.path.join(qdir, n + ".sql")).read()) for n in names]


def family(name: str) -> int:
    return int(name[:-1])


def build_database(repo: str, path: str) -> None:
    import duckdb

    if os.path.exists(path):
        return
    con = duckdb.connect(path + ".tmp")
    con.execute(f"SET VARIABLE datadir='{repo}'")
    con.execute(open(os.path.join(repo, "duckdb/schema.sql")).read())
    con.execute(open(os.path.join(repo, "duckdb/load.sql")).read())
    con.close()
    os.replace(path + ".tmp", path)


def _geo(values: list[float]) -> float:
    return math.exp(sum(math.log(max(v, 1e-12)) for v in values) / len(values))


def run(repo: str, sample_rows: int = 10_000, psql: list[str] | None = None,
        execute: bool = False, timeout: float = 60.0, db: str | None = None, log=print) -> dict:
    import duckdb
    import sqlglot

    from ..estimator import FactorEstimator
    from ..query import parse_join_query
    from ..stats import Statistics, collect_statistics
    from .endtoend import DEFAULT_SETTINGS, FORCED_PLAN_SETTINGS, connected_subsets, forced_sql, optimize, run_sql, subset_key, subset_sql, true_cost
    from .truth import exact_count, materialize_filtered

    base = data_dir()
    db = db or os.path.join(base, "job.duckdb")
    build_database(repo, db)
    con = duckdb.connect(db, read_only=True)
    con.execute("SET threads=4")
    files = query_files(repo)
    names = [n for n, _ in files]
    queries = [parse_join_query(sql) for _, sql in files]

    stats_path = os.path.join(base, f"job_stats_{sample_rows}.pkl.gz")
    if not os.path.exists(stats_path):
        pairs = sorted({((q.tables[e.left], e.left_col), (q.tables[e.right], e.right_col))
                        for q in queries for e in q.edges})
        tables = sorted({t for q in queries for t in q.tables.values()})
        collect_statistics(con, tables, pairs, sample_rows=sample_rows).save(stats_path)

    truth_dir = os.path.join(base, "job_truth")
    os.makedirs(truth_dir, exist_ok=True)
    truth = []
    for name, q in zip(names, queries):
        def build(q=q):
            pf = materialize_filtered(con, q)
            return {subset_key(s): exact_count(con, q, s, prefiltered=pf) for s in connected_subsets(q)}
        truth.append(cached_json(os.path.join(truth_dir, name + ".json"), build))

    est = FactorEstimator(Statistics.load(stats_path))
    start = time.perf_counter()
    cards = {"true": truth,
             "kumosql": [{subset_key(s): est.estimate(q, s) for s in connected_subsets(q)} for q in queries]}
    n_subsets = sum(len(t) for t in truth)
    est_seconds = time.perf_counter() - start
    if psql:
        def pg():
            out = []
            for q in queries:
                ss = connected_subsets(q)
                out.append(dict(zip(map(subset_key, ss), psql_explain_rows(psql, [subset_sql(q, s) for s in ss]))))
            return out
        cards["postgres"] = cached_json(os.path.join(base, "job_subsets_pg.json"), pg)

    out: dict = {"queries": len(queries), "sub_joins": n_subsets,
                 "stats_mb": round(os.path.getsize(stats_path) / 1e6, 2),
                 "estimate_ms": round(1000 * est_seconds / n_subsets, 2), "q_error": {}}
    held = [i for i, n in enumerate(names) if family(n) not in TUNING_FAMILIES]
    for k in cards:
        if k == "true":
            continue
        for label, idx in (("all", range(len(queries))), ("held_out", held)):
            errs = [q_error(cards[k][i][s], v) for i in idx for s, v in truth[i].items() if "," in s]
            out["q_error"][f"{k}/{label}"] = percentiles(errs)
    log("q-error (sub-joins of 2+ relations)", out["q_error"])

    plans, ratios = [], {k: [] for k in cards}
    for i, q in enumerate(queries):
        row = {k: optimize(q, (lambda m: lambda s: m[subset_key(s)])(cards[k][i])) for k in cards}
        best = true_cost(row["true"], truth[i]) + 1.0
        for k in cards:
            ratios[k].append((true_cost(row[k], truth[i]) + 1.0) / best)
        plans.append(row)
    out["plan_cost_vs_optimal"] = {k: {**percentiles(v), "geomean": _geo(v),
                                       "held_out_geomean": _geo([v[i] for i in held])}
                                   for k, v in ratios.items()}
    log("plan cost / optimal", out["plan_cost_vs_optimal"])

    if execute:
        times: dict[str, list[float]] = {"duckdb": [], **{k: [] for k in cards}}
        mismatches = 0
        for i, q in enumerate(queries):
            # quoted identifiers: JOB uses ``at`` as an alias, a keyword in DuckDB
            original = sqlglot.transpile(files[i][1].strip().rstrip(";"), read="postgres",
                                         write="duckdb", identify=True)[0]
            sqls = {"duckdb": original, **{k: forced_sql(q, plans[i][k]) for k in cards}}
            ref = None
            for k, sql in sqls.items():
                con.execute(DEFAULT_SETTINGS if k == "duckdb" else FORCED_PLAN_SETTINGS)
                t, res = run_sql(con, sql, timeout)
                if t != float("inf"):
                    t = min(t, run_sql(con, sql, timeout)[0])
                if res is not None:
                    if ref is None:
                        ref = res
                    elif res != ref:
                        mismatches += 1
                times[k].append(t)
            log(names[i], {k: round(v[-1], 3) for k, v in times.items()})
        out["runtime"] = {k: {"seconds": round(sum(min(t, timeout) for t in v), 2),
                              "held_out_seconds": round(sum(min(v[i], timeout) for i in held), 2),
                              "timeouts": sum(t == float("inf") for t in v)} for k, v in times.items()}
        out["runtime"]["timeout_seconds"] = timeout
        out["result_mismatches"] = mismatches
        out["per_query_seconds"] = {k: dict(zip(names, v)) for k, v in times.items()}
        log("runtime", out["runtime"])
    return out
