"""Prototype: runtime cost model features and a small measurement. Scratch only."""
import sys, json, os, time, math, statistics, re
sys.path.insert(0, "/home/claude/KumoSQL/src")
sys.path.insert(0, "/tmp/claude-0/wl")
import logging
logging.getLogger("sqlglot").setLevel(logging.ERROR)
import sqlglot
from sqlglot import exp
from proto import schema, held, REPO, DB, OUT
from kumosql import view_candidates as vc
from kumosql.joinorder.bench import stats_ceb
from kumosql.joinorder.query import parse_join_query, JoinQuery, Edge
from kumosql.joinorder.stats import collect_statistics, Statistics
from kumosql.joinorder.estimator import FactorEstimator
from kumosql.joinorder.planner import optimize, plan_cost
from kumosql.joinorder.bench.endtoend import run_sql


def cols_used(jq, alias):
    used = set()
    for f in jq.filters.get(alias, []):
        used |= {c.name for c in f.find_all(exp.Column)}
    for e in jq.edges:
        if alias in (e.left, e.right):
            used.add(e.col(alias))
    for refs, node in jq.residual:
        if alias in refs:
            used |= {c.name for c in node.find_all(exp.Column) if c.table == alias}
    return max(len(used), 1)


def contracted(jq, group, name="V"):
    tables = {a: t for a, t in jq.tables.items() if a not in group}
    tables[name] = "__view__"
    edges = []
    seen = set()
    for e in jq.edges:
        l = name if e.left in group else e.left
        r = name if e.right in group else e.right
        if l == r:
            continue
        key = (l, e.left_col if l != name else e.left + "." + e.left_col, r, e.right_col if r != name else e.right + "." + e.right_col)
        edges.append(Edge(l, e.left_col, r, e.right_col))
    return JoinQuery(sql=jq.sql, tables=tables, filters={a: [] for a in tables}, edges=edges)


def features(jq, est, rows):
    card = lambda s: est.estimate(jq, s)
    plan = optimize(jq, card)
    c = plan_cost(plan, card)
    s = sum(rows[t] * cols_used(jq, a) for a, t in jq.tables.items())
    return s, c


def features_over(jq, group, est, rows, v_rows, v_cols):
    cq = contracted(jq, group)

    def card(sub):
        real = set()
        for a in sub:
            real |= set(group) if a == "V" else {a}
        return est.estimate(jq, frozenset(real))
    plan = optimize(cq, card)
    c = plan_cost(plan, card)
    s = sum(rows[t] * cols_used(jq, a) for a, t in jq.tables.items() if a not in group) + v_rows * v_cols
    return s, c


def main():
    import duckdb
    sch = schema()
    w = stats_ceb.workload(REPO)
    qs = {f"q{i:03d}": sql.rstrip(";") for i, (c, sql) in enumerate(w)}
    views = json.load(open(f"{OUT}/views.json"))["views"]
    proofs = json.load(open(f"{OUT}/proofs.json"))
    jqs = {q: parse_join_query(sql) for q, sql in qs.items()}
    stats_path = f"{OUT}/stats_stats.pkl.gz"
    if not os.path.exists(stats_path):
        con = duckdb.connect(DB, read_only=True)
        pairs = sorted({((jq.tables[e.left], e.left_col), (jq.tables[e.right], e.right_col)) for jq in jqs.values() for e in jq.edges})
        tables = sorted({t for jq in jqs.values() for t in jq.tables.values()})
        collect_statistics(con, tables, pairs, sample_rows=10000).save(stats_path)
    st = Statistics.load(stats_path)
    est = FactorEstimator(st)
    rows = {t: ts.rows for t, ts in st.tables.items()}
    graphs = {q: vc.graph_of(sql, sch) for q, sql in qs.items()}
    placements = {q: (vc.shapes_of(g) if g else {}) for q, g in graphs.items()}
    feats = {}
    for q, jq in jqs.items():
        feats[q] = features(jq, est, rows)
    vfeat = {}
    shapes = {}
    for v, rec in views.items():
        jv = parse_join_query(rec["sql"])
        e2 = FactorEstimator(st)
        vrows = e2.estimate(jv, frozenset(jv.tables))
        card = lambda s, jv=jv, e2=e2: e2.estimate(jv, s)
        plan = optimize(jv, card)
        ncols = len(jv.select)
        sb = sum(rows[t] * max(1, sum(1 for x in jv.select if x.find(exp.Column).table == a)) for a, t in jv.tables.items())
        vfeat[v] = {"est_rows": vrows, "true_rows": rec["true_rows"], "S": sb, "C": plan_cost(plan, card), "W": vrows * ncols, "ncols": ncols}
        g = vc.graph_of(rec["sql"], sch)
        shapes[v] = next(iter(vc.shapes_of(g)))  # unused
    pairs = {}
    by_shape = {}
    # recover the candidate shape from the view's own graph: the full-table shape
    for v, rec in views.items():
        g = vc.graph_of(rec["sql"], sch)
        full = [s for s, order in vc.shapes_of(g).items() if len(s.tables) == len(rec["tables"])]
        by_shape[v] = full[0]
    for q, v, status, reason, sql in proofs:
        if status != "rewritten":
            continue
        order = placements[q].get(by_shape[v])
        if order is None:
            continue
        jq = jqs[q]
        ncols_v = sum(cols_used(jq, a) for a in order)
        pairs[(q, v)] = features_over(jq, frozenset(order), est, rows, vfeat[v]["est_rows"], ncols_v)
    json.dump({"q": feats, "v": vfeat, "qv": {f"{q}|{v}": f for (q, v), f in pairs.items()}}, open(f"{OUT}/features.json", "w"))
    print(len(feats), len(vfeat), len(pairs))


if __name__ == "__main__":
    main()
