# Making local execution match BigQuery more closely

[All simple guides](README.md) · [Full reference](../docs/bigquery-on-duckdb.md)

KumoSQL runs many result checks locally in DuckDB after translating BigQuery SQL. The engines sometimes interpret similar SQL differently. A DuckDB difference can therefore be a misleading claim about BigQuery unless that difference is handled.

## What the compatibility layer does

It sets BigQuery-style NULL ordering and UTC timestamps, fixes supported translation differences, and handles supported result representation differences.

For example, BigQuery NUMERIC keeps more decimal precision than DuckDB's default DECIMAL. BigQuery also rounds FLOAT64-to-INT64 halfway cases away from zero, while DuckDB rounds ties to even; the compatibility layer adjusts those casts. Week numbering, substring positions, NULL-sensitive functions, and array indexing also need care. A “safe” array access and one that should raise an error are different operations.

The layer applies to BigQuery-dialect execution paths, including counterexample replay, random checks, synthetic comparisons, and incremental simulation. Other input dialects retain their own execution handling.

## Picks that are not the same twice

Some queries may return any one value from a group (`ANY_VALUE`) or any one of several tied rows (`LIMIT`). BigQuery can choose differently from DuckDB, and even two copies of the same pick can differ in DuckDB. When a pair only differs through such a pick, the search no longer calls it different. It reruns the candidate with the pick guarded: if a group holds more than one value, or a `LIMIT` cuts through ties, the run fails and the pair stays unknown. A pick over a group that holds one value, such as a column the grouping already fixes, still counts.

## Checked against Google's expected rows

Google's GoogleSQL compliance tests list the rows each query must return. [One eval](evals/googlesql-expected-results.md) runs those queries through this layer and compares. The differences it found are now fixed (a millisecond count that included whole seconds, a backslash in a `LIKE` pattern, `SPLIT` with a NULL delimiter) or refused: when the layer cannot tell what BigQuery would return, for example a string escape or a time zone written as an offset, the query fails and the pair stays unknown.

## Keeping the guards fast

Each guard wraps an operation, such as a division, and runs inside the query. When guards were nested (a division inside a division), the local engine used to copy the inner part several times, so a few queries became much slower to plan and could run out of time in the evals. Each guard now looks at its inputs once. The answers do not change; the queries just plan quickly again.

## Read the limits

Compatibility fixes do not turn DuckDB into BigQuery. Unsupported or unfaithful shapes must not become accepted BigQuery counterexamples just because local results differ.

The full reference lists the fixes, native BigQuery checks, and unsupported cases. Read it when a result depends on dates, arrays, numeric precision, errors, or engine-specific functions. [Provers](provers.md) explains why a confirmed execution difference and an equivalence proof are different evidence.
