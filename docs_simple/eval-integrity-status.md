# What came of the audit

[All simple guides](README.md) · [Full reference](../docs/eval-integrity-status.md)

The [outside audit](eval-integrity-audit-2026-10-02.md) listed places where a passing check showed less than it seemed to. Every finding was re-run on the current code before anything was changed, because the code had moved on since the audit.

For example, one finding was that the query lifter could name a helper query after a real table the query already read, and the checker then agreed the two queries matched when they returned different rows. The lifter now picks names that no table or other helper query uses, and the checker declines any query that reads a table named like one of its own generated helpers.

The fixes that are in place:

- **Name clashes.** Generated names never collide with real tables. A name inside a helper query's own definition refers to a real table, not to the helper being defined, and is left alone.
- **Invalid SQL.** The lifter no longer writes a `WITH` before `UPDATE` or `DELETE`, which BigQuery rejects.
- **The sample-query check.** Each sample has a written expectation, so a sample that cannot run, or that the lifter leaves unchanged, no longer counts as a success. A sample earns credit only when it was actually run against a declared set of tables and worked; a check with no table list earns none.
- **Broken input.** A rewrite that leaves a cut-off query untouched is no longer labelled trusted, and a rewrite of a query BigQuery would reject (a column its subquery does not have, or `HAVING` with no grouping) is no longer called proven. The check only catches those two cases.
- **Random testing.** The pairs are fixed before any proof is tried, and a proof of a query against an identical copy of itself no longer counts toward the floors.

Limits: a few things were found but left to the evals that own them. The full page lists each finding and its state. No benchmark score changed because of these fixes.
