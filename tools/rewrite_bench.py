"""Run SQL-RewriteBench with KumoSQL's proof-gated query rewriter.

SQL-RewriteBench (https://github.com/SQL-RewriteBench/benchmark, Apache-2.0)
packages 180 PostgreSQL rewrite cases over TPC-DS and DSB data. A method gets
each case's ``benchmark_input.sql`` and ``schema_profile.yaml`` and returns a
rewritten statement or "no rewrite". The benchmark executes both statements,
requires identical results, and scores each case with CGOQ (correctness-gated
optimization quality: runtime speedup plus credit for a simpler statement).
Unsafe or non-executable rewrites score zero; the headline is CGOQ@N over all
180 cases.

KumoSQL's method is ``kumosql.query_optimizer.optimize``: rule-based rewrites
whose output is only returned when KumoSQL's prover proves it equivalent to the
input. No LLM is involved at evaluation time and the reference rewrites are
never read by the method.

    python tools/rewrite_bench.py --bench ../benchmark --method kumosql
    python tools/rewrite_bench.py --bench ../benchmark --method reference   # the benchmark's own rewrites
    python tools/rewrite_bench.py --bench ../benchmark --method kumosql --no-exec  # proofs only

Execution needs PostgreSQL with the TPC-DS and DSB data loaded (see
docs/rewrite-benchmarks.md); ``--db tpcds_sf10=tpcds --db dsb=dsb`` maps the
case databases to local database names. Timings are the median of five runs
after one warm-up, as in the benchmark's released reports; the two statements
alternate so that drift in the machine's speed affects both alike.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from kumosql import query_optimizer  # noqa: E402

POOLS = ("EQUIV", "PERF", "ROBUST", "PG-AWARE")
CASES_DIR = ("benchmark", "benchmark_inputs packages", "cases")
REFERENCE_DIR = ("benchmark", "reproducibility", "reference_bundle", "cases")
WORKBOOK = ("output", "results", "excution results", "Case_reference_Sql_execution_report.xlsx")


def load_cases(bench: Path, only: list[str] | None = None) -> list[dict]:
    cases = []
    for folder in sorted(bench.joinpath(*CASES_DIR).iterdir()):
        name = folder.name
        if only and not any(name == o or name.startswith(o) for o in only):
            continue
        profile = yaml.safe_load((folder / "schema" / "schema_profile.yaml").read_text(encoding="utf-8"))
        cases.append(
            {
                "name": name,
                "pool": name.rsplit("-", 1)[0],
                "sql": (folder / "sql" / "benchmark_input.sql").read_text(encoding="utf-8"),
                "profile": profile,
                "database": profile.get("database"),
            }
        )
    return cases


def is_held_out(name: str) -> bool:
    """Cases numbered 4, 9, 14, ... in each pool (one in five) are held out from rule development."""

    return int(name.rsplit("-", 1)[1]) % 5 == 4


def scs_scorer(bench: Path):
    """The benchmark's own SCS implementation, with caps fit as its docs describe."""

    sys.path.insert(0, str(bench / "src" / "benchmark scripts"))
    import calculate_scs  # type: ignore

    rows = []
    workbook = bench.joinpath(*WORKBOOK)
    if workbook.exists():
        from openpyxl import load_workbook

        sheet = load_workbook(workbook, read_only=True, data_only=True)["Case Report"]
        values = list(sheet.iter_rows(values_only=True))
        header = values[0]
        rows = [dict(zip(header, r))["benchmark_sql_sql"] for r in values[1:] if r and r[1]]
    if not rows:
        rows = [c["sql"] for c in load_cases(bench)]
    caps = calculate_scs.fit_caps([calculate_scs.extract_features(s, "postgres")["features"] for s in rows])
    return lambda sql: calculate_scs.score_sql(sql, "postgres", caps)["scs"]


def cgoq(speedup: float, cb: float, cr: float) -> float:
    """CGOQ with the benchmark's default parameters (``calculate_cgoq.compute_row_cgoq``)."""

    delta, lam, theta, cap, gamma = 0.05, 0.4, 0.10, 0.30, 0.10
    tau = math.log(2 / 1.05)
    z = math.log(speedup)
    mag = max(abs(z) - math.log(1 + delta), 0.0)
    r = 0.0 if mag == 0 else math.copysign(math.tanh(mag / tau), z)
    drop = max(0.0, (cb - cr) / max(cb, 1e-9))
    k = min(1.0, max(0.0, (drop - theta) / (cap - theta)))
    excess = max(math.log(1 / speedup) - math.log(1 + delta), 0.0)
    b = 1 - min(1.0, max(0.0, excess / (math.log(1 + gamma) - math.log(1 + delta))))
    return 100.0 * (r + lam * (1 - abs(r)) * b * k)


class Executor:
    def __init__(self, databases: dict[str, str], host: str, runs: int, timeout_s: float):
        import psycopg

        self.psycopg = psycopg
        self.databases = databases
        self.host = host
        self.runs = runs
        self.timeout_ms = int(timeout_s * 1000)
        self.connections: dict = {}

    def _connection(self, database: str):
        name = self.databases.get(database, database)
        if name not in self.connections:
            conn = self.psycopg.connect(host=self.host, dbname=name, autocommit=True)
            conn.execute(f"SET statement_timeout = {self.timeout_ms}")
            self.connections[name] = conn
        return self.connections[name]

    def run(self, database: str, sql: str) -> dict:
        """Execute once: ``{"ok", "rows", "ms"}`` or ``{"ok": False, "error"}``."""

        conn = self._connection(database)
        try:
            start = time.perf_counter()
            cur = conn.execute(sql.strip().rstrip(";"))
            rows = cur.fetchall() if cur.description else []
            return {"ok": True, "rows": rows, "ms": (time.perf_counter() - start) * 1000}
        except Exception as error:  # noqa: BLE001 - every database error is a result
            return {"ok": False, "error": f"{type(error).__name__}: {str(error).strip()[:300]}"}

    def compare(self, database: str, benchmark: str, rewrite: str) -> dict:
        """Run both statements (one warm-up each, then alternating), median times and first results."""

        first_b = self.run(database, benchmark)
        if not first_b["ok"]:
            return {"benchmark_error": first_b["error"]}
        first_r = self.run(database, rewrite)
        if not first_r["ok"]:
            return {"rewrite_error": first_r["error"], "benchmark_rows": first_b["rows"]}
        tb, tr = [], []
        runs = self.runs if max(first_b["ms"], first_r["ms"]) < self.timeout_ms / 3 else 1
        for _ in range(runs):
            for sql, times in ((benchmark, tb), (rewrite, tr)):
                got = self.run(database, sql)
                if not got["ok"]:
                    return {"rewrite_error": got["error"], "benchmark_rows": first_b["rows"]}
                times.append(got["ms"])
        return {
            "benchmark_rows": first_b["rows"],
            "rewrite_rows": first_r["rows"],
            "benchmark_ms": statistics.median(tb),
            "rewrite_ms": statistics.median(tr),
        }


def _norm(value):
    if isinstance(value, float):
        return round(value, 9)
    return value


def results_equal(left: list, right: list, ordered: bool) -> bool:
    a = [tuple(_norm(v) for v in r) for r in left]
    b = [tuple(_norm(v) for v in r) for r in right]
    if Counter(a) != Counter(b):
        return False
    return a == b if ordered else True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bench", type=Path, required=True, help="checkout of SQL-RewriteBench/benchmark")
    parser.add_argument("--method", default="kumosql", choices=["kumosql", "reference", "none"])
    parser.add_argument("--cases", nargs="*", help="case names or prefixes (default: all 180)")
    parser.add_argument("--no-exec", action="store_true", help="only rewrite and prove; do not execute")
    parser.add_argument("--host", default="/tmp")
    parser.add_argument("--db", action="append", default=[], help="CASE_DB=LOCAL_DB, e.g. tpcds_sf10=tpcds")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=120.0, help="statement timeout in seconds")
    parser.add_argument("--cost", action="store_true", help="let the rewriter compare EXPLAIN costs")
    parser.add_argument("--out", type=Path, help="write per-case results as JSON")
    parser.add_argument("--jobs", type=int, default=1, help="rewrite this many cases in parallel before executing")
    parser.add_argument("--rewrites", type=Path, help="reuse the rewrites in an earlier --out file instead of rewriting")
    parser.add_argument("--split", choices=["all", "dev", "held-out"], default="all", help="held-out: cases numbered 4, 9, 14, ... in each pool, kept out of rule development")
    args = parser.parse_args(argv)

    cases = load_cases(args.bench, args.cases)
    if args.split != "all":
        cases = [c for c in cases if is_held_out(c["name"]) == (args.split == "held-out")]
    earlier = {r["case"]: r for r in json.loads(args.rewrites.read_text())} if args.rewrites else {}
    databases = dict(item.split("=", 1) for item in args.db)
    if args.method == "kumosql" and args.jobs > 1:
        from concurrent.futures import ProcessPoolExecutor

        todo = [c for c in cases if c["name"] not in earlier]
        explain = (databases, args.host) if args.cost and not args.no_exec else None
        with ProcessPoolExecutor(args.jobs) as pool:
            for case, outcome in zip(todo, pool.map(_rewrite_case, todo, [explain] * len(todo))):
                earlier[case["name"]] = outcome
    scs = scs_scorer(args.bench)
    executor = None if args.no_exec else Executor(databases, args.host, args.runs, args.timeout)

    results = []
    for case in cases:
        started = time.perf_counter()
        record = {"case": case["name"], "pool": case["pool"]}
        if args.method == "reference":
            ref = args.bench.joinpath(*REFERENCE_DIR, case["name"], "sql", "reference_rewrite_01.sql")
            rewritten, outcome = ref.read_text(encoding="utf-8"), None
        elif args.method == "none":
            rewritten, outcome = None, None
        elif case["name"] in earlier:
            rewritten = earlier[case["name"]].get("sql")
            record["steps"] = earlier[case["name"]].get("steps", [])
            record["reason"] = earlier[case["name"]].get("reason", "")
            if "seconds" in earlier[case["name"]]:
                started -= earlier[case["name"]]["seconds"]
        else:
            cost = None
            if args.cost and executor is not None:
                cost = _explain_cost(executor, case["database"])
            outcome = query_optimizer.optimize(
                case["sql"],
                query_optimizer.Catalog.from_schema_profile(case["profile"]),
                dialect="postgres",
                cost=cost,
            )
            rewritten = outcome.sql
            record["steps"] = list(outcome.steps)
            record["reason"] = outcome.reason
        record["rewrite_seconds"] = round(time.perf_counter() - started, 3)
        if rewritten is None or _same_text(rewritten, case["sql"]):
            record["status"] = "NO_REWRITE"
            record["cgoq"] = 0.0
            results.append(record)
            _print(record)
            continue
        record["sql"] = rewritten
        cb, cr = scs(case["sql"]), scs(rewritten)
        record["scs"] = [cb, cr]
        if executor is None:
            record["status"] = "UNEXECUTED"
            record["cgoq"] = None
            results.append(record)
            _print(record)
            continue
        got = executor.compare(case["database"], case["sql"], rewritten)
        if "benchmark_error" in got:
            record.update(status="BENCHMARK_INPUT_FAILED", error=got["benchmark_error"], cgoq=0.0)
        elif "rewrite_error" in got:
            record.update(status="NON_EXECUTABLE", error=got["rewrite_error"], cgoq=0.0)
        else:
            ordered = query_optimizer.has_top_level_order(case["sql"], "postgres")
            if not results_equal(_jsonable(got["benchmark_rows"]), _jsonable(got["rewrite_rows"]), ordered):
                record.update(status="UNSAFE_REWRITE", cgoq=0.0)
            else:
                speedup = got["benchmark_ms"] / max(got["rewrite_ms"], 1e-6)
                record.update(
                    status="VALID",
                    ms=[round(got["benchmark_ms"], 2), round(got["rewrite_ms"], 2)],
                    speedup=round(speedup, 4),
                    cgoq=round(cgoq(speedup, cb, cr), 3),
                )
        results.append(record)
        _print(record)

    _summary(results, len(cases))
    if args.out:
        args.out.write_text(json.dumps(results, indent=1, default=str))
    return 0


def _rewrite_case(case: dict, explain: tuple | None = None) -> dict:
    """Rewrite one case in a worker process; ``explain=(databases, host)`` turns on the EXPLAIN cost guard."""

    started = time.perf_counter()
    try:
        executor = Executor(explain[0], explain[1], 1, 60) if explain is not None else None
        cost = _explain_cost(executor, case["database"]) if executor is not None else None
        try:
            outcome = query_optimizer.optimize(
                case["sql"], query_optimizer.Catalog.from_schema_profile(case["profile"]), dialect="postgres", cost=cost
            )
        finally:
            for conn in (executor.connections.values() if executor is not None else ()):
                conn.close()
    except Exception as error:  # noqa: BLE001 - a crash is a failure to rewrite, never a rewrite
        outcome = query_optimizer.Outcome(None, f"error: {type(error).__name__}: {str(error)[:200]}")
    return {
        "sql": outcome.sql,
        "steps": list(outcome.steps),
        "reason": outcome.reason,
        "seconds": round(time.perf_counter() - started, 3),
    }


def _explain_cost(executor: Executor, database: str):
    def cost(sql: str) -> float | None:
        conn = executor._connection(database)
        try:
            plan = conn.execute("EXPLAIN (FORMAT JSON) " + sql.strip().rstrip(";")).fetchone()[0]
            return float(plan[0]["Plan"]["Total Cost"])
        except Exception:  # noqa: BLE001
            return None

    return cost


def _same_text(a: str, b: str) -> bool:
    return " ".join(a.lower().split()).rstrip(";") == " ".join(b.lower().split()).rstrip(";")


def _jsonable(rows):
    return json.loads(json.dumps([list(r) for r in rows], default=str))


def _print(record: dict) -> None:
    extra = ""
    if record.get("speedup") is not None:
        extra = f" speedup={record['speedup']:.2f} scs={record['scs'][0]:.1f}->{record['scs'][1]:.1f}"
    elif record.get("error"):
        extra = " " + record["error"][:120]
    elif record.get("scs"):
        extra = f" scs={record['scs'][0]:.1f}->{record['scs'][1]:.1f}"
    cg = record.get("cgoq")
    print(f"{record['case']:<12} {record['status']:<15} cgoq={'-' if cg is None else f'{cg:7.2f}'}{extra}", flush=True)


def _summary(results: list[dict], n: int) -> None:
    status = Counter(r["status"] for r in results)
    total = sum(r["cgoq"] or 0.0 for r in results)
    print()
    print(f"cases {n}; " + ", ".join(f"{k} {v}" for k, v in sorted(status.items())))
    print(f"CGOQ@N {total / max(n, 1):.2f}; unsafe {status.get('UNSAFE_REWRITE', 0)}")
    valid = [r for r in results if r["status"] == "VALID"]
    if valid:
        gm = math.exp(sum(math.log(r["speedup"]) for r in valid) / len(valid))
        print(f"valid {len(valid)}; GM speedup {gm:.3f}; CGOQ@Valid {sum(r['cgoq'] for r in valid) / len(valid):.2f}")
    for pool in POOLS:
        rows = [r for r in results if r["pool"] == pool]
        if rows:
            print(f"  {pool:<9} n={len(rows):<3} CGOQ {sum(r['cgoq'] or 0 for r in rows) / len(rows):7.2f}  rewritten {sum(r['status'] != 'NO_REWRITE' for r in rows)}")


if __name__ == "__main__":
    raise SystemExit(main())
