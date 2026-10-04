# What came of the audit

[All simple guides](README.md) · [Full reference](../docs/eval-integrity-status.md)

The [outside audit](eval-integrity-audit-2026-10-02.md) listed places where a passing check showed less than it seemed to. Every finding was re-run on the current code before anything was changed, because the code had moved on since the audit.

For example, one finding was that the query lifter could name a helper query after a real table the query already read, and the checker then agreed the two queries matched when they returned different rows. The lifter now picks names that no table or other helper query uses, and the checker declines any query that reads a table named like one of its own generated helpers.

The fixes that are in place:

- **Name clashes.** Generated names never collide with real tables. A name inside a helper query's own definition refers to a real table, not to the helper being defined, and is left alone.
- **Invalid SQL.** The lifter no longer writes a `WITH` before `UPDATE` or `DELETE`, which BigQuery rejects.
- **The sample-query check.** Each sample has a written expectation, so a sample that cannot run, or that the lifter leaves unchanged, no longer counts as a success. A sample earns credit only when it was actually run against a declared set of tables and worked; a check with no table list earns none.
- **Comparing query results.** When two queries are run and their rows compared, a true/false value is no longer the same as the number 1, a not-a-number value is no longer the same as the text `NaN`, and a struct is no longer the same as a list of pairs. Comparing floats exactly is now an option, and each result says which setting it used. The old rule of treating true as 1 is still available on request for test sets written for databases that have no true/false type. A test that checks proofs against real execution also fails if it ends up checking almost nothing.
- **Scoreboard bookkeeping.** A row whose outcome counts do not add up to its size must now say what the counts measure, and the held-out cases are no longer printed by default.
- **Random testing.** The pairs are fixed before any proof is tried, and a proof of a query against an identical copy of itself no longer counts toward the floors.

Limits: two smaller follow-ups are listed on the full page and not done (a parse-recovered input that comes back unchanged is still labelled "unchanged", and a few inputs that parse but would not run can still be proved). The full page lists each finding and its state. No benchmark score changed because of these fixes.
