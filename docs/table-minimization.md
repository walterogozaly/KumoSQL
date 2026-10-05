# Table minimization: the simplest SQL that keeps the protected tables

[Plain-language version](../docs_simple/table-minimization.md)

Give KumoSQL any number of table definitions (say tables 1 through 20, each one `SELECT`) and say which of them are **protected**. It returns the set of tables with the lowest total complexity it can find in which every protected table still exists under the same name, with the same output columns in the same order, and is **proved** to return the same rows as before. Unprotected tables may be dropped, folded into their readers, merged into an equal table, pruned of columns nobody reads, or rewritten.

Agreement on test data never counts: every step is kept only when KumoSQL's pipeline prover proves each protected table equal to the original. A step it cannot prove is rejected, so the worst answer is the input unchanged.

## Use it

- **Python:** `kumosql.table_minimizer.minimize_tables(tables, protected, sources=None, dialect="bigquery", timeout_ms=5000, max_seconds=120)` returns a `TableMinimization`: `.tables` (name to SQL, in the input dialect), `.proofs` (per protected table: `unchanged` or `proved`, with the prover's assumptions), `.score` and `.original_score`, `.moves`, `.removed`, `.changed`, `.added` (new shared tables), `.rejected_moves`, `.tried`, `.rejected`, `.stopped` and `.to_json()`. Bad input raises `MinimizationError` (an unknown protected table, a cycle, a name used for both a source and a table).
- **Options** (all off by default; [project reduction](project-reduction.md) uses them): `factor=True` moves a query repeated in several places into one new table (below); `drop_only=True` restricts moves to dropping unused tables and pruning safely unused output columns, with no folding, merging, simplification or factoring; `fixed={name: columns or None}` keeps tables exactly as given, like unreadable ones, and keeps every table they read proved unchanged; `incremental=[name, ...]` does the same for tables whose rows depend on earlier runs (a SELECT cannot say so, nor give a `uniqueKey`, an `updatePartitionFilter` or a schedule), so such a table is never folded, merged, pruned or rewritten; `checked=[...]` names unprotected tables that may be removed but, while they survive, must stay proved equal on the columns they keep; `keep_columns={name: [...]}` columns a table never loses to pruning; `lower_score_only=True` takes a rewrite only when it lowers the score (not on a tie broken by shorter SQL).
- **Checking a proposal:** `verify_tables(original, candidate, protected, sources=None, dialect="bigquery", fixed=(), incremental=(), checked=())` returns, per protected table (and per `checked` table the candidate keeps), `unchanged`, `proved` (with assumptions), `missing` or `unknown` (with the reason). The candidate may add tables of its own; a `fixed` or `incremental` table it changes or drops makes every result `unknown`. The eval harness can use it to tell a proved answer from one that only agrees on data.
- **Eval:** on the [table-minimization eval](evals/table-minimization.md) it simplifies 255 of 270 dev cases (63 of 64 held out) with 0 wrong, reaching 80% of the reference reduction (`python tools/minimization_bench.py`).
- **Eval harness:** `kumosql.table_minimizer:minimize_case` takes a case in the shared table-minimization case format (`{id, dialect, sources, tables, protected}`, and optionally `incremental`) and returns `{name: SQL}`.
- **CLI:** `python -m kumosql minimize-tables CASE.json` (`-` reads stdin; `--max-seconds`, `--timeout-ms`). The file holds `tables`, `protected` and optionally `sources`, `dialect` and `incremental` (tables kept as written). Output is the JSON of `to_json()`; progress goes to stderr.

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
| factor | With `factor=True`: a query written in at least two places (a derived table in `FROM` or `JOIN`, a top-level CTE, or a whole table) with the same canonical form (table aliases and column qualifiers do not matter) and output names, which reads only tables whose columns are known, is moved into one new table (named after the CTE it replaces when there is one), or replaced by an existing table that already returns it. |

The search is greedy: at each step every move is scored, the cheapest are proved first, and the first one proved is taken. A table that could merge into several equal-looking tables tries an exact copy first. A second start folds every unprotected table into its readers at once and then continues greedily; the cheaper proved result wins. A shared intermediate that would be copied into several readers stays when copying costs more.

Every state is checked against the **original** tables (never against the previous step), so proofs do not chain. The check is the Refactor page's: `refactor.check_observable`, built on `pipeline_equivalence.prove_models` (layer lemmas, then everything inlined), with the output column names compared as well, since the prover compares columns by position. Saved equivalences from the app are not used. Proofs are cached per protected table and the SQL it depends on.

A table whose rows can change from one evaluation to the next is never folded, merged, pruned or rewritten, and readers of it are not rewritten either: random and time functions (`RAND`, `GENERATE_UUID`, `CURRENT_*`), `ANY_VALUE`, `ARRAY_AGG`/`STRING_AGG`, `LIMIT`, sampling, `_TABLE_SUFFIX` and `FOR SYSTEM_TIME AS OF`. Copying such a table into two readers would evaluate it twice. It can still be dropped when nothing reads it.

`tests/test_table_minimizer.py` carries seven tempting rewrites that each change a protected table (a filter pushed into a protected stage, shared siblings built with `UNION ALL`, a `LEFT JOIN` ON predicate taken as a filter, `DISTINCT` on a superset, `AVG` as a sum over `COUNT(*)`, a `NULL` join key kept, a global `COUNT(*)` turned into a grouped one). For each, DuckDB shows the difference on a witness database, `verify_tables` refuses it, and the minimizer's own answer agrees with the original on the witness and on random databases.

Names are handled with care. A change whose SQL, written back with the names as given, has a `WITH` table named like a table it reads is rejected, because the `WITH` table would capture the reference. When inlining for a proof puts two `WITH` tables of one name in one query (dbt-style tables that all start `WITH source AS (...), renamed AS (...)`), each gets a name of its own, with its references, following `WITH` scopes; where the scopes are not plain (a recursive `WITH`, or one name defined twice in one `WITH`) the change is not trusted to the prover. Tables of one name in two datasets (`a.t`, `b.t`) are kept apart.

## Names and scripts

Four rules keep a proof about the input's own tables (`kumosql.minimizer_identity`, regression tests in `tests/test_table_minimizer.py`; audit #538):

- **A script is not a query.** Text with more than one statement, `CALL`, DDL or DML is returned exactly as given, never read as its first `SELECT`. Every table whose name it mentions is kept and proved unchanged like a protected one, so a later statement's reads and effects cannot be dropped. A protected script is reported as `unchanged` (in `verify_tables`: `missing`, `unchanged` or `unknown` when its text differs), never left out of the result.
- **Identity keeps its case.** For `dialect="bigquery"` names are case-sensitive (the BigQuery default): `p.D.stage` and `p.d.stage` are two tables, and a read of one is never bound to the other. Names that differ only by case are *ambiguous*, because a dataset configured for case-insensitive table names would make them one: such a table, and every table that reads one, is kept exactly as written, and a proof that rests on tables of another name carries the assumption that names are case-sensitive. Other dialects keep folding unquoted names to lower case.
- **Internal names cannot collide with real ones.** A bare or two-part name is searched under an internal catalog that occurs nowhere in the input, so a table the input names `kumo_min.tables.t` can never replace a bare `t`, and the original score counts every input table.
- **A `WITH` table hides a read only inside its own query.** A one-part read of `stage` is a read of the `WITH` table only where that table is in scope (the query under the `WITH`, and the `WITH` tables listed after it). A nested `WITH stage AS (...)` in a subquery does not hide a physical read of `stage` elsewhere in the statement (`ast_utils.is_cte_reference`, used by `refactor._table_nodes`).

Before an answer is returned it is checked once more against the input's own names: no table of it may read a table the answer removed or a name the input never used. If one does, the input is the answer.

## Limits

- Dataset case policy: with a dataset configured for case-insensitive table names, names that differ only by case are one table. The minimizer then refuses to change them (above); it does not look the policy up.
- `minimize_tables` reads SELECT bodies only, so it cannot tell that a model is incremental, or know its `uniqueKey`, `updatePartitionFilter` or schedule. Name incremental tables in `incremental=` (or an `incremental` list in a `minimize-tables` case file) and they stay exactly as written, with every table they read kept and proved unchanged; the minimizer never rewrites the transition itself. [Project reduction](project-reduction.md) does this for you from the Dataform config. Nothing detects an incremental table you leave unnamed.
- Greedy search: it finds a good answer, not always the optimum. `max_seconds` and `max_steps` bound it; `.stopped` says which one stopped it.
- Shared logic is moved into a new table only with `factor=True`, and only for queries repeated with the same canonical form; it does not rewrite protected tables beyond the simplifier's forms.
- Proofs are as sound as KumoSQL's prover: "0 wrong" on the eval means no answer differed on the DuckDB check databases, and known false proofs that are still open in the prover apply here too.
- Proof coverage is the prover's: steps it cannot prove (some outer-join chains, AVG rebuilt across a rollup) are rejected and listed in `rejected_moves` with the reason.
- The prover reads BigQuery SQL. Another `dialect` is transpiled to BigQuery with sqlglot to search and back for the answer.
