import sys, json
sys.path.insert(0, "/home/claude/KumoSQL/src"); sys.path.insert(0, "/tmp/claude-0/wl")
from proto import held, OUT
from kumosql.cost_model import calibrate
from kumosql import backtest as bt
F = json.load(open(f"{OUT}/features.json")); M = json.load(open(f"{OUT}/measure.json"))
samples = [({"scan": F["q"][q][0], "join": F["q"][q][1], "query": 1.0}, M["q"][q]["t"]) for q in F["q"] if not held(q) and M["q"][q]["t"]]
cal = calibrate(samples, ["scan", "join", "query"])
P = lambda s, c: cal.model.predict({"scan": s, "join": c, "query": 1.0})
pred, meas, rows = [], [], []
for key, r in M["qv"].items():
    q, v = key.split("|")
    if not M["q"][q]["t"]: continue
    after = r["t"] if r["t"] is not None else r["timeout"]
    p = P(*F["q"][q]) - P(*F["qv"][key]); m = M["q"][q]["t"] - after
    pred.append(p); meas.append(m); rows.append((key, round(p,4), round(m,4), r["same"], held(q)))
print(len(rows), "pairs; same:", sum(1 for r in rows if r[3]), "timeouts:", sum(1 for k,r in M["qv"].items() if r["t"] is None))
print(json.dumps({k: v["value"] if isinstance(v, dict) else v for k, v in bt.score(pred, meas, k=20).items()}, indent=0))
rows.sort(key=lambda r: -r[2])
for r in rows[:15]: print(r)
print("pred-positive:", sum(1 for p in pred if p > 0), "meas-positive:", sum(1 for m in meas if m > 0))
print("---- anchored to measured before ----")
for name, fn in [("calibrated", lambda q, key: P(*F["qv"][key]) / P(*F["q"][q])),
                 ("join only", lambda q, key: (F["qv"][key][1] + 1) / (F["q"][q][1] + 1)),
                 ("scan+join raw", lambda q, key: sum(F["qv"][key]) / sum(F["q"][q]))]:
    pred, meas = [], []
    for key, r in M["qv"].items():
        q, v = key.split("|")
        if not M["q"][q]["t"]: continue
        after = r["t"] if r["t"] is not None else r["timeout"]
        pred.append(M["q"][q]["t"] * (1 - fn(q, key))); meas.append(M["q"][q]["t"] - after)
    s = bt.score(pred, meas, k=20)
    print(name, {k: round(v["value"], 3) for k, v in s.items() if isinstance(v, dict) and v["value"] is not None})
print("---- measured before minus predicted after ----")
pred, meas = [], []
for key, r in M["qv"].items():
    q, v = key.split("|")
    if not M["q"][q]["t"]: continue
    after = r["t"] if r["t"] is not None else r["timeout"]
    pred.append(M["q"][q]["t"] - P(*F["qv"][key])); meas.append(M["q"][q]["t"] - after)
s = bt.score(pred, meas, k=20)
print({k: (round(v["value"], 3), [round(x, 3) for x in v["interval"]]) for k, v in s.items() if isinstance(v, dict) and v["value"] is not None})
import statistics
errs = [abs(p - m) for p, m in zip(pred, meas)]
print("median abs err", statistics.median(errs), "pred>0", sum(p > 0 for p in pred), "meas>0", sum(m > 0 for m in meas), "agree", sum((p > 0) == (m > 0) for p, m in zip(pred, meas)))
