"""Score KumoSQL on rewrites that vendor and style-guide documentation calls equivalent.

Each case in ``tests/fixtures/documented_rewrites/cases.jsonl`` is a query pair written for KumoSQL
(BigQuery dialect, generic tables) that mirrors one documented before/after rewrite: SQLFluff rules,
dbt's SQL style guide, BigQuery and Snowflake guides, and Redshift and SQL Server tuning advice. The
documentation text is not copied; ``source`` links the page and ``claim`` says what it recommends.
Some documented rewrites are wrong as written (a date filter that spans a year, an ``IN`` turned into
a join that repeats rows, a filter on another column); those are labelled ``not_equivalent`` and must
never be proved.

Labels are hand-assigned and checked on DuckDB exactly as for DB-GPT's examples
(``tools/dbgpt_rules_bench.py``, whose deciding and checking code this reuses): random databases for
``equivalent``, a stored counterexample for ``not_equivalent``, every difference confirmed with
DuckDB's optimizer off.

    python tools/documented_rewrites_bench.py
    python tools/documented_rewrites_bench.py --write-results
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dbgpt_rules_bench as shared  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "documented_rewrites"


def load_cases():
    return shared.load_cases(FIXTURES)


def results_row(results: list[dict]) -> dict:
    from bench_common import today

    equivalent = [r for r in results if r["label"] == "equivalent"]
    different = [r for r in results if r["label"] == "not_equivalent"]
    counts = Counter(r["outcome"] for r in results)
    return {
        "suite": "Documented rewrites (vendor and style guides)",
        "order": 37,
        "size": len(results),
        "score": f"{sum(r['outcome'] == 'proven' for r in equivalent)}/{len(equivalent)} proved, {sum(r['outcome'] == 'refuted' for r in different)}/{len(different)} refuted, {sum(r['wrong'] for r in results)} wrong",
        "metric": "Rewrites that SQLFluff, dbt, BigQuery, Snowflake, Redshift and SQL Server documentation recommends, as cases written for KumoSQL: sound ones proved, the documented rewrites that change results refuted by a database.",
        "evidence": "proof",
        "correctness": "Labels checked on DuckDB by the test suite (random databases, stored counterexamples, optimizer off); wrong is a proof against a not-equivalent label or a refutation against an equivalent one.",
        "coverage": {k: counts[k] for k in ("proven", "refuted", "unknown") if counts[k]},
        "held_out": "none",
        "docs": "docs/evals/documented-rewrites.md",
        "command": "python tools/documented_rewrites_bench.py --write-results",
        "date": today(),
        "caveats": "Cases written for KumoSQL from documented claims collected by an outside research assistant and re-labelled by hand on DuckDB; 25 cases, no held-out split (tuned on test). Legacy-SQL migration mappings and rewrites that need unenforced keys are left out.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write-results", action="store_true", help="update benchmarks/results/documented-rewrites.json")
    args = parser.parse_args(argv)
    from bench_common import quiet, write_results

    quiet()
    cases = load_cases()
    bad = {c.id: p for c in cases if (p := shared.label_problems(c))}
    for case_id, problems in bad.items():
        print(f"label of case {case_id} does not hold: {'; '.join(problems)}")
    results = [shared.decide(c) for c in cases]
    for result in results:
        print(f"{result['id']:>9} {result['label']:15} {result['outcome']:12}{' WRONG' if result['wrong'] else ''}")
    row = results_row(results)
    print(row["score"])
    if args.write_results:
        write_results("documented-rewrites", row)
    return 1 if bad or any(r["wrong"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
