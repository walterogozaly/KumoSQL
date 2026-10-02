"""ClickBench's 43 queries: does a proven KumoSQL rewrite make PostgreSQL faster?

ClickBench (https://github.com/ClickHouse/ClickBench, CC BY-NC-SA 4.0, so its
files are read from a checkout and not copied here) times 43 analytical
queries over one wide ``hits`` table. For each query this tool

1. runs ``kumosql.query_optimizer.optimize`` with only the table definition
   (no data, no LLM); a rewrite is returned only when the prover proves it;
2. executes the original and the rewrite on PostgreSQL, alternating five
   timed runs after a warm-up, and checks that both return the same rows
   (as bags, or in order when the query has ORDER BY).

The real ``hits`` data set (100 million rows) cannot be downloaded from this
environment, so ``--generate N`` fills the table with N synthetic rows drawn
per column type (small integer domains, a few repeated strings including the
empty string and Google URLs, dates in July 2013). Timings on synthetic data
show whether a rewrite removes work; they are not ClickBench results.

    python tools/clickbench_bench.py --clickbench ../ClickBench --db clickbench --generate 1000000
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from kumosql import query_optimizer as qo  # noqa: E402
from rewrite_bench import results_equal  # noqa: E402


def read_table(create_sql: str) -> tuple[list[tuple[str, str]], qo.Catalog]:
    columns = re.findall(r"^\s*(\w+)\s+([A-Za-z]+(?:\(\d+\))?)\s+NOT NULL", create_sql, flags=re.M)
    names = [n.lower() for n, _ in columns]
    catalog = qo.Catalog(
        columns={"hits": names},
        types={"hits": {n.lower(): t.lower() for n, t in columns}},
        not_null={"hits": set(names)},
        keys={"hits": []},
    )
    return columns, catalog


def _value(name: str, kind: str) -> str:
    kind = kind.upper()
    lname = name.lower()
    if kind.startswith("SMALLINT"):
        return "(random() * 3)::int" if lname.startswith(("is", "has", "dont", "java", "good", "cookie")) else "(random() * 20)::int"
    if kind.startswith("INTEGER"):
        return "(random() * 100)::int" if lname in ("counterid", "regionid") else "(random() * 100000)::int"
    if kind.startswith("BIGINT"):
        return "(random() * 1000000000)::bigint" if lname == "watchid" else "(random() * 50000)::bigint"
    if kind.startswith("DATE"):
        return "date '2013-07-01' + (random() * 30)::int"
    if kind.startswith("TIMESTAMP"):
        return "timestamp '2013-07-01' + random() * interval '30 days'"
    if kind.startswith("CHAR"):
        return "'a'"
    if lname in ("url", "referer"):
        return "(ARRAY['', 'http://www.google.com/search?q=' || (random() * 50)::int, 'https://example.org/' || (random() * 500)::int || '/page', 'http://yandex.ru/' || (random() * 200)::int || '/'])[1 + (random() * 3)::int]"
    if lname == "title":
        return "(ARRAY['', 'Google search', 'News ' || (random() * 100)::int, 'Shop'])[1 + (random() * 3)::int]"
    return "(ARRAY['', 'a', 'b', 'phrase ' || (random() * 300)::int])[1 + (random() * 3)::int]"


def generate(conn, columns: list[tuple[str, str]], create_sql: str, rows: int) -> None:
    conn.execute("DROP TABLE IF EXISTS hits")
    conn.execute(create_sql)
    select = ", ".join(_value(n, t) for n, t in columns)
    conn.execute(f"INSERT INTO hits SELECT {select} FROM generate_series(1, {rows})")
    conn.execute("ANALYZE hits")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--clickbench", type=Path, required=True, help="checkout of ClickHouse/ClickBench")
    parser.add_argument("--db", default="clickbench")
    parser.add_argument("--host", default="/tmp")
    parser.add_argument("--generate", type=int, default=0, help="(re)create hits with this many synthetic rows")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--no-exec", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    create_sql = (args.clickbench / "postgresql" / "create.sql").read_text()
    queries = [q.strip() for q in (args.clickbench / "postgresql" / "queries.sql").read_text().splitlines() if q.strip()]
    columns, catalog = read_table(create_sql)
    conn = None
    if not args.no_exec:
        import psycopg

        conn = psycopg.connect(host=args.host, dbname=args.db, autocommit=True)
        if args.generate:
            generate(conn, columns, create_sql, args.generate)
        conn.execute("SET statement_timeout = 600000")

    def run(sql):
        start = time.perf_counter()
        cur = conn.execute(sql.rstrip(";"))
        rows = cur.fetchall()
        return rows, (time.perf_counter() - start) * 1000

    results = []
    for number, sql in enumerate(queries, start=1):
        outcome = qo.optimize(sql, catalog, dialect="postgres")
        record = {"query": number, "rewritten": outcome.sql is not None, "steps": list(outcome.steps), "sql": outcome.sql}
        if outcome.sql is not None and conn is not None:
            try:
                base_rows, _ = run(sql)
                new_rows, _ = run(outcome.sql)
                tb, tr = [], []
                for _ in range(args.runs):
                    tb.append(run(sql)[1])
                    tr.append(run(outcome.sql)[1])
                ordered = qo.has_top_level_order(sql)
                same = results_equal(json.loads(json.dumps(base_rows, default=str)), json.loads(json.dumps(new_rows, default=str)), ordered)
                record.update(
                    same_rows=same,
                    ms=[round(statistics.median(tb), 2), round(statistics.median(tr), 2)],
                    speedup=round(statistics.median(tb) / max(statistics.median(tr), 1e-6), 3),
                )
            except Exception as error:  # noqa: BLE001
                record["error"] = f"{type(error).__name__}: {error}"[:200]
        results.append(record)
        extra = f" speedup={record['speedup']} same_rows={record['same_rows']}" if "speedup" in record else (" " + record.get("error", "") if record.get("error") else "")
        print(f"Q{number:<3} {'rewritten ' + '+'.join(sorted(set(outcome.steps))) if outcome.sql else 'none'}{extra}", flush=True)

    changed = [r for r in results if r["rewritten"]]
    timed = [r for r in changed if "speedup" in r]
    faster = [r for r in timed if r["same_rows"] and r["speedup"] >= 1.1]
    wrong = [r for r in timed if not r["same_rows"]]
    print()
    print(f"queries {len(results)}; proven rewrites {len(changed)}; executed {len(timed)}; same rows {len(timed) - len(wrong)}; at least 10% faster {len(faster)}")
    if timed:
        gm = statistics.geometric_mean([r["speedup"] for r in timed])
        print(f"geometric-mean speedup over rewritten queries {gm:.3f}")
    if args.out:
        args.out.write_text(json.dumps(results, indent=1))
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
