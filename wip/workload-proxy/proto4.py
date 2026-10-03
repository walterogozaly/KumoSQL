"""Prototype: calibrate, predict, compare. Scratch only."""
import sys, json, hashlib
sys.path.insert(0, "/home/claude/KumoSQL/src")
sys.path.insert(0, "/tmp/claude-0/wl")
from proto import held, OUT
from kumosql.cost_model import calibrate
from kumosql import backtest as bt
from kumosql.materialization import Template, Option, Candidate, Evidence, Problem, select


def freq(q):
    return [1, 1, 2, 4][int(hashlib.sha256(("freq:" + q).encode()).hexdigest(), 16) % 4]


F = json.load(open(f"{OUT}/features.json"))
M = json.load(open(f"{OUT}/measure.json"))
WRITE = float(sys.argv[1]) if len(sys.argv) > 1 else None

samples = [({"scan": F["q"][q][0], "join": F["q"][q][1], "query": 1.0}, M["q"][q]["t"]) for q in F["q"] if not held(q) and M["q"][q]["t"]]
cal = calibrate(samples, ["scan", "join", "query"])
print(json.dumps(cal.to_json(), indent=1))
w = dict(zip(cal.model.features, cal.model.weights))
wcell = WRITE if WRITE is not None else w["scan"] * 2


def tq(q):
    return cal.model.predict({"scan": F["q"][q][0], "join": F["q"][q][1], "query": 1.0})


def tqv(q, v):
    s, c = F["qv"][f"{q}|{v}"]
    return cal.model.predict({"scan": s, "join": c, "query": 1.0})


def tb(v):
    f = F["v"][v]
    return cal.model.predict({"scan": f["S"], "join": f["C"], "query": 1.0}) + wcell * f["W"]


served = {}
for key in F["qv"]:
    q, v = key.split("|")
    served.setdefault(v, []).append(q)

pred, meas, names = [], [], []
for v in sorted(M["v"]):
    route = [q for q in served.get(v, []) if M["q"][q]["t"] and tqv(q, v) < tq(q)]
    p = sum(freq(q) * (tq(q) - tqv(q, v)) for q in route) - tb(v)
    mm = 0.0
    bad = False
    for q in route:
        r = M["qv"].get(f"{q}|{v}")
        if r is None:
            bad = True
            continue
        after = r["t"] if r["t"] is not None else r["timeout"]
        mm += freq(q) * (M["q"][q]["t"] - after)
    mm -= M["v"][v]["build"]
    pred.append(p); meas.append(mm); names.append(v)
    print(v, len(served.get(v, [])), len(route), round(p, 3), round(mm, 3), "est_rows", round(F["v"][v]["est_rows"]), "true", F["v"][v]["true_rows"], "build", round(M["v"][v]["build"], 2), round(tb(v), 2))
print(json.dumps(bt.score(pred, meas, k=min(10, len(pred))), indent=1))
