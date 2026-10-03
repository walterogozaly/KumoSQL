# Finding public SQL test material

[All simple guides](README.md) · [Full research reference](../docs/public-sql-evaluation-sources.md)

This research page proposes sources for broader SQL tests: query pairs, engine fixtures, complete databases, and real BigQuery or Dataform projects.

It is a source search and import plan. The external suites were not executed during the search. [The integration inventory](evals/public-sources.md) records what KumoSQL actually covers.

## Match a source to a question

| Source type | Useful for |
| --- | --- |
| Before/after query pairs | Testing equivalence and unsafe changes |
| Expected-result fixtures | Checking behavior on known data |
| Complete small databases | Multi-table workloads with real keys and NULLs |
| Data generators | Repeatable tests at different sizes |
| Native SQLX projects | Dependency and protected-text checks |

Keep original and adapted SQL separate. A dialect conversion can change meaning, and a performance recommendation may depend on data guarantees or deliberately change the task.

## Build a reproducible import

Pin source revisions and hashes, retain licenses and case identifiers, and record schemas, constraints, expected relationships, and comparison rules. A bag comparison preserves duplicate counts; an unordered set comparison does not.

Start with small attributed cases and complete small databases, then add slower or credential-dependent runs. Report unsupported cases, timeouts, and no-ops alongside successes.

The full reference includes concrete source links, proposed manifests, database-loader requirements, and test lanes. [Additional sources](additional-public-sql-sources.md) extends the search without treating each related corpus as independent evidence.
