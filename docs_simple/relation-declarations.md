# Saved query relationships

You can prepare a local record that says two SQL queries represent the same rows and columns. The record keeps both queries, their output columns, a preferred query, where the claim applies, how it was supported, and where it came from.

Saving this record does **not** check that the queries really match. It does not run a warehouse query or change KumoSQL's proof and rewrite features.

## A small example

Suppose `table_y` has `col_id`, `col_old` and `keep`, while `table_x` has `col_id`, `col_new` and `keep`. You can prepare these two sides:

```sql
SELECT * EXCEPT (col_old), col_old AS col_new FROM table_y
```

```sql
SELECT keep, col_id, col_new FROM table_x
```

KumoSQL can expand the known columns and match them by name even though their order differs. You must provide each table's column types. KumoSQL compares those types exactly after removing case and extra spaces; it does not guess that one type can be converted to another.

In Python, call `prepare(left_sql, right_sql, schemas, preferred_side="right")` to build a record. Pass it an `Evidence` and `Scope` when you want to record how the claim was checked and where it applies. Call `declare(...)` to save the record in KumoSQL's local data folder. The full guide has the exact API and all metadata choices: [query-to-query relation declarations](../docs/relation-declarations.md).

## What this supports today

Known-table stars and direct column selections can be resolved. Filters and joins remain in the saved SQL. Missing or repeated output names, unknown star columns, ambiguous columns and types that do not match are reported as errors.

Computed outputs such as `COUNT(*)`, CTEs and derived tables are not resolved yet. This record is not proof that the two sides match, and no existing proof or rewrite feature reads it. An assertion, snapshot label or freshness scope is information about the user's premise; it does not verify that premise. The full guide also lists the current limits and how revocation works.
