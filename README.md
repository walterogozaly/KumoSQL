# BigQuery SQL Tools

Small, deterministic transformations for BigQuery SQL that are trusted by tests rather than by an LLM at runtime.

## Local browser UI

On Windows, install into a user-owned virtual environment, then start the local editor:

```powershell
py -3.11 -m venv "$env:LOCALAPPDATA\bq-sql-tools"
& "$env:LOCALAPPDATA\bq-sql-tools\Scripts\python.exe" -m pip install .
& "$env:LOCALAPPDATA\bq-sql-tools\Scripts\bq-sql-tools-ui.exe"
```

Open the URL printed by the command if your browser does not open automatically. Paste BigQuery SQL or Dataform SQLX on the left, choose one or more transformations, and review the proposed SQL and verification report on the right. The editor updates after a short pause while typing or when you change a selected rule. You can also use **Transform SQL** or Ctrl+Enter. Rules run in the order displayed, and output that cannot be verified remains visible with a review warning.

The server listens only on `127.0.0.1`, uses the existing Python package, and does not send pasted SQL to an external service. Use `bq-sql-tools-ui --no-browser` to start without opening a browser, or `--port 8766` to choose another local port. Stop it with Ctrl+C. No UI-specific dependency is required.

## Rewrite rules

Each transformation is a rule in a registry. A rule only says how to rewrite one parsed statement; a shared driver handles SQLX blocks and `${...}` interpolations, strict parsing with a visible recovery fallback, formatting, byte-for-byte no-ops, and CTE dependency checks.

| Rule | What it does |
| --- | --- |
| `lift_subqueries` | Lifts FROM/JOIN subqueries into named top-level CTEs (details below) |
| `inline_single_use_ctes` | Replaces each root CTE that is referenced exactly once with an inline subquery |
| `remove_trivial_predicates` | Removes always-true filters: `WHERE 1 = 1`, `AND TRUE`, `OR FALSE`, and other literal-only comparisons |
| `remove_redundant_parentheses` | Removes parentheses that cannot change how an expression parses |
| `deduplicate_ctes` | Points references to a root CTE at an earlier CTE with an identical body, then drops the duplicate |
| `remove_unused_ctes` | Removes root CTEs that nothing references |

`apply_rule` and `apply_rules` run rules and check every changed output against its input with the conservative equivalence prover. The result's `verification.status` is `unchanged`, `proven`, or `unproven`; an unproven output is still returned, with the reasons in `verification.details`, and `result.success` is false.

```python
from bq_sql_tools import apply_rules

result = apply_rules(["inline_single_use_ctes"], sql_text)
if not result.success:
    print(result.verification.status, result.verification.details)
print(result.sql)
```

Verification is per statement. For `CREATE ... AS` and `INSERT ... SELECT`, the text around the query must be unchanged and the queries must be proven equivalent; any change to a final `ORDER BY` is unproven. Other statements must render identically. For SQLX, config/js/operations blocks must be identical and each interpolation is treated as an opaque fragment identified by its text.

`inline_single_use_ctes` skips recursive WITH clauses, queries with nested WITH scopes, CTEs with column aliases, references that differ from the CTE name only in case, and references with anything beyond an alias (such as `FOR SYSTEM_TIME`).

The cleanup rules only use rewrites that hold in SQL's three-valued logic. `remove_trivial_predicates` drops `TRUE` from `AND` and `FALSE` from `OR` inside WHERE, HAVING, QUALIFY and JOIN conditions, but never applies `x AND FALSE` or `x OR TRUE`, which would discard `x` and any error it raises. It keeps `ON TRUE`, keeps `HAVING TRUE` without a GROUP BY, and folds numeric comparisons only between INT64 literals or identical literals. `remove_redundant_parentheses` keeps parentheses around an unaliased projection (BigQuery names the column after it), around a field access like `(a).b`, and around `AND` inside `OR`. `deduplicate_ctes` skips nondeterministic bodies, bodies with LIMIT, and merges that would repeat a relation name in one FROM clause.

To add a rule, subclass `RewriteRule`, set `name` and `summary`, implement `rewrite_statement(statement, index)` to edit the statement in place and return `(change_count, diagnostics)`, and decorate the class with `@register_rule`.

```powershell
rewrite-sql input.sqlx --rule inline_single_use_ctes --output output.sqlx
```

`rewrite-sql` exits 2 when a rule fails and 3 when the output is not proven equivalent (pass `--allow-unproven` to accept it).

## First goal: subquery lifting

`bq_sql_tools.lift_subqueries()` promotes every relational subquery used in a `FROM` or `JOIN` clause into a uniquely named top-level CTE. It accepts BigQuery SQL and Dataform SQLX. For SQLX, `config`, `js`, `pre_operations`, and `post_operations` blocks are preserved, while `${...}` interpolations are masked during parsing and restored afterward.

Scalar, `EXISTS`, and correlated predicate subqueries are intentionally left in place because changing those into CTEs can change query semantics. The result includes diagnostics, and an unrecoverable parse or transform error is never reported as success.

Existing CTE dependencies are respected: a lift from inside an existing CTE is placed immediately before that CTE, while a lift from the main query is appended after the existing CTEs. The lifter supports the `WITH` AST slot used by both older and newer supported `sqlglot` releases, checks for undefined or forward CTE references, and uses four-space formatting for transformed SQL. If there is nothing to lift, the input is returned byte-for-byte unchanged.

For valid-but-unsupported BigQuery syntax, the tool may use `sqlglot` recovery mode; those rows still report a `recovered_parse` diagnostic so the exception is visible to reviewers.

```python
from bq_sql_tools import lift_subqueries

result = lift_subqueries(sql_text)
if not result.success:
    raise RuntimeError(result.diagnostics)
print(result.sql)
```

SQLX example:

```python
result = lift_subqueries('''config { type: "table" }
SELECT *
FROM (SELECT id FROM ${ref("customers")}) AS c''')
assert result.success
```

## Test the supplied SQL workbook CSV

The repository includes a sanitized, generic copy at `tests/fixtures/generic_sql_workbooks.csv`. It preserves the eight-column schema, all 621 rows, SQL statement structure, and repeated references while removing project-specific identifiers and metadata. The full fixture test uses:

```powershell
pytest -m slow tests/test_workbook_fixture.py
```

Override its path with `BQ_SQL_TOOLS_TEST_CSV` to test another CSV, including the original private export. To regenerate the checked-in fixture from the private source:

```powershell
python tools/sanitize_fixture.py private.csv tests/fixtures/generic_sql_workbooks.csv
```

The test reads large CSV fields, checks all 621 workbook rows, and fails unless every row has zero remaining relational subqueries and no fatal diagnostics. A separate unit test checks the generic fixture for source metadata leaks and schema drift.

The normal unit suite is:

```powershell
pytest
```

## Conservative SQL equivalence

`bq_sql_tools.prove_equivalent(left_sql, right_sql)` returns `proven_equivalent` only when both inputs are strict, single-query BigQuery statements whose normalized ASTs match after relational subquery lifting. By default it compares result bags, so unspecified row order is ignored.

Root CTEs are renamed by position after being put in a canonical dependency order, so CTE order alone does not block a proof; reordering is skipped for recursive WITH, forward references, or names that differ only in case.

Before comparing, the prover also removes grouping parentheses, flattens `AND`/`OR` chains, removes `TRUE` from `AND` and `FALSE` from `OR` in filter and join conditions, drops `WHERE TRUE`, merges root CTEs with identical bodies, and drops unreferenced CTEs. These normalizations are written separately from the cleanup rules, and a three-valued-logic test evaluates both against the original predicates.

It refuses to prove queries containing volatile values, windows, tie-sensitive aggregates, `TABLESAMPLE`, or any `LIMIT`/`OFFSET`. Structural differences are reported as `not_proven`, never as a proof of inequivalence. This is intentional: false negatives are acceptable; false positives are not.

For audit or optional execution, `result.verifier_sql` contains a BigQuery query that counts JSON-encoded result rows on each side and compares their multiplicities with a full outer join. The verifier is an execution artifact and does not override the static safety checks.

```python
from bq_sql_tools import prove_equivalent

result = prove_equivalent(
    "SELECT id FROM (SELECT id FROM `p.d.customers`) AS c",
    "WITH base AS (SELECT id FROM `p.d.customers`) SELECT id FROM base AS c",
)
assert result.proven
```

CLI usage:

```powershell
prove-sql-equivalent left.sql right.sql --verifier-sql verify.sql
```

## Result equivalence on synthetic data

The static prover only accepts rewrites whose normalized ASTs match. To test rewrites it cannot prove, `bq_sql_tools.check_result_equivalence(left_sql, right_sql, schema)` runs both sides against the same deterministic synthetic tables in a local DuckDB engine (BigQuery SQL is translated with `sqlglot`) and compares the results as multisets, including column names.

- Seed 0 is always empty tables; other seeds include NULLs and duplicate rows, drawn from small value domains so joins and groups collide.
- Every run gets a fresh in-memory connection. Tables written by a script (`CREATE TABLE ... AS`, `INSERT`) are renamed to run-unique local names, and the final written table is compared when the script does not end in a query.
- Dataform SQLX is supported: blocks are dropped and `${ref(...)}` becomes a table name. Any other interpolation, unknown table, or execution failure is reported as `error`, never as equivalent.
- A mismatch returns `different` with the failing seed and the rows only one side produced. Agreement is evidence, not a proof.

```python
from bq_sql_tools import assert_result_equivalent, lift_subqueries

schema = {"p.d.orders": {"customer_id": "INT64", "amount": "FLOAT64"}}
original = "SELECT * FROM (SELECT customer_id, SUM(amount) AS total FROM `p.d.orders` GROUP BY 1) AS t"
assert_result_equivalent(original, lift_subqueries(original).sql, schema)
```

Install the engine with `pip install -e ".[execution]"` (it is included in `.[dev]`). `tests/test_result_equivalence.py` runs every lifted query in its corpus through the harness and checks that deliberately broken rewrites are caught.

## SMT equivalence prover

`bq_sql_tools.prove_equivalent_smt(left_sql, right_sql)` proves semantic equivalence with Z3 instead of comparing syntax, so it accepts rewrites such as filter pushdown into a CTE, join reordering, `DISTINCT` to `GROUP BY`, `CASE` to `IF`, redundant predicates, and self-join elimination under `DISTINCT`. Install it with the optional extra: `pip install -e ".[smt]"`.

It models inner and cross joins, `WHERE`, derived tables and CTEs, `GROUP BY`/`HAVING` with `COUNT`, `SUM`, `MIN`, `MAX`, `AVG`, `COUNTIF`, `LOGICAL_AND`/`LOGICAL_OR`, `UNION ALL`/`UNION DISTINCT`, `SELECT DISTINCT`, and NULLs with three-valued logic. Other deterministic functions are uninterpreted: equal inputs give equal outputs. Anything else (outer joins, windows, `LIMIT`, predicate subqueries, nondeterministic functions) returns `not_proven`.

The result is one of:

- `proven_equivalent`: the two queries return the same bag of rows on every database, under the listed `assumptions` (no NaN, runtime errors not modeled, column types not compared).
- `not_equivalent`: `counterexample.tables` is a small database on which the queries return different rows (`left_rows`, `right_rows`). It is only reported when no uninterpreted function is involved.
- `not_proven`: outside the subset, or no proof was found.

```python
from bq_sql_tools import prove_equivalent_smt

result = prove_equivalent_smt(
    "SELECT id FROM (SELECT id, a FROM t WHERE a = 1) AS s WHERE id > 0",
    "WITH s AS (SELECT id, a FROM t WHERE id > 0) SELECT id FROM s WHERE a = 1",
)
assert result.proven
```

Pass `schema={"t": ["id", "a"]}` to enable `SELECT *` and unqualified columns in joins, and `exact_arithmetic=True` to reason about `+`, `-` and `*` exactly (right for INT64 and NUMERIC, not FLOAT64). The CLI prints JSON and exits 0 only on a proof:

```powershell
prove-sql-smt left.sql right.sql --schema schema.json
```

`tests/test_smt_fuzz.py` checks the prover against SQLite on random queries and databases: every proof must hold and every counterexample must separate the queries.

## Whole-pipeline analysis

`load_sqlx_project(root)` loads a Dataform project (`definitions/**/*.sqlx`, with `workflow_settings.yaml` or `dataform.json` defaults) or a plain folder of `.sql` files. `load_compiled_graph(path)` loads the JSON from `dataform compile --json`, which is the exact compiled SQL and is preferred when available.

The result is a `Pipeline` that qualifies every model in dependency order, so each model sees the output columns of the models it reads:

- `upstream`, `downstream`, `topological_order()`: the model graph, with cycles reported as diagnostics.
- `column_lineage()`, `upstream_columns(col)`, `downstream_columns(col)`: column-level lineage across models and CTEs, with transitive closure. This is the blast radius of changing a column.
- `dead_columns()`: output columns of intermediate models that nothing downstream reads anywhere (SELECT, WHERE, JOIN, GROUP BY, or inside a Dataform `${...}` expression). Terminal models count as pipeline outputs. "Dead" means not read inside the pipeline; a dashboard reading an intermediate table directly is invisible here. Results are withheld for a table when any reader could not be analysed (an unparseable model, or `SELECT *` over a source with unknown columns).
- `duplicate_selects()`: identical normalized SELECT subtrees in more than one place, such as the same CTE copied into several models. These are candidates for a shared model.
- `near_duplicate_selects(threshold=0.7)`: SELECTs that are similar but not identical, such as a CTE copied into several models where one copy gained a filter or a column. Each query level (nested CTEs and subqueries collapse to tokens) becomes a multiset of tree shingles; MinHash banding proposes pairs on large pipelines, exact Jaccard similarity confirms them, and each cluster is compared clause by clause with its centre. When the differences fit a shared model, the cluster carries `shared_sql`: `literal_parameters` (only constants differ; they become `@parameters`), `extra_filters` (extra WHERE conjuncts on a non-aggregating SELECT, applied downstream as each copy's `residual_filters`), `extra_columns` (the union of columns), or both of the last two. Anything else is `mixed`, with differences but no SQL. These are candidates to check with `prove_equivalent` or `check_rewrite`, not proven rewrites.

Source table columns come from `source_schema={"project.dataset.table": {"col": "TYPE"}}`. `fetch_table_schemas()` fills it from BigQuery with free dry runs.

```powershell
bq-pipeline-report path/to/dataform --source-schema sources.json --similarity 0.7 -o report.json
```

## Comparing pipeline outputs before and after a refactor

`plan_output_comparison(before, after=None, before_location=..., after_location=...)` plans a comparison of every table, view and incremental model's output across two builds of a pipeline, for example production against a development dataset built from the refactored code. It generates BigQuery SQL in three tiers, from cheapest to most detailed:

- `plan.compare_sql()`: one query that scans each table once and returns one row per model and column with a `matches` flag. Each side is fingerprinted as a row count, a whole-row checksum and a checksum per column, where a checksum is `SUM(FARM_FINGERPRINT(TO_JSON_STRING(value)))` in `BIGNUMERIC`. That ignores row order, counts duplicate rows, treats `NULL` as a value and cannot overflow. Whole rows are built with columns sorted by name, so reordering columns alone still matches.
- `plan.fingerprint_sql("before")`: the same fingerprints for one side, for when the original tables are overwritten by the refactored run. Save its rows, rebuild, run it for `"after"`, and compare with `compare_snapshots(before_rows, after_rows)`.
- `plan.drilldown_sql(model, keys=[...], columns=[...])`: the rows that differ for one model. With `keys`, each differing key is reported as `only_before`, `only_after`, `row_count_differs` or `changed`, with the names of the changed columns and both rows as JSON. Without keys it is a bag difference of whole rows.

`summarize_comparison(rows)` turns the comparison results into one `ModelDiff` per model. A mismatching column localises the difference; a whole-row mismatch where every column matches means values moved between rows.

Models are matched by target, and when `after` is a different pipeline only the output columns both sides share are compared; added, dropped or new models are diagnostics. Use `normalize={"amount": "ROUND({col}, 6)"}` for float noise, `ignore_columns=["loaded_at"]` for load timestamps, and `where={model: predicate}` to limit a scan to some partitions. A model whose columns are unknown (a `SELECT *` over a source without a schema) is compared on whole rows only.

`table_fingerprint_sql`, `compare_tables_sql` and `diff_rows_sql` do the same for any pair of tables.

```powershell
bq-compare-outputs compare path/to/dataform --source-schema sources.json --after-dataset-suffix _dev > compare.sql
bq query --use_legacy_sql=false --format=json < compare.sql > results.json
bq-compare-outputs summarize results.json
bq-compare-outputs drilldown path/to/dataform --after-dataset-suffix _dev --model proj.analytics.orders --keys order_id
```

## BigQuery dry-run check

`dry_run(sql, project)` asks BigQuery to plan a query without running it. That costs nothing and reads no data, but it checks names and types and returns the output schema and bytes that would be scanned. `check_rewrite(original, rewritten, project)` accepts a rewrite only when both queries plan and their output schemas match exactly (column order, type, mode and nested fields). That is a necessary check, not a proof of equivalence.

Credentials come from `BQ_ACCESS_TOKEN`, or a service account key in `GOOGLE_APPLICATION_CREDENTIALS_JSON` (contents) or `GOOGLE_APPLICATION_CREDENTIALS` (path) with `pip install '.[bigquery]'`. Read-only roles are enough: BigQuery Job User plus Data Viewer.

```powershell
bq-dry-run original.sql --rewritten rewritten.sql --project my-project
```

## CLI

```powershell
lift-subqueries input.sql --output output.sql --report
```
