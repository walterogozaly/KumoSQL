"""STATS-CEB benchmark (Han et al., VLDB 2022, "Cardinality Estimation in DBMS: A
Comprehensive Benchmark Evaluation").

Data and workload come from https://github.com/Nathaniel-Han/End-to-End-CardEst-Benchmark
(clone it and pass ``--repo``). The benchmark has 146 COUNT(*) queries over the
simplified STATS (stats.stackexchange.com) dataset and 2,603 sub-plan queries,
the sub-joins a Postgres optimizer estimates while planning them.
"""

from __future__ import annotations

import argparse
import os
import re
from typing import Any

from .common import cached_json, data_dir, percentiles, psql_explain_rows, q_error

TABLES = ["badges", "comments", "posthistory", "postlinks", "posts", "tags", "users", "votes"]


def workload(repo: str) -> list[tuple[int, str]]:
    out = []
    with open(os.path.join(repo, "workloads/stats_CEB/stats_CEB.sql")) as fh:
        for line in fh:
            if line.strip():
                card, sql = line.strip().split("||", 1)
                out.append((int(card), sql))
    return out


def sub_plans(repo: str) -> list[tuple[str, int]]:
    """(sql, index of the parent query) for every sub-plan query."""
    out = []
    with open(os.path.join(repo, "workloads/stats_CEB/sub_plan_queries/stats_CEB_sub_queries.sql")) as fh:
        for line in fh:
            if line.strip():
                sql, qid = line.strip().rsplit("||", 1)
                out.append((sql, int(qid)))
    return out


def published_estimates(repo: str) -> dict[str, list[float]]:
    est_dir = os.path.join(repo, "workloads/stats_CEB/sub_plan_queries/estimates")
    out = {}
    for name in sorted(os.listdir(est_dir)):
        m = re.match(r"stats_CEB_sub_queries_(\w+)\.txt", name)
        if m:
            with open(os.path.join(est_dir, name)) as fh:
                out[m.group(1)] = [float(x) for x in fh.read().split()]
    return out


def build_database(repo: str, path: str) -> None:
    import duckdb

    if os.path.exists(path):
        return
    con = duckdb.connect(path + ".tmp")
    ddl = open(os.path.join(repo, "datasets/stats_simplified/stats.sql")).read()
    ddl = re.sub(r"SERIAL PRIMARY KEY", "INTEGER", ddl)
    con.execute(ddl)
    for t in TABLES:
        src = {"posthistory": "postHistory", "postlinks": "postLinks"}.get(t, t)
        csv = os.path.join(repo, f"datasets/stats_simplified/{src}.csv")
        con.execute(f"COPY {t} FROM '{csv}' (HEADER, NULLSTR '')")
    con.close()
    os.replace(path + ".tmp", path)


def true_cards(con: Any, sqls: list[str], progress_path: str | None = None) -> list[int]:
    """Exact COUNT(*) of each query, resumable through a progress file."""
    import json

    done: dict[int, int] = {}
    if progress_path and os.path.exists(progress_path):
        with open(progress_path) as fh:
            for line in fh:
                i, n = json.loads(line)
                done[i] = n
    fh = open(progress_path, "a") if progress_path else None
    try:
        for i, sql in enumerate(sqls):
            if i not in done:
                done[i] = int(con.execute(sql.rstrip(";")).fetchone()[0])
                if fh:
                    fh.write(json.dumps([i, done[i]]) + "\n")
                    fh.flush()
    finally:
        if fh:
            fh.close()
    return [done[i] for i in range(len(sqls))]


def join_pairs(sqls: list[str]):
    from ..query import parse_join_query

    pairs = set()
    for sql in sqls:
        q = parse_join_query(sql)
        for e in q.edges:
            pairs.add(((q.tables[e.left], e.left_col), (q.tables[e.right], e.right_col)))
    return sorted(pairs)


def run(repo: str, sample_rows: int = 10_000, psql: list[str] | None = None,
        execute: bool = False, timeout: float = 60.0, log=print) -> dict:
    """Score STATS-CEB: sub-plan Q-error, plan quality and (optionally) runtime."""
    import duckdb

    from ..estimator import FactorEstimator
    from ..query import parse_join_query
    from ..stats import Statistics, collect_statistics
    from .endtoend import DEFAULT_SETTINGS, FORCED_PLAN_SETTINGS, connected_subsets, forced_sql, optimize, run_sql, subset_key, subset_sql, true_cost
    from .truth import exact_count

    base = data_dir()
    db = os.path.join(base, "stats.duckdb")
    build_database(repo, db)
    con = duckdb.connect(db, read_only=True)
    wl = workload(repo)
    queries = [parse_join_query(sql) for _, sql in wl]
    subs = sub_plans(repo)
    sub_queries = [parse_join_query(sql) for sql, _ in subs]

    def all_tables(q):
        return frozenset(q.tables)

    truth = cached_json(os.path.join(base, "stats_sub_truth.json"),
                        lambda: [exact_count(con, q, all_tables(q)) for q in sub_queries])
    mismatched = sum(exact_count(con, q, all_tables(q)) != card for q, (card, _) in zip(queries, wl))
    stats_path = os.path.join(base, f"stats_stats_{sample_rows}.json.gz")
    if not os.path.exists(stats_path):
        collect_statistics(con, TABLES, join_pairs([sql for _, sql in wl]), sample_rows=sample_rows).save(stats_path)
    est = FactorEstimator(Statistics.load(stats_path))
    start = __import__("time").perf_counter()
    ours = [est.estimate(q, all_tables(q)) for q in sub_queries]
    est_seconds = __import__("time").perf_counter() - start
    out: dict = {"queries": len(wl), "sub_plans": len(subs), "truth_mismatches": mismatched,
                 "stats_mb": round(os.path.getsize(stats_path) / 1e6, 2),
                 "estimate_ms": round(1000 * est_seconds / len(subs), 1), "q_error": {}}
    sources = {"kumosql": ours}
    if psql:
        sources["postgres"] = cached_json(os.path.join(base, "stats_sub_pg.json"),
                                          lambda: psql_explain_rows(psql, [sql for sql, _ in subs]))
    sources.update(published_estimates(repo))
    for name, vals in sources.items():
        out["q_error"][name] = percentiles(q_error(e, t) for e, t in zip(vals, truth))
    log("q-error", out["q_error"])

    # Plan quality: every connected sub-join of the 146 queries.
    sub_truth = cached_json(os.path.join(base, "stats_subsets_truth.json"), lambda: [
        {subset_key(s): exact_count(con, q, s) for s in connected_subsets(q)} for q in queries])
    cards = {"true": sub_truth, "kumosql": [{subset_key(s): est.estimate(q, s) for s in connected_subsets(q)}
                                            for q in queries]}
    if psql:
        def pg_subsets():
            res = []
            for q in queries:
                ss = connected_subsets(q)
                res.append(dict(zip(map(subset_key, ss), psql_explain_rows(psql, [subset_sql(q, s) for s in ss]))))
            return res
        cards["postgres"] = cached_json(os.path.join(base, "stats_subsets_pg.json"), pg_subsets)
    plans = []
    ratios: dict[str, list[float]] = {k: [] for k in cards}
    for i, q in enumerate(queries):
        row = {k: optimize(q, (lambda m: lambda s: m[subset_key(s)])(cards[k][i])) for k in cards}
        best = max(true_cost(row["true"], sub_truth[i]), 1.0)
        for k in cards:
            ratios[k].append(true_cost(row[k], sub_truth[i]) / best)
        plans.append(row)
    out["plan_cost_vs_optimal"] = {k: percentiles(v) for k, v in ratios.items()}
    log("plan cost / optimal", out["plan_cost_vs_optimal"])

    if execute:
        con.execute("SET threads=4")
        times: dict[str, list[float]] = {"duckdb": [], **{k: [] for k in cards}}
        wrong = 0
        for i, q in enumerate(queries):
            sqls = {"duckdb": wl[i][1].rstrip(";"), **{k: forced_sql(q, plans[i][k]) for k in cards}}
            for k, sql in sqls.items():
                con.execute(DEFAULT_SETTINGS if k == "duckdb" else FORCED_PLAN_SETTINGS)
                t, res = run_sql(con, sql, timeout)
                if t < timeout / 3:
                    t = min(t, run_sql(con, sql, timeout)[0])
                if res is not None and res[0][0] != wl[i][0]:
                    wrong += 1
                times[k].append(t)
        out["runtime"] = {k: {"seconds": round(sum(min(t, timeout) for t in v), 1),
                              "timeouts": sum(t == float("inf") for t in v)} for k, v in times.items()}
        out["runtime"]["timeout_seconds"] = timeout
        out["wrong_results"] = wrong
        log("runtime", out["runtime"])
    return out
