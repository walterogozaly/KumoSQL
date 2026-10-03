"""Prototype: hash-join features with a cache term. Scratch only."""
import sys, json, os, math, statistics
sys.path.insert(0, "/home/claude/KumoSQL/src"); sys.path.insert(0, "/tmp/claude-0/wl")
import logging; logging.getLogger("sqlglot").setLevel(logging.ERROR)
from proto import schema, held, REPO, OUT
from proto2 import cols_used, contracted
from kumosql import view_candidates as vc
from kumosql.joinorder.bench import stats_ceb
from kumosql.joinorder.query import parse_join_query
from kumosql.joinorder.stats import Statistics
from kumosql.joinorder.estimator import FactorEstimator
from kumosql.joinorder.planner import optimize
from kumosql.cost_model import calibrate
from kumosql import backtest as bt

T = float(sys.argv[1]) if len(sys.argv) > 1 else 2 ** 17


def hj(plan, f):
    if plan.left is None:
        return
    hj(plan.left, f); hj(plan.right, f)
    a, b = plan.left.card, plan.right.card
    build, probe = min(a, b), max(a, b)
    f["build"] += build; f["probe"] += probe
    f["out_large" if build > T else "out_small"] += plan.card


def feats(jq, card, scan):
    plan = optimize(jq, card)
    f = {"scan": scan, "build": 0.0, "probe": 0.0, "out_small": 0.0, "out_large": 0.0, "query": 1.0}
    hj(plan, f)
    return f


sch = schema(); w = stats_ceb.workload(REPO)
qs = {f"q{i:03d}": sql.rstrip(";") for i, (c, sql) in enumerate(w)}
views = json.load(open(f"{OUT}/views.json"))["views"]; proofs = json.load(open(f"{OUT}/proofs.json"))
M = json.load(open(f"{OUT}/measure.json"))
st = Statistics.load(f"{OUT}/stats_stats.pkl.gz"); est = FactorEstimator(st)
rows = {t: ts.rows for t, ts in st.tables.items()}
jqs = {q: parse_join_query(sql) for q, sql in qs.items()}
FQ = {}
for q, jq in jqs.items():
    FQ[q] = feats(jq, lambda s, jq=jq: est.estimate(jq, s), float(sum(rows[t] * cols_used(jq, a) for a, t in jq.tables.items())))
placements = {q: vc.shapes_of(vc.graph_of(sql, sch)) for q, sql in qs.items()}
by_shape = {}
for v, rec in views.items():
    g = vc.graph_of(rec["sql"], sch)
    by_shape[v] = [s for s in vc.shapes_of(g) if len(s.tables) == len(rec["tables"])][0]
FQV = {}
for q, v, status, reason, sql in proofs:
    if status != "rewritten" or f"{q}|{v}" not in M["qv"]:
        continue
    order = placements[q].get(by_shape[v]); jq = jqs[q]; group = frozenset(order)
    cq = contracted(jq, group)
    def card(sub, jq=jq, group=group):
        real = set()
        for a in sub:
            real |= set(group) if a == "V" else {a}
        return est.estimate(jq, frozenset(real))
    vrows = est.estimate(parse_join_query(views[v]["sql"]), frozenset(parse_join_query(views[v]["sql"]).tables))
    scan = sum(rows[t] * cols_used(jq, a) for a, t in jq.tables.items() if a not in group) + vrows * sum(cols_used(jq, a) for a in group)
    FQV[f"{q}|{v}"] = feats(cq, card, float(scan))
names = ["scan", "build", "probe", "out_small", "out_large", "query"]
samples = [(FQ[q], M["q"][q]["t"]) for q in FQ if not held(q) and M["q"][q]["t"]]
cal = calibrate(samples, names)
print({k: round(v, 3) if isinstance(v, float) else v for k, v in cal.cross_validated.items()}, dict(zip(names, [f"{x:.3g}" for x in cal.model.weights])))
P = cal.model.predict
for mode in ("diff", "ratio", "model"):
    pred, meas = [], []
    for key, r in M["qv"].items():
        q, v = key.split("|")
        if not M["q"][q]["t"] or key not in FQV: continue
        after = r["t"] if r["t"] is not None else r["timeout"]
        b = M["q"][q]["t"]
        p = {"diff": b - P(FQV[key]), "ratio": b * (1 - P(FQV[key]) / P(FQ[q])), "model": P(FQ[q]) - P(FQV[key])}[mode]
        pred.append(p); meas.append(b - after)
    s = bt.score(pred, meas, k=20)
    print(mode, {k: (round(v["value"], 3), [round(x, 2) for x in v["interval"]]) for k, v in s.items() if isinstance(v, dict) and v["value"] is not None})
# after-only q-error on pairs
pa = [P(FQV[k]) for k in M["qv"] if k in FQV and M["qv"][k]["t"]]
ma = [M["qv"][k]["t"] for k in M["qv"] if k in FQV and M["qv"][k]["t"]]
from kumosql.cost_model import error_summary
print("after q-error", error_summary(pa, ma))
