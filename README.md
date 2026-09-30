# KumoSQL

KumoSQL helps you change BigQuery SQL and Dataform models with evidence instead of hope. It is built around three questions:

1. **What depends on this query, table, or column?** Pipeline analysis traces model and column lineage and shows the blast radius of a change.
2. **Where is the same work being done repeatedly?** Duplicate and near-duplicate SELECT detection finds logic that could be shared.
3. **Can a proposed change be shown to preserve behavior?** Rewrites are small, deterministic transformations, checked by static proof, SMT, synthetic-data comparison, and BigQuery dry runs. Tests decide what is trusted, not an LLM at runtime, and output that cannot be verified is flagged rather than reported as a success.

New here? See the [Getting Started guide](docs/getting-started.md) for installation, a Python example, and the browser UI.

## Local browser UI

On Windows, install into a user-owned virtual environment, then start the local editor:

```powershell
py -3.11 -m venv "$env:LOCALAPPDATA\kumosql"
& "$env:LOCALAPPDATA\kumosql\Scripts\python.exe" -m pip install .
& "$env:LOCALAPPDATA\kumosql\Scripts\kumosql-ui.exe"
```

Open the URL printed by the command if your browser does not open automatically. Turn on transformations in the **Pipeline** sidebar, paste BigQuery SQL or Dataform SQLX (or open or drop a `.sql`/`.sqlx` file) into **Original SQL**, and review the highlighted result, a line diff, and the per-step verification report. Use **GitHub** to browse the SQLX models in a public Dataform repository and open a file in the editor. Rules run top to bottom; drag them or use the arrows to change the order. The result updates after a short pause while typing, or use **Transform SQL** or Ctrl+Enter. Output that cannot be verified remains visible with a review warning. **Examples** loads sample SQL for each rule, and the result can be copied, downloaded, or sent back to the editor for another pass.

The server listens only on `127.0.0.1` and uses the existing Python package. Pasted SQL stays local. GitHub browsing contacts GitHub only when you connect a repository or open one of its files; it supports public Dataform repositories and is read-only. The **BigQuery browser** is a separate page that lists projects, datasets, tables, and table schemas using your Google Cloud credentials. It contacts BigQuery only when you open that page, select an item, or press **Refresh projects**; the SQL editor itself remains local. Use `kumosql-ui --no-browser` to start without opening a browser, or `--port 8766` to choose another local port. Stop it with Ctrl+C. No UI-specific dependency is required.

**Query graph**, **Cost** and **Change reports** are pages for the upcoming roadmap work: readers, change impact, and lineage; measured cost and savings; and semantic change reports. Until the issues behind them land, they show labeled sample data. [docs/ui-roadmap.md](docs/ui-roadmap.md) lists the issue and data shape behind each area.

To use the catalog with Google Cloud CLI credentials, install the BigQuery extra and sign in with Application Default Credentials:

```powershell
python -m pip install '.[bigquery]'
gcloud auth application-default login
```

The browser uses the active ADC identity and read-only BigQuery scope. It lists projects that identity can see; BigQuery permissions still control which datasets, tables, and schemas appear. `gcloud auth login` alone does not configure Application Default Credentials.

**Saved state.** UI preferences (theme, enabled rules and their order), named SQLFluff formatting configurations, and scopes are saved by the local server in one JSON file in the standard per-user data directory (`%APPDATA%\kumosql\state.json` on Windows, `~/Library/Application Support/kumosql/state.json` on macOS, `$XDG_DATA_HOME/kumosql/state.json` or `~/.local/share/kumosql/state.json` on Linux). Set `KUMOSQL_HOME` to use another directory. The CLI and Python API read the same file, so a scope saved in the UI works with `--scope`.

**Formatting and complexity.** The `format_sql` operation formats BigQuery SQL with [sqlfluff](https://sqlfluff.com) and is verified like every other rule. Open *Formatting preferences* in the sidebar to set keyword case, comma position, indentation, line length, and which sqlfluff rules to apply or exclude. Save multiple named configurations and switch between them; the active config is included in the server's persistent local state. The stats bar shows a structural complexity score before and after (hover for the breakdown). The score is a weighted sum of joins, CTEs, subqueries, set operations, `CASE` expressions, window functions, `AND`/`OR` predicates and SELECT nesting depth, banded low (<10), moderate (<25), high (<50) or very high. sqlfluff cannot parse Dataform SQLX, so those inputs are left unformatted and unscored. The same is available in Python: `format_sql(sql)` and `complexity(sql)`.

**Scopes.** A scope is a saved, named label of associations on any field: a `My Team` scope of authors, or a `My Team's Projects` scope of BigQuery projects. Create them under *Scopes* in the sidebar or from the shell:

```powershell
kumosql-scopes add "My Team" --field author ana@co.com bo@co.com
kumosql-scopes add "My Projects" --field project growth-*
kumosql-scopes list
kumosql-pipeline-report path/to/dataform --scope "My Projects"
```

Values match case-insensitively and a trailing `*` matches a prefix. A scope may constrain several fields, and a record must match all of them. In Python, `Scope.matches(record)` and `Scope.filter(records)` apply a scope to any dicts (job rows, model metadata). `Pipeline.report(scope=...)` limits the pipeline report to models whose `project`, `dataset`, `name` or `table` match; a scope on any other field (such as `author`) is rejected, and a scope that matches no models prints a warning. Duplicate groups are kept when any occurrence is in scope, so they can list out-of-scope occurrences as context.

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
| `format_sql` | Formats with sqlfluff using your saved formatting preferences (SQL only, not SQLX) |

`apply_rule` and `apply_rules` run rules and check every changed output against its input with the conservative equivalence prover. Each result has one `verification.status` and a `verification.checks` list with the individual evidence:

- `unchanged`: the output matches the input.
- `proven`: the equivalence prover established equivalence. A planner check, when available, is reported separately.
- `planner_checked`: a planner accepted the candidate, but equivalence was not proven. This is not trusted for automatic acceptance.
- `unproven`: equivalence was not established and there is no successful planner-only result. A failed planner check is reported here as a failed check.
- `failed`: a fatal rewrite error occurred, such as a parse, transform, or output validation failure.

Each check has a `kind`, `outcome`, and human-readable `detail`. Pipeline results keep checks on each step and collect rule-prefixed step checks alongside any direct end-to-end check. Only `unchanged` and `proven` are trusted, so `result.success` is false for `planner_checked`, `unproven`, and `failed`. The rewrite CLI prints the same label and checks; it exits with status 3 for untrusted output unless `--allow-unproven` is supplied, and exits with status 2 for a fatal rule failure without writing its output.

```python
from kumosql import apply_rules

result = apply_rules(["inline_single_use_ctes"], sql_text)
if not result.success:
    print(result.verification.status, result.verification.details)
print(result.sql)
```

`summarize_evidence(results)` reports what share of changed outputs has useful evidence, as an anonymized aggregate. A result is changed when its output text differs from its input. Only a static proof counts as useful evidence; planner-only results are reported separately (`planner_only`, plus `planner.passed` and `planner.failed` counts that overlap the label buckets) and never count toward the headline percentage. The label counts `proven`, `planner_checked`, `unproven` and `failed` are disjoint and sum to `changed`. The summary contains counts and percentages only, built from labels and check outcomes, never from SQL, names, paths or free-text reasons. Percentages are `None` when fewer than `min_changed` (default 5) outputs changed. It raises `ValueError` if unchanged text carries a changed label or a changed output lacks an evidence label. `summary.to_json()` gives the serializable form.

When the structural prover cannot canonicalize a change that touches only WHERE, HAVING, QUALIFY or JOIN ON conditions (for example a redundant or subsumed conjunct), `verify_rewrite` tries the SMT prover described below. A proof is reported as `proven` with an `smt_proof` check listing its assumptions (no NaN, runtime errors not modeled, result column types not compared). A counterexample, an unsupported construct such as an outer join, or a missing `z3-solver` install leaves the result `unproven` and the reason appears in `verification.details`. Other changes never reach SMT. Pass `smt_timeout_ms` to `verify_rewrite` to change the 5000 ms solver limit.

Verification is per statement. For `CREATE ... AS` and `INSERT ... SELECT`, the text around the query must be unchanged and the queries must be proven equivalent; any change to a final `ORDER BY` is unproven. Other statements must render identically. For SQLX, config/js/operations blocks must be identical and each interpolation is treated as an opaque fragment identified by its text.

`inline_single_use_ctes` skips recursive WITH clauses, queries with nested WITH scopes, CTEs with column aliases, references that differ from the CTE name only in case, and references with anything beyond an alias (such as `FOR SYSTEM_TIME`).

The cleanup rules only use rewrites that hold in SQL's three-valued logic. `remove_trivial_predicates` drops `TRUE` from `AND` and `FALSE` from `OR` inside WHERE, HAVING, QUALIFY and JOIN conditions, but never applies `x AND FALSE` or `x OR TRUE`, which would discard `x` and any error it raises. It leaves UPDATE, DELETE and MERGE statements unchanged because DML rewrites cannot currently be proven. It keeps `ON TRUE`, keeps `HAVING TRUE` without a GROUP BY, and folds numeric comparisons only between INT64 literals or identical literals. `remove_redundant_parentheses` keeps parentheses around an unaliased projection (BigQuery names the column after it), around a field access like `(a).b`, and around `AND` inside `OR`. `deduplicate_ctes` skips nondeterministic bodies, bodies with LIMIT, and merges that would repeat a relation name in one FROM clause.

The structural proof no longer refuses a query just because it contains `RAND()`, `GENERATE_UUID()`, `CURRENT_*` or `SESSION_USER()`. Such a call is accepted when the rewrite leaves it identical and in the same number and place (checked before and after normalization); any added, removed, duplicated, merged or modified call stays `not_proven`. Windows, tie-sensitive or order-sensitive aggregates, sampling and `LIMIT`/`OFFSET` still block the proof. A proof that relied on unchanged calls reports how many in its diagnostics.

To add a rule, subclass `RewriteRule`, set `name` and `summary`, implement `rewrite_statement(statement, index)` to edit the statement in place and return `(change_count, diagnostics)`, and decorate the class with `@register_rule`.

```powershell
rewrite-sql input.sqlx --rule inline_single_use_ctes --output output.sqlx
```

`rewrite-sql` exits 2 when a rule fails and 3 when the output is not proven equivalent (pass `--allow-unproven` to accept it). A rule failure prints a diagnostic and does not write its result.

## First goal: subquery lifting

`kumosql.lift_subqueries()` promotes every relational subquery used in a `FROM` or `JOIN` clause into a uniquely named top-level CTE. It accepts BigQuery SQL and Dataform SQLX. For SQLX, `config`, `js`, `pre_operations`, and `post_operations` blocks are preserved, while `${...}` interpolations are masked during parsing and restored afterward.

Scalar, `EXISTS`, and correlated predicate subqueries are intentionally left in place because changing those into CTEs can change query semantics. The result includes diagnostics, and an unrecoverable parse or transform error is never reported as success.

Existing CTE dependencies are respected: a lift from inside an existing CTE is placed immediately before that CTE, while a lift from the main query is appended after the existing CTEs. The lifter supports the `WITH` AST slot used by both older and newer supported `sqlglot` releases, checks for undefined or forward CTE references, and uses four-space formatting for transformed SQL. If there is nothing to lift, the input is returned byte-for-byte unchanged.

Run the parser compatibility regressions locally with `python tools/test_sqlglot_matrix.py`. The script creates temporary virtual environments for the minimum supported `sqlglot` release (`26.0.0`) and the current validated release (`30.20.0`), then runs the CTE-lifting, rule-registry, and SQLX tests in each. It exits unsuccessfully if setup or any test fails. Pass `--versions 26.0.0 30.20.0` to select releases explicitly; update `SUPPORTED_SQLGLOT_VERSIONS` in the script when the supported matrix changes.

For valid-but-unsupported BigQuery syntax, the tool may use `sqlglot` recovery mode; those rows still report a `recovered_parse` diagnostic so the exception is visible to reviewers.

```python
from kumosql import lift_subqueries

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

## Test the authored SQL fixture

The repository includes 32 hand-written sample queries at `tests/fixtures/sql_subquery_samples.json`. Each entry contains only an `id` and `sql_text`. The samples cover nested relations, joins, CTE placement, DML, DDL, and Dataform SQLX. The full fixture test uses:

```powershell
pytest -m slow tests/test_workbook_fixture.py
```

Override its path with `KUMOSQL_TEST_FIXTURE` to test another CSV or JSON fixture. The test fails unless every sample has zero remaining relational subqueries and no fatal diagnostics. A separate unit test verifies the small fixture shape.

The normal unit suite is:

```powershell
pytest
```

## Conservative SQL equivalence

`kumosql.prove_equivalent(left_sql, right_sql)` returns `proven_equivalent` only when both inputs are strict, single-query BigQuery statements whose normalized ASTs match after relational subquery lifting. By default it compares result bags, so unspecified row order is ignored.

Root CTEs are renamed by position after being put in a canonical dependency order, so CTE order alone does not block a proof; reordering is skipped for recursive WITH, forward references, or names that differ only in case.

Before comparing, the prover also removes grouping parentheses, flattens `AND`/`OR` chains, removes `TRUE` from `AND` and `FALSE` from `OR` in filter and join conditions, drops `WHERE TRUE`, merges root CTEs with identical bodies, and drops unreferenced CTEs. These normalizations are written separately from the cleanup rules, and a three-valued-logic test evaluates both against the original predicates.

It refuses to prove queries containing volatile values, windows, tie-sensitive aggregates, `TABLESAMPLE`, or any `LIMIT`/`OFFSET`. Structural differences are reported as `not_proven`, never as a proof of inequivalence. This is intentional: false negatives are acceptable; false positives are not.

For audit or optional execution, `result.verifier_sql` contains a BigQuery query that counts JSON-encoded result rows on each side and compares their multiplicities with a full outer join. The verifier is an execution artifact and does not override the static safety checks.

```python
from kumosql import prove_equivalent

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

The static prover only accepts rewrites whose normalized ASTs match. To test rewrites it cannot prove, `kumosql.check_result_equivalence(left_sql, right_sql, schema)` runs both sides against the same deterministic synthetic tables in a local DuckDB engine (BigQuery SQL is translated with `sqlglot`) and compares the results as multisets, including column names.

- Seed 0 is always empty tables; other seeds include NULLs and duplicate rows, drawn from small value domains so joins and groups collide.
- Every run gets a fresh in-memory connection. Tables written by a script (`CREATE TABLE ... AS`, `INSERT`) are renamed to run-unique local names, and the final written table is compared when the script does not end in a query.
- Dataform SQLX is supported: blocks are dropped and `${ref(...)}` becomes a table name. Any other interpolation, unknown table, or execution failure is reported as `error`, never as equivalent.
- A mismatch returns `different` with the failing seed and the rows only one side produced. Agreement is evidence, not a proof.
- Data generation is seeded and repeatable: the same schema and seed always produce the same rows (a test pins a digest of a small dataset, so a change to the value domains or draw order fails loudly). Column order in the schema mapping is part of the input.
- Each side is executed twice per seed on identical data. A side that differs from itself (for example `RAND()` or `GENERATE_UUID()`) makes the result `inconclusive`, naming the side and seed, instead of a false `different` or `equivalent`. Failures while fetching results are reported as `error`.

```python
from kumosql import assert_result_equivalent, lift_subqueries

schema = {"p.d.orders": {"customer_id": "INT64", "amount": "FLOAT64"}}
original = "SELECT * FROM (SELECT customer_id, SUM(amount) AS total FROM `p.d.orders` GROUP BY 1) AS t"
assert_result_equivalent(original, lift_subqueries(original).sql, schema)
```

Install the engine with `pip install -e ".[execution]"` (it is included in `.[dev]`). `tests/test_result_equivalence.py` runs every lifted query in its corpus through the harness and checks that deliberately broken rewrites are caught.

## SMT equivalence prover

`kumosql.prove_equivalent_smt(left_sql, right_sql)` proves semantic equivalence with Z3 instead of comparing syntax, so it accepts rewrites such as filter pushdown into a CTE, join reordering, `DISTINCT` to `GROUP BY`, `CASE` to `IF`, redundant predicates, and self-join elimination under `DISTINCT`. Install it with the optional extra: `pip install -e ".[smt]"`.

It models inner and cross joins, `WHERE`, derived tables and CTEs, `GROUP BY`/`HAVING` with `COUNT`, `SUM`, `MIN`, `MAX`, `AVG`, `COUNTIF`, `LOGICAL_AND`/`LOGICAL_OR`, `UNION ALL`/`UNION DISTINCT`, `SELECT DISTINCT`, and NULLs with three-valued logic. Other deterministic functions are uninterpreted: equal inputs give equal outputs. Anything else (outer joins, windows, `LIMIT`, predicate subqueries, nondeterministic functions) returns `not_proven`.

The result is one of:

- `proven_equivalent`: the two queries return the same bag of rows on every database, under the listed `assumptions` (no NaN, runtime errors not modeled, column types not compared).
- `not_equivalent`: `counterexample.tables` is a small database on which the queries return different rows (`left_rows`, `right_rows`). It is only reported when no uninterpreted function is involved.
- `not_proven`: outside the subset, or no proof was found.

```python
from kumosql import prove_equivalent_smt

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
- `report(observed_reads=..., observed_scope=...)`: adds a `graph` section whose edges retain declared, parsed and observed provenance, categorical confidence and observed first/last timestamps. `ObservedRead` accepts source-agnostic job rows with `creation_time`, `destination` and `referenced_tables`; callers can adapt exported history offline, and unmatched or destination-less rows remain flagged. The existing `upstream` report stays a map of model keys to parent keys.
- `completeness()`: what the analysis could not see, so a partial graph is never mistaken for a complete one. It returns `complete`, `views` (a flag each for `graph`, `lineage`, `impact` and `dead_columns`; false means results there may miss readers or edges), `by_code` counts, and `gaps`, one per asset with a `kind` (`parse_error`, `inaccessible`, `unmatched_reference`, `unattributed_reads`, or the diagnostic code such as `skipped_statements`, `unparsed_operation`, `cycle`), a `message` and `blocking`. Blocking causes are assets that failed to load or parse, scripts with extra queries (only the last is analysed), operations (never parsed), ambiguous table names, dependency cycles, and job-history rows that matched no asset or had no destination. Tables outside the pipeline are listed as non-blocking gaps. `report()` includes the same block at `completeness` and inside `graph`; a scoped report recomputes it from the diagnostics that remain in scope. Diagnostics also carry `severity` and `effect`, and each names one model (models that could not be analysed are reported one by one, so scoping keeps them).
- `column_lineage()`, `upstream_columns(col)`, `downstream_columns(col)`: column-level lineage across models and CTEs, with transitive closure. This is the blast radius of changing a column.
- `explain_lineage()`, `trace_column(col)`, `lineage_report()`: explain how each output column is built. Every output column gets one entry with a status: `traced` (built from listed source columns), `constant` (checked to read no column, such as a literal or `COUNT(*)`) or `unknown` with a reason (`unexpanded_star`, `unresolved_column` for an ambiguous or missing column, `unknown_column` for a column absent from a known schema, `lineage_error`, `untraceable_source`). Each entry also carries a transform: `passthrough`, `renamed`, `expression`, `aggregate`, `window` or `union`, taking the strongest step inside the model. `trace_column` follows the entries back to source columns and lists, separately, every column on the way that could not be traced (including columns of models that could not be parsed), so a trace with unknowns is a lower bound rather than a guess. `report()` includes the same rows as `column_lineage`. `column_lineage()` keeps its old shape but no longer contains a `*` edge for an unexpanded `SELECT *`.
- `dead_columns()`: output columns of intermediate models that nothing downstream reads anywhere (SELECT, WHERE, JOIN, GROUP BY, or inside a Dataform `${...}` expression). Terminal models count as pipeline outputs. "Dead" means not read inside the pipeline; a dashboard reading an intermediate table directly is invisible here. Results are withheld for a table when any reader could not be analysed (an unparseable model, or `SELECT *` over a source with unknown columns).
- `duplicate_selects()`: identical normalized SELECT subtrees in more than one place, such as the same CTE copied into several models. These are candidates for a shared model.
- `near_duplicate_selects(threshold=0.7)`: SELECTs that are similar but not identical, such as a CTE copied into several models where one copy gained a filter or a column. Each query level (nested CTEs and subqueries collapse to tokens) becomes a multiset of tree shingles; MinHash banding proposes pairs on large pipelines, exact Jaccard similarity confirms them, and each cluster is compared clause by clause with its centre. When the differences fit a shared model, the cluster carries `shared_sql`: `literal_parameters` (only constants differ; they become `@parameters`), `extra_filters` (extra WHERE conjuncts on a non-aggregating SELECT, applied downstream as each copy's `residual_filters`), `extra_columns` (the union of columns), or both of the last two. Anything else is `mixed`, with differences but no SQL. These are candidates to check with `prove_equivalent` or `check_rewrite`, not proven rewrites.

Source table columns come from `source_schema={"project.dataset.table": {"col": "TYPE"}}`. `fetch_table_schemas()` fills it from BigQuery with free dry runs.

```powershell
kumosql-pipeline-report path/to/dataform --source-schema sources.json --similarity 0.7 -o report.json
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
kumosql-compare-outputs compare path/to/dataform --source-schema sources.json --after-dataset-suffix _dev > compare.sql
bq query --use_legacy_sql=false --format=json < compare.sql > results.json
kumosql-compare-outputs summarize results.json
kumosql-compare-outputs drilldown path/to/dataform --after-dataset-suffix _dev --model proj.analytics.orders --keys order_id
```

## BigQuery dry-run check

`dry_run(sql, project)` asks BigQuery to plan a query without running it. It checks names and types and returns the output schema and an estimate of bytes processed. `check_rewrite(original, rewritten, project)` reports whether both statements planned and their output schemas match (column order, type, mode, and nested fields). Matching plans and schemas do not show that the statements return the same results. The estimated byte delta is a planning estimate, not a measure of changed results.

Normal `apply_rule`, `apply_rules`, the rewrite CLI, and UI do not make network requests. Call `attach_planner_check` explicitly in Python, or pass `--planner-project` to `rewrite-sql`, to attach an end-to-end planner check. It records whether each side planned, whether schemas match, schema differences, and any estimated byte delta. A planner pass can label an otherwise unproven rewrite `planner_checked`; it does not upgrade an existing proof or make an unproven result trusted. Plan failures and schema mismatches add a failed planner check and leave the result nontrusted. Missing credentials or an unavailable planner are reported as `not_run` and do not fail the rewrite.

```python
from kumosql import apply_rules, attach_planner_check

result = apply_rules(["remove_trivial_predicates"], sql_text)
result = attach_planner_check(result, "your-billing-project")
print(result.verification.to_json())
```

SQLX is not sent to BigQuery as source text. Supply compiled SQL for both sides with `--planner-compiled-original` and `--planner-compiled-rewritten`; without both, the planner check is reported as not run. Multi-statement scripts and non-SELECT statements are also skipped with an explicit `not_run` check.

Credentials come from `BQ_ACCESS_TOKEN`, a service account key in `GOOGLE_APPLICATION_CREDENTIALS_JSON` (contents) or `GOOGLE_APPLICATION_CREDENTIALS` (path), or Google Application Default Credentials. ADC supports `gcloud auth application-default login`; install `pip install '.[bigquery]'` for the Google auth library. BigQuery dry runs need BigQuery Job User plus Data Viewer. Catalog browsing uses the read-only BigQuery scope and requires permission to list projects and read the selected metadata.

```powershell
kumosql-dry-run original.sql --rewritten rewritten.sql --project my-project
rewrite-sql input.sql --rule remove_trivial_predicates --planner-project my-project
```

The dry-run command reports planning and schema outcomes separately from rewrite evidence. It exits 0 when both statements plan with matching schemas and 2 otherwise. `rewrite-sql` keeps its usual exit behavior: a planner-only result is nontrusted and exits 3 unless `--allow-unproven` is supplied; a fatal rewrite failure remains exit 2 and its output is withheld.

## Cost, change reports and refactoring proposals

These pieces build on the pipeline graph. They report evidence and never claim more than they know: anything they cannot determine is shown as `unknown` or `unattributed`. The UI's `/cost` and `/changes` pages still show sample data until a job history or a pair of project snapshots is supplied; `docs/ui-roadmap.md` lists the JSON each view expects.

- **Cost attribution** (`costs.py`): `load_jobs(path)` reads a JSON, JSON lines or CSV export of query job history, and `build_cost(pipeline, jobs)` attributes each job's billed bytes to one graph node. Measured and estimated costs are separate types and cannot be added. Jobs that cannot be placed go to an explicit `unattributed` bucket with a reason, and attributed plus unattributed always equals the total. A query with no destination is charged to the outermost node it read, so a query against a view lands on the view.
- **Repeated work** (`repeated_work.py`): `repeated_work_report(pipeline)` lists identical logic, similar logic and tables read repeatedly, within one query and across models, with the location of each repeat. It reports where work repeats, not what it costs.
- **Table roles** (`table_roles.py`): `infer_roles(pipeline, row_counts=None, declared=None)` gives every model and declared source a role (`dimension`, `fact`, `bridge` or `unknown`), a `high`/`medium`/`low` confidence and the list of signals behind it. Signals come from how parsed models read the table (lookup join vs. aggregated), its shape (source column types, or GROUP BY/DISTINCT keys and aggregate columns for models), relative size when `row_counts` is given, and a hand-declared role, which always wins. Two independent agreeing signals give `high`; one gives at most `medium`; conflicting signals give `unknown` with the conflict listed; size never decides alone. Unavailable signals are listed with `available: false` and never vote, and readers that could not be parsed are counted as unexamined. Names are never used to pick a role. `table_roles_report(pipeline)` returns the JSON form.
- **Ranking and recommendations** (`opportunities.py`, `recommendations.py`): `rank_opportunities` orders opportunities by measured cost, frequency and downstream reach without mixing `measured`, `estimate` and `upper_bound` savings. `build_recommendation` states where work repeats, who relies on it, the proposed change and how it would be verified.
- **Cost rules** (`cost_rules.py`): `rule_catalog()` lists cost rewrite rules with their safe conditions and review requirements. One rule ships so far: `remove_redundant_distinct`, which removes `DISTINCT` only over a plain `GROUP BY` whose keys are all projected unchanged.
- **Validated savings** (`savings.py`): `Ledger` records accepted changes with their estimate, then computes validated savings only from measured before and after windows (`min_runs` defaults to 1).
- **Change reports** (`change_report.py`, `kumosql-change-report BASE HEAD [--cost FILE] [-o FILE]`): compares two project snapshots and reports, for each changed model, the verification label, cost (unknown unless supplied) and downstream consumers, with `complete: false` when the graph has gaps.
- **Review check** (`ci_check.py`, `kumosql-ci-check report.json`): turns a change report into a check conclusion and a markdown comment. `success` needs every change proven or unchanged, complete consumer lists and no diagnostics; anything less is `neutral`, and a failed change is `failure`. `docs/change-report-workflow.example.yml` shows a workflow; it is not installed under `.github/workflows`.
- **Query sources** (`query_sources.py`): a source is `connected` only when every asset it reports maps to a graph identity; otherwise it is `not_enabled`.
- **Failures stay local** (`resilience.py`): an asset that cannot be read or parsed becomes an entry in `diagnostics` and the rest of the report is still produced. Diagnostics carry no file contents.
- **Refactoring proposals** (`shared_logic.py`, `filter_pushdown.py`, `proposal_readiness.py`): `propose_shared_logic` and `find_upstream_filter_proposals` suggest extracting shared logic or pushing a filter upstream, listing every affected consumer and refusing when the consumer set is incomplete. Neither applies changes. `assess_proposal` marks a proposal `ready` only when every consumer is `proven` or `unchanged`; a missing result is `unknown`.

## CLI

```powershell
lift-subqueries input.sql --output output.sql --report
```

## What's next

KumoSQL is moving toward a query graph that combines declared and observed dependencies with measured cost, backed by the verification engine described above. The near-term focus is making failed rewrites impossible to mistake for successful ones and making verification results easier to review.
