"""Measure the workload advisor on the JOB development/held-out family split.

    python tools/workload_job_bench.py --repo PATH --write-results

The candidate miner and selector see only families 1--16. Families 17--33 are
used once, after selection, to evaluate proof-approved rewrites and total time.
The DuckDB file and intermediate caches live under ``KUMOSQL_BENCH_DATA``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from kumosql.joinorder.bench.job_materialize import run  # noqa: E402
from bench_common import quiet, today, write_results  # noqa: E402


def _score_row(result: dict) -> dict:
    held = result["held_out"]
    before = held["baseline_seconds"]
    after = held["advisor_with_builds_seconds"]
    saving = before - after
    pct = 100 * saving / before if before else 0.0
    return {
        "suite": "Workload advisor JOB proxy",
        "order": 65,
        "size": held["queries_measured"],
        "score": f"{after:.2f}s vs {before:.2f}s; {pct:+.1f}%",
        "metric": "Held-out JOB runtime from measured per-query timings, scaled to the declared daily frequency and including selected-view builds.",
        "evidence": "executed",
        "correctness": (f"{held['wrong']} wrong among {result['candidate_mining']['development_execution_pairs'] + held['proofs']} "
                        "proof-approved rewrite attempts; result bags compared in DuckDB, "
                        "and differences rechecked with DuckDB's optimizer disabled."),
        "coverage": {},
        "held_out": f"Families 17-33 ({held['queries_measured']}/54 queries measured); views chosen on families 1-16.",
        "docs": "docs/evals/workload-advisor-job.md",
        "command": "python tools/workload_job_bench.py --repo PATH --query-runs-per-day 10 --write-results",
        "date": today(),
        "caveats": ("Ten query runs/day is a stated scenario assumption, not JOB schedule data. Bounded candidate sample; "
                    "local JOB/IMDb license terms are not verified. One-machine DuckDB timings, not a warehouse cost estimate."),
        "performance": (f"Development-selected advisor: {after:.2f}s/day incl. builds; baseline: {before:.2f}s/day "
                        f"({held['queries_measured']} measured query timings scaled to the declared frequency)."),
        "measurements": result,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="clone of https://github.com/danolivo/jo-bench")
    parser.add_argument("--db", help="existing DuckDB file; otherwise uses the benchmark cache")
    parser.add_argument("--max-views", type=int, default=10)
    parser.add_argument("--max-candidates", type=int, default=40)
    parser.add_argument("--row-cap", type=int, default=30_000_000)
    parser.add_argument("--sample-rows", type=int, default=10_000)
    parser.add_argument("--query-runs-per-day", type=float, default=1.0,
                        help="assumed daily frequency for each query; each selected view builds once per day")
    parser.add_argument("--proof-timeout-ms", type=int, default=1_000)
    parser.add_argument("--execution-timeout", type=float, default=60.0)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--write-results", action="store_true", help="write the JOB row and regenerate README scoreboard")
    args = parser.parse_args(argv)
    quiet()
    result = run(args.repo, db=args.db, max_views=args.max_views, max_candidates=args.max_candidates,
                 row_cap=args.row_cap, sample_rows=args.sample_rows,
                 query_runs_per_day=args.query_runs_per_day,
                 proof_timeout_ms=args.proof_timeout_ms, execution_timeout=args.execution_timeout,
                 repeats=args.repeats, log=lambda line: print(line, file=sys.stderr, flush=True))
    if args.write_results:
        path = write_results("workload-advisor-job", _score_row(result))
        result["scoreboard_result_path"] = str(path)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
