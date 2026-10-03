# Table minimization: the simplest SQL that keeps the protected tables

Give KumoSQL any number of table definitions (say tables 1 through 20, each one `SELECT`) and say which of them are **protected**. It returns the set of tables with the lowest total complexity it can find in which every protected table still exists under the same name, with the same output columns in the same order, and is **proved** to return the same rows as before. Unprotected tables may be dropped, folded into their readers, merged into an equal table, pruned of columns nobody reads, or rewritten.

Agreement on test data never counts: every step is kept only when KumoSQL's pipeline prover proves each protected table equal to the original. A step it cannot prove is rejected, so the worst answer is the input unchanged.

## Use it

- **Python:** `kumosql.table_minimizer.minimize_tables(tables, protected, sources=None, dialect="bigquery", timeout_ms=5000, max_seconds=120)` returns a `TableMinimization`: `.tables` (name to SQL, in the input dialect), `.proofs` (per protected table: `unchanged` or `proved`, with the prover's assumptions), `.score` and `.original_score`, `.moves`, `.removed`, `.changed`, `.rejected_moves`, `.stopped` and `.to_json()`. Bad input raises `MinimizationError` (an unknown protected table, a cycle, a name used for both a source and a table).
- **Checking a proposal:** `verify_tables(original, candidate, protected, sources=None, dialect="bigquery")` returns, per protected table, `unchanged`, `proved` (with assumptions), `missing` or `unknown` (with the reason). The candidate may add tables of its own. The eval harness can use it to tell a proved answer from one that only agrees on data.
- **Eval harness:** `kumosql.table_minimizer:minimize_case` takes a case in the shared table-minimization case format (`{id, dialect, sources, tables, protected}`) and returns `{name: SQL}`.
- **CLI:** `python -m kumosql minimize-tables CASE.json` (`-` reads stdin; `--max-seconds`, `--timeout-ms`). The file holds `tables`, `protected` and optionally `sources` and `dialect`. Output is the JSON of `to_json()`; progress goes to stderr.

Queries name sources and other tables by name (bare, `dataset.table` or `project.dataset.table`). A name a query reads that is not a table is a source. `sources` describes them as `{name: {"columns": {column: type}, "key": [...], "not_null": [...]}}`, as `{column: type}` or as a list of columns. Columns let the prover expand `SELECT *`; a key and NOT NULL columns are facts it may assume (they are listed in the proof's assumptions).

## Complexity

The score is the one the eval cases use: the repo's sqlfluff measure (`kumosql.formatting.complexity`, the weighted count of joins, CTEs, subqueries, set operations, CASE, window functions, AND/OR and SELECT nesting from the sqlfluff parse tree) summed over the tables, **plus one per table**. sqlfluff has no single complexity number of its own; this is KumoSQL's score over sqlfluff's parse tree, and the table term makes dropping a `SELECT *` pass-through count. `table_minimizer.pipeline_score(tables)` computes it. Ties go to the shorter SQL.

## How it searches

Moves, each scored on the whole set of tables:

| Move | What it does |
| --- | --- |
| drop | An unprotected table nothing reads is removed. |
| fold | An unprotected table is written into each reader as a derived table, then each reader is simplified (`kumosql.sql_simplify`: merge the derived table's filters and projections into the outer query, drop unused CTEs, and so on). A fold alone raises the score; folded and simplified it usually lowers it. |
| merge | An unprotected table that returns the same columns from the same tables as another is replaced by it in its readers. |
| prune | Columns of an unprotected table that no reader mentions are removed (never under `DISTINCT` or a star read). |
| simplify | One table's SQL is replaced by a simpler form of itself. |

The search is greedy: at each step every move is scored, the cheapest are proved first, and the first one proved is taken. A second start folds every unprotected table into its readers at once and then continues greedily; the cheaper proved result wins. A shared intermediate that would be copied into several readers stays when copying costs more.

Every state is checked against the **original** tables (never against the previous step), so proofs do not chain. The check is the Refactor page's: `refactor.check_observable`, built on `pipeline_equivalence.prove_models` (layer lemmas, then everything inlined), with the output column names compared as well, since the prover compares columns by position. Saved equivalences from the app are not used. Proofs are cached per protected table and the SQL it depends on.

A table whose rows can change from one evaluation to the next is never folded, merged, pruned or rewritten, and readers of it are not rewritten either: random and time functions (`RAND`, `GENERATE_UUID`, `CURRENT_*`), `ANY_VALUE`, `ARRAY_AGG`/`STRING_AGG`, `LIMIT`, sampling, `_TABLE_SUFFIX` and `FOR SYSTEM_TIME AS OF`. Copying such a table into two readers would evaluate it twice. It can still be dropped when nothing reads it.

`tests/test_table_minimizer.py` carries seven tempting rewrites that each change a protected table (a filter pushed into a protected stage, shared siblings built with `UNION ALL`, a `LEFT JOIN` ON predicate taken as a filter, `DISTINCT` on a superset, `AVG` as a sum over `COUNT(*)`, a `NULL` join key kept, a global `COUNT(*)` turned into a grouped one). For each, DuckDB shows the difference on a witness database, `verify_tables` refuses it, and the minimizer's own answer agrees with the original on the witness and on random databases.

Two name traps are refused outright. A change whose SQL, written back with the names as given, has a `WITH` table named like a table it reads is rejected, because the `WITH` table would capture the reference. A change whose tables, once inlined, put two `WITH` tables of one name in one query is not trusted to the prover. Tables of one name in two datasets (`a.t`, `b.t`) are kept apart.

A table that is not a single readable query (a script, `CALL`, DDL) is returned exactly as given, and every table it reads is kept and proved unchanged like a protected one.

## Limits

- Greedy search: it finds a good answer, not always the optimum. `max_seconds` and `max_steps` bound it; `.stopped` says which one stopped it.
- It does not yet extract shared logic into a new table, and it does not rewrite protected tables beyond the simplifier's forms.
- Proofs are as sound as KumoSQL's prover: "0 wrong" on the eval means no answer differed on the DuckDB check databases, and known false proofs that are still open in the prover apply here too.
- Proof coverage is the prover's: steps it cannot prove (some outer-join chains, AVG rebuilt across a rollup) are rejected and listed in `rejected_moves` with the reason.
- The prover reads BigQuery SQL. Another `dialect` is transpiled to BigQuery with sqlglot to search and back for the answer.
