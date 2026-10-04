# Making local execution match BigQuery more closely

[All simple guides](README.md) · [Full reference](../docs/bigquery-on-duckdb.md)

KumoSQL runs many result checks locally in DuckDB after translating BigQuery SQL. The engines sometimes interpret similar SQL differently. A DuckDB difference can therefore be a misleading claim about BigQuery unless that difference is handled.

## What the compatibility layer does

It sets BigQuery-style NULL ordering and UTC timestamps, fixes supported translation differences, and handles supported result representation differences.

For example, BigQuery NUMERIC keeps more decimal precision than DuckDB's default DECIMAL. Week numbering, substring positions, NULL-sensitive functions, and array indexing also need care. A “safe” array access and one that should raise an error are different operations.

The layer applies to BigQuery-dialect execution paths, including counterexample replay, random checks, synthetic comparisons, and incremental simulation. Other input dialects retain their own execution handling.

## Read the limits

Compatibility fixes do not turn DuckDB into BigQuery. Unsupported or unfaithful shapes must not become accepted BigQuery counterexamples just because local results differ.

The full reference lists the fixes, native BigQuery checks, and unsupported cases. Read it when a result depends on dates, arrays, numeric precision, errors, or engine-specific functions. [Provers](provers.md) explains why a confirmed execution difference and an equivalence proof are different evidence.

The standalone counterexample search also checks arbitrary aggregate choices. Shuffling table rows may still produce the same pick because a hash group chooses its own order. Before reporting a difference, the search therefore checks that every `ANY_VALUE`, `FIRST` or `LAST` group has only one possible value. Groups containing both NULL and a value, and window or decorated picks, are declined conservatively. The reported results still come from the original queries. Synthetic tests cover the guard; checks for ties under an ordered row limit remain unfinished. See the [full reference](../docs/bigquery-on-duckdb.md).
