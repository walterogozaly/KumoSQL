# Optimizer wrong-result bugs

`cases.jsonl` holds 24 query pairs, each taken from a public bug report in which a database's optimizer returned wrong rows. The trackers are Apache Calcite (8), Apache Spark (6), CockroachDB (5), DuckDB (2), MySQL (1) and ClickHouse (1). `source` links the report. `left` is the query as written and `right` is the rewrite the optimizer made, spelled out as SQL. `setup` and `data` give the report's tables and rows, or a minimal fixture where the report gives none, and `note` says how each pair was adapted to DuckDB.

The pairs were collected on 2026-10-03 by an outside research assistant (request R019, `/mnt/project-files/R019-wrong-result-pairs.json` in the project) and rewritten here to the fields above. The bug reports' text is not copied. Each pair was then re-run, and the tests re-run it every time: on DuckDB with its optimizer off (`kumosql.duckdb_load.run_unoptimized`), or on SQLite for `bug-005`, which DuckDB cannot run (an `EXISTS` in a `LEFT JOIN`'s `ON`). Every pair returns different rows on its own data.

One pair was dropped: R019-007 ran a single query twice, once with a DuckDB optimizer disabled, so it is not a pair of queries. The ids keep R019's numbers.

**Overlap.** None of these pairs appears in another eval's fixtures (checked by normalized text). Several belong to rewrite families KumoSQL already tests, such as empty-input aggregates, decorrelation, LEFT JOIN ON FALSE and null-safe equality.
