# Refutation strength fixtures

`r024.jsonl` holds 42 query pairs that return different rows, one JSON object per line: `id`, `source` (the part of the sweep it came from), `dialect` (`duckdb` for 34, `bigquery` for 8), `schema` (tables, column types), `keys`, `left`, `right`, `witness` (a database, as rows in column order, on which the two return different rows), `why` and `note`. `unrefutable` marks R012b-18, whose difference is `APPROX_COUNT_DISTINCT` against `COUNT(DISTINCT)`: BigQuery's approximation is not reproduced on DuckDB, so no database shows it soundly.

**Source.** The pairs were collected by an outside research assistant (request R024) from five earlier requests: correlated domain joins (R006, 10 pairs), aggregation pushdown (R009, 12), windows (R011, 8), documented rewrites (R012b, 3) and decarrelation (R002, 9). The optimizer-bug pairs (R019) and the VeriEQL pairs (R013) are scored from their own sources, not copied here.

**Adaptation.** Each pair was rewritten into the fields above and its witness replayed on DuckDB with the optimizer off (`kumosql.duckdb_load.run_unoptimized`); the harness does so again on every run (`confirmed` in its output), so every pair is known to differ on its own data.

**The other sources** of `tools/refutation_strength_bench.py` are read from elsewhere: `tests/fixtures/optimizer_bugs`, `tests/fixtures/targeted_data/cases.json`, `tests/fixtures/unsafe_rewrite_cases.jsonl` and the VeriEQL download (Literature 46 and 47, Calcite 12 and 231; CC BY-NC-SA 4.0, never stored in the repository). See [the eval's page](../../../docs/evals/refutation-strength.md).

**Overlap.** The R006, R009, R011, R012b and R002 parts also belong to rewrite families other evals test (decorrelation, aggregate pushdown, window rewrites); the overlap with those evals was not checked by text. All of them were seen while building the refuter, so none is held out.
