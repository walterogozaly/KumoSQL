# More sources for SQL tests

[All simple guides](README.md) · [Full research reference](../docs/additional-public-sql-sources.md)

This is a second set of research leads for expanding KumoSQL's evaluations. It adds to [the first search](public-sql-evaluation-sources.md).

The research inspected source material; it did not import or execute the proposed suites. Use [the integration inventory](evals/public-sources.md) to find what has actually been added.

## What kinds of additions are useful?

- SQLFluff structural fixtures and Trino paired tests provide concrete before/after SQL.
- Engine bug reports provide small databases that expose unsafe optimizer changes.
- Mozilla BigQuery tests provide native GoogleSQL examples and expected outputs.
- Arcwise corrections supply intentional repairs, which can be negative equivalence tests.
- Complete sample databases and generators provide more varied tables, constraints, and data.

## Keep the evidence attached

A repaired query often intentionally returns different results. An expected rewrite string is not a proof. A fixed-database comparison establishes only what happened on that data. A schema plus generator is different from a complete fixed database.

Record the original SQL, dialect, version, schema, comparison rules, license, and any adaptation. Overlapping sources should not be counted as independent tests.

The full research page contains specific artifacts, links, access restrictions, verified revisions, and proposed extraction batches. Recommendations and availability describe the research snapshot, so check the inventory and source before starting a new import.
