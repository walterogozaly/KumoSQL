"""Join-order and cardinality-estimation benchmarks (dev tooling).

    python tools/joinorder_bench.py stats-ceb --repo PATH [--psql "psql -h /tmp -p 5433 -d stats"] [--execute]
    python tools/joinorder_bench.py job --repo PATH [--psql "psql -d imdb"] [--execute]

``--repo`` is a clone of https://github.com/Nathaniel-Han/End-to-End-CardEst-Benchmark
(STATS-CEB) or https://github.com/danolivo/jo-bench (JOB).
Data and caches go to ``$KUMOSQL_BENCH_DATA`` (default ``~/.kumosql-bench``),
never into git. ``--psql`` adds Postgres' own estimates as a baseline (the
database must already hold the data). ``--execute`` also times every query in
DuckDB. Results are printed as JSON and saved next to the data. See
docs/joinorder.md.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from kumosql.joinorder.bench import job, stats_ceb  # noqa: E402
from kumosql.joinorder.bench.common import data_dir  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="suite", required=True)
    p = sub.add_parser("stats-ceb")
    p.add_argument("--repo", required=True)
    p.add_argument("--sample-rows", type=int, default=10_000)
    p.add_argument("--psql", help="psql command for a Postgres database holding STATS")
    p.add_argument("--execute", action="store_true", help="also time every query in DuckDB")
    p.add_argument("--timeout", type=float, default=60.0)
    p = sub.add_parser("job")
    p.add_argument("--repo", required=True, help="clone of https://github.com/danolivo/jo-bench")
    p.add_argument("--sample-rows", type=int, default=10_000)
    p.add_argument("--psql", help="psql command for a Postgres database holding IMDB")
    p.add_argument("--execute", action="store_true", help="also time every query in DuckDB")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--db", help="existing DuckDB file with the IMDB tables")
    args = parser.parse_args(argv)
    psql = shlex.split(args.psql) if args.psql else None
    if args.suite == "stats-ceb":
        result = stats_ceb.run(args.repo, args.sample_rows, psql, args.execute, args.timeout,
                               log=lambda *a: print(*a, file=sys.stderr))
    if args.suite == "job":
        result = job.run(args.repo, args.sample_rows, psql, args.execute, args.timeout, args.db,
                         log=lambda *a: print(*a, file=sys.stderr))
    path = os.path.join(data_dir(), f"{args.suite}-results.json")
    with open(path, "w") as fh:
        json.dump(result, fh, indent=2)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
