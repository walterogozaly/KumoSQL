"""Prototype: measure stored candidates. Scratch only."""
import sys, json, os, time, shutil, statistics
sys.path.insert(0, "/home/claude/KumoSQL/src")
sys.path.insert(0, "/tmp/claude-0/wl")
import logging
logging.getLogger("sqlglot").setLevel(logging.ERROR)
import sqlglot
from proto import REPO, DB, OUT
from kumosql.joinorder.bench import stats_ceb
from kumosql.joinorder.bench.endtoend import run_sql

WORK = "/root/.kumosql-bench/stats_work.duckdb"


def duck(sql, table=None):
    out = sqlglot.transpile(sql, read="postgres", write="duckdb")[0]
    return out.replace("mv0", table) if table else out


def median_run(con, sql, timeout, reps=3):
    times, res = [], None
    for _ in range(reps):
        t, r = run_sql(con, sql, timeout)
        if t == float("inf"):
            return None, None
        times.append(t)
        res = r
    return statistics.median(times), res


def main(which):
    import duckdb
    if not os.path.exists(WORK):
        shutil.copy(DB, WORK)
    con = duckdb.connect(WORK)
    con.execute("SET threads=4")
    w = stats_ceb.workload(REPO)
    qs = {f"q{i:03d}": sql.rstrip(";") for i, (c, sql) in enumerate(w)}
    cards = {f"q{i:03d}": c for i, (c, sql) in enumerate(w)}
    views = json.load(open(f"{OUT}/views.json"))["views"]
    proofs = json.load(open(f"{OUT}/proofs.json"))
    mpath = f"{OUT}/measure.json"
    m = json.load(open(mpath)) if os.path.exists(mpath) else {"q": {}, "v": {}, "qv": {}}
    for q, sql in qs.items():
        if q in m["q"]:
            continue
        t, r = median_run(con, duck(sql), 20.0)
        m["q"][q] = {"t": t, "ok": r is not None and r[0][0] == cards[q]}
    json.dump(m, open(mpath, "w"))
    served = {}
    for q, v, status, reason, sql in proofs:
        if status == "rewritten":
            served.setdefault(v, []).append((q, sql))
    for v in which:
        if v in m["v"]:
            continue
        rec = views[v]
        con.execute(f"DROP TABLE IF EXISTS mv_{v}")
        start = time.perf_counter()
        con.execute(f"CREATE TABLE mv_{v} AS {duck(rec['sql'])}")
        build = time.perf_counter() - start
        size = con.execute(f"SELECT COUNT(*) FROM mv_{v}").fetchone()[0]
        con.execute("CHECKPOINT")
        for q, sql in served.get(v, []):
            base = m["q"][q]["t"]
            if base is None:
                m["qv"][f"{q}|{v}"] = {"t": None, "skipped": "original timed out"}
                continue
            timeout = min(max(5 * base, 2.0), 30.0)
            t, r = median_run(con, duck(sql, f"mv_{v}"), timeout)
            m["qv"][f"{q}|{v}"] = {"t": t, "timeout": timeout if t is None else None, "same": None if r is None else r[0][0] == cards[q]}
        con.execute(f"DROP TABLE mv_{v}")
        con.execute("CHECKPOINT")
        m["v"][v] = {"build": build, "rows": size}
        json.dump(m, open(mpath, "w"))
        print(v, round(build, 2), size, len(served.get(v, [])), flush=True)


if __name__ == "__main__":
    views = sorted(json.load(open(f"{OUT}/views.json"))["views"])
    if sys.argv[1:] == ["all"]:
        main(views)
    else:
        main(views[:: max(1, len(views) // 10)][:10])
