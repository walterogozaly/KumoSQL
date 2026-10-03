import sys, json, time, duckdb, sqlglot
sys.path.insert(0, "/home/claude/KumoSQL/src")
from kumosql.joinorder.bench import stats_ceb
from kumosql.joinorder.bench.endtoend import run_sql
repo = "/root/.kumosql-bench/End-to-End-CardEst-Benchmark"
w = stats_ceb.workload(repo)
con = duckdb.connect("/root/.kumosql-bench/stats.duckdb", read_only=True)
con.execute("SET threads=4")
out = []
for i, (card, sql) in enumerate(w):
    d = sqlglot.transpile(sql, read="postgres", write="duckdb")[0]
    t, res = run_sql(con, d, 20.0)
    if t != float("inf"):
        t = min(t, run_sql(con, d, 20.0)[0])
    out.append({"i": i, "t": t if t != float("inf") else None, "ok": res is not None and res[0][0] == card})
    print(i, round(t, 3), out[-1]["ok"], flush=True)
json.dump(out, open("/tmp/claude-0/wl/stats_base.json", "w"))
