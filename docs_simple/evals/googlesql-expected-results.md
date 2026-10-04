# Checking BigQuery translation against Google's expected rows

[Simple eval index](README.md) · [Full reference](../../docs/evals/googlesql-expected-results.md)

KumoSQL looks for a database on which two queries differ by running them on a local engine, DuckDB, after translating BigQuery SQL. If the local engine answers differently from BigQuery, KumoSQL could report a difference that BigQuery would never show. This eval checks the translation against an outside answer key.

Google publishes compliance tests for GoogleSQL, the language of BigQuery. Each test has a query and the rows it must return. The eval builds each test's tables from the published rows, runs the translated query on DuckDB, and compares.

For example, `EXTRACT(MILLISECOND FROM TIME '12:34:56.999999')` must give 999. The local engine gave 56999, because it counted the whole seconds. The translation now fixes that. For a query like `SPLIT(x, NULL)` or a time zone written as `UTC+1234`, where the local engine reads the SQL differently and there is no safe fix, the translation now refuses and the pair stays unknown.

## What counts

Tests that need things BigQuery does not have (protos, other integer sizes, parameters) are skipped. Of the rest, a case can agree, be declined, be impossible to run locally, or be of a result type the harness cannot read. A wrong case means the translation ran and returned other rows; that must stay at zero. A case that only differs because the tests assume a Los Angeles time zone and BigQuery uses UTC is counted apart.

## Limits of the evidence

- Agreement shows the translation matches Google's reference answers, not BigQuery itself.
- About a third of the cases carry no verdict because the local engine or the parser cannot run them.
- A fifth of the test files were held out. The first run on them, after fixing only from the other files, still had 38 wrong, so the translation gained refusals for those too. Because they were looked at, that split is marked "tuned on test".
- Refusals are coarse: some queries that happened to be fine are now declined.

The full reference has the pinned commit, the file checksums, the recorded scores and the list of what was found. Run it with `python tools/googlesql_results_eval.py`.
