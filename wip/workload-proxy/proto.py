"""Prototype: materialization proxy on STATS-CEB. Scratch only."""
import sys, json, hashlib, re, os, time, statistics, math
from concurrent.futures import ProcessPoolExecutor
sys.path.insert(0, "/home/claude/KumoSQL/src")
import logging
logging.getLogger("sqlglot").setLevel(logging.ERROR)
import sqlglot
from kumosql import view_candidates as vc
from kumosql import model_reuse as mr
from kumosql.joinorder.bench import stats_ceb
from kumosql.joinorder.query import parse_join_query, JoinQuery, Edge
from kumosql.joinorder.bench.truth import exact_count
from kumosql.joinorder.stats import collect_statistics, Statistics
from kumosql.joinorder.estimator import FactorEstimator
from kumosql.joinorder.planner import optimize, plan_cost

REPO = "/root/.kumosql-bench/End-to-End-CardEst-Benchmark"
DB = "/root/.kumosql-bench/stats.duckdb"
OUT = "/tmp/claude-0/wl"
CAP = 30_000_000


def schema():
    ddl = open(REPO + "/datasets/stats_simplified/stats.sql").read()
    out = {}
    for m in re.finditer(r"CREATE TABLE (\w+) \((.*?)\);", ddl, re.S):
        out[m.group(1).lower()] = [l.strip().split()[0].lower() for l in m.group(2).split(",\n") if l.strip()]
    return out


def held(name):
    return int(hashlib.sha256(("split:" + name).encode()).hexdigest(), 16) % 4 == 0


def cached(path, build):
    if os.path.exists(path):
        return json.load(open(path))
    v = build()
    json.dump(v, open(path, "w"))
    return v


def prove(task):
    q, sql, v, vsql, sch = task
    try:
        r = mr.rewrite_over_model(sql, vsql, schema=sch, timeout_ms=10000)
    except Exception as e:
        return (q, v, "error", str(e)[:100], None)
    return (q, v, r.status, r.reason[:100], r.sql)


def main():
    sch = schema()
    w = stats_ceb.workload(REPO)
    qs = {f"q{i:03d}": sql.rstrip(";") for i, (c, sql) in enumerate(w)}
    cards = {f"q{i:03d}": c for i, (c, sql) in enumerate(w)}
    dev = {k: v for k, v in qs.items() if not held(k)}
    cands = vc.mine(dev, sch)
    import duckdb
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET threads=4")

    def sizes():
        out = {}
        for i, c in enumerate(cands):
            jq = parse_join_query(c.sql())
            out[c.sql()] = exact_count(con, jq, frozenset(jq.tables))
        return out
    true_size = cached(f"{OUT}/cand_sizes.json", sizes)
    keep = [c for c in cands if true_size[c.sql()] <= CAP]
    print(len(cands), "mined;", len(keep), "under cap")
    views = {f"v{i:02d}": c for i, c in enumerate(keep)}

    graphs = {q: vc.graph_of(sql, sch) for q, sql in qs.items()}
    placements = {q: (vc.shapes_of(g) if g else {}) for q, g in graphs.items()}

    def proofs():
        tasks = []
        for v, c in views.items():
            for q, sql in qs.items():
                if c.shape in placements[q]:
                    tasks.append((q, sql, v, c.sql(), sch))
        print(len(tasks), "proof tasks", flush=True)
        with ProcessPoolExecutor(4) as pool:
            res = list(pool.map(prove, tasks, chunksize=4))
        return [list(r) for r in res]
    pr = cached(f"{OUT}/proofs.json", proofs)
    from collections import Counter
    print(Counter(r[2] for r in pr))
    json.dump({"views": {v: {"sql": c.sql(), "tables": list(c.shape.tables), "true_rows": true_size[c.sql()]} for v, c in views.items()}}, open(f"{OUT}/views.json", "w"))


if __name__ == "__main__":
    main()
