# Optimizer wrong-result bugs

[Plain-language version](../../docs_simple/evals/optimizer-bugs.md)

24 query pairs taken from public bug reports in which a database's optimizer returned wrong rows: Apache Calcite, Apache Spark, CockroachDB, DuckDB, MySQL and ClickHouse. Each pair is the query as written and the rewrite the optimizer made of it, spelled out as SQL, with the report's tables and rows. The two return different rows, so this is a **must-not-prove** set: a proof is a soundness bug. The pairs are in `tests/fixtures/optimizer_bugs` ([sources, adaptations and overlap](../../tests/fixtures/optimizer_bugs/README.md)). Results file: `optimizer-bugs`.

```
python tools/optimizer_bugs_bench.py                    # a few seconds
python tools/optimizer_bugs_bench.py --show unknown
python tools/optimizer_bugs_bench.py --write-results
```

## How a pair is decided

1. **proven**: `prove_equivalent_algebraic` (DuckDB dialect, output names ignored) proves the pair, using the column types, primary keys, UNIQUE and NOT NULL columns the setup declares (CHECK constraints are not used). Any proof is **wrong**.
2. **refuted**: the prover, with its counterexample search on (`search_counterexample=True`, see [refutation strength](refutation-strength.md)), finds the two queries differ. A database it attaches is replayed by the harness (`kumosql.refutation_replay`) and must separate the pair; one that does not is **wrong**.
3. **unknown**: anything else.

Every run also checks that each pair really differs on its own data: DuckDB with its optimizer off (`kumosql.duckdb_load.run_unoptimized`), or SQLite for the one pair DuckDB cannot run. Results are compared as lists when both queries end in `ORDER BY`, and as bags otherwise.

**Held out.** One case in five, by a hash of its id (5 of 24), is held out. The first run showed every case.

## Scores

2026-10-03: **21/24 refuted, 0 proved, 0 wrong**; every pair confirmed to differ and every attached database replayed. Held out: 4/5 refuted, 0 proved. Before the counterexample search was switched on here, 5/24 were refuted (held out 2/5).

| Outcome | Pairs |
| --- | --- |
| unknown | bug-001 (held out: the solver's counterexample does not separate the pair when run, so it is dropped and nothing else finds one in time), bug-005 (runs only on SQLite, and the search runs on DuckDB), bug-025 (differs only through an arbitrary `DISTINCT ON` pick) |
| refuted | the other 21 |

**Baseline.** On master before 2026-10-03 01:51 UTC, bug-004 was **proved**: a false proof. `SELECT 'US' FROM events` was proved equal to `SELECT 'US' FROM (SELECT COUNT(*) FROM events) g`, because a rule folded the outer select into a global aggregate and dropped its one-row result. The global-aggregate cardinality fix (#417) landed before this eval; bug-004 is kept as a regression in the eval's test. The counterexample search was built with these pairs in the refutation-strength eval; held-out bug-001 prompted the replay of solver counterexamples (tuned on test).

## Limits

* Most rewrites are SQL readings of the plan a report prints, not SQL the reporter wrote. Where a report shows only a plan, the pair encodes its reported effect on a minimal fixture.
* The rows come from the reports, so many pairs are on tiny or empty tables. That is the point: most of these bugs appear only on empty inputs, NULLs or duplicates.
