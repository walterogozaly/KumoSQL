# Engine test suites

An execution-based regression eval. Other engines' test suites hold thousands of queries with real data. Each query runs in DuckDB, goes through every KumoSQL rewrite (inline single-use CTEs, remove trivial predicates, redundant parentheses and unused CTEs, deduplicate CTEs, remove redundant DISTINCT, format), and runs again. A rewrite that changes the result is a behaviour change, and that count must stay 0.

    python tools/engine_suites.py --suite duckdb-slt          # about 2 hours on 4 cores
    python tools/engine_suites.py --suite sqlglot-fixtures    # about 20 minutes
    python tools/engine_suites.py --suite sqlite-slt --stride 4 --max-queries 40
    python tools/engine_suites.py --suite duckdb-slt --limit 40 --stride 60   # quick sample

Add `--scoreboard` to rewrite the `benchmarks/results` rows, then run `python tools/scoreboard.py`. `--json FILE` keeps the summary; a `FILE.partial` log lets an interrupted run resume. Suites are sparse git clones into `$KUMOSQL_SUITES_DIR` (default `~/.cache/kumosql-suites`); nothing is vendored. The test `tests/test_engine_suites.py` covers the harness; its slow test runs a sample of the DuckDB suite.

## Suites

| Suite | Source (pinned in the summary) | Licence | What is taken |
| --- | --- | --- | --- |
| DuckDB SQLLogicTest | `duckdb/duckdb` `test/sql` | MIT | every `.test` file; setup runs natively, each `query` record is a case |
| SQLite SQLLogicTest | `gregrahn/sqllogictest` mirror, the SQLite corpus SQuaLity reuses | public domain / MIT (SQuaLity: MIT) | every 4th file, the first 40 queries of each, run on DuckDB as SQuaLity does |
| SQLGlot fixtures | `tobymao/sqlglot` `tests/fixtures` | MIT | the optimizer, qualify, simplify and identity fixtures, plus the TPC-H (22) and TPC-DS (99) queries with their bundled data |

SQuaLity ([paper](https://arxiv.org/abs/2410.21731), [code](https://github.com/suyZhong/SQuaLity)) pulls DuckDB's tests, SQLite's SQLLogicTest, PostgreSQL's regression tests and MySQL's tests into one format. The SQLite and DuckDB corpora are both SLT and run here; PostgreSQL's and MySQL's are not yet. For the SQLGlot fixtures each pair's expected output is also executed, so the fixture's own expectation is checked independently (`expected_match`); the check applies to SQLGlot's side, not to KumoSQL's.

## Method

1. A file's setup statements run natively in a fresh in-memory DuckDB. Files needing extensions, `load`, `restart` or other directives are skipped and counted.
2. A query must be one plain read-only statement with no random, clock or environment functions.
3. **Control**: the query is written DuckDB to BigQuery and back to DuckDB with no KumoSQL rule, and run twice. KumoSQL reads BigQuery, so this separates dialect-translation gaps from rewrite behaviour. Queries whose control fails, differs from the original, or is not repeatable are *unsupported*.
4. **Treated**: the BigQuery text goes through the canonical rule order and runs again. A different tree is *transformed*, the same tree is *declined*, and an exception or rule failure is an *error*. A rewrite that only dropped parentheses that DuckDB's writer then cannot re-read is a *translation artifact* (unsupported), not a KumoSQL failure.
5. **Wrong** is a transformed query whose result multiset differs from the control or that no longer runs. Wrong cases that the verifier had labelled `proven` are counted separately, as prover failures.
6. **Adapted variants**: a plain query is rarely touched by a cleanup rule, so each query also runs as equivalent variants that give the rules work (wrapped in a subquery, wrapped in a CTE, with an unused CTE, with `WHERE 1 = 1 AND (TRUE)`). A variant must first return the same rows as the query. Original and adapted cases are scored in separate rows.

Cases are keyed by the normalised query plus a hash of the file's setup, so identical cases shared between suites collapse into one and keep every provenance (`case_id` is `suite:file:line`). Files whose path hashes to 0 mod 4 are held out: fixes are developed against the other files, and both splits are reported.

Scores keep correctness (wrong, must be 0), coverage (transformed, declined, unsupported, timeout, error) and performance (median and p95 milliseconds through all rules) apart. In the scoreboard's coverage column `proven` means an executed rewrite whose result matched the control; it is evidence by execution, not a proof.

## Results

Measured 2026-10-02 after the fixes below.

| Row | Cases | Executed | Transformed | Declined | Unsupported | Timeout | Error | Wrong |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| DuckDB SQLLogicTest, original | 34,998 | 13,655 | 334 | 13,321 | 21,311 | 31 | 1 | 0 |
| DuckDB SQLLogicTest, adapted | 62,528 | 54,419 | 40,814 | 13,605 | 8,109 | 0 | 0 | 0 |
| SQLGlot fixtures, original | 4,640 | 593 | 121 | 472 | 4,044 | 1 | 2 | 0 |
| SQLGlot fixtures, adapted | 2,325 | 2,228 | 1,365 | 863 | 97 | 0 | 0 | 0 |
| SQLite SQLLogicTest, original | 6,050 | 6,035 | 1,929 | 4,106 | 15 | 0 | 0 | 0 |
| SQLite SQLLogicTest, adapted | 24,066 | 24,066 | 19,960 | 4,106 | 0 | 0 | 0 | 0 |

Most original queries are declined because they are already clean: few test queries contain the redundancy these rules remove. Most unsupported DuckDB cases use DuckDB-only syntax or functions that do not survive the BigQuery round trip, or depend on setup the harness does not replay. Timeouts are queries over 5 seconds, files over 15 minutes, or hung files. Summaries with per-rule counts are in `benchmarks/engine_suites/`.

## Bugs found

The first full DuckDB run found, and this work fixed with regression tests:

- `format_sql` turned `- -i` into `--i`, which is a comment. The formatter now refuses any output that no longer parses like its input.
- sqlfluff raised an `AssertionError` on some inputs, which escaped `apply_rules`; it is now a reported rule failure.
- `remove_unused_ctes` dropped a CTE passed to a table function by bare name (`histogram_values(cte, x)`, DuckDB syntax) and the verifier still said `proven`. The rule now keeps a CTE whose name appears as a bare identifier. The verifier side is tracked separately.
- `remove_trivial_predicates` emptied `FILTER (WHERE TRUE)`, producing invalid SQL (the output check caught it, but the rewrite was lost); the clause is left alone.

## Caveats

- Rows are compared as multisets, so row order is not checked (a top-level `ORDER BY` is not verified).
- The check is as strong as the test data. Rewrites are not proven here; the prover's verdict is recorded and compared with the execution result.
- The SQLite SLT row is a sample (every 4th file, 40 queries per file), not the full 5.9 million queries.
- Cases whose original run does not match the suite's expected output (`expected_match` false) stay in the score, because the check compares treated against control rather than against the expected text.
- PostgreSQL and MySQL tests from SQuaLity, and DuckDB's non-SLT tests, are not included.
