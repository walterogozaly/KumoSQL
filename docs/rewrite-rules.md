# Rewrite rules

The rewrite rule registry and the subquery lifter.

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
| `remove_redundant_distinct` | Removes `DISTINCT` over a plain `GROUP BY` whose keys are all projected unchanged (see *Cost rules*) |
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

`summarize_evidence(results)` reports what share of changed outputs has useful evidence, as an anonymized aggregate. A result is changed when its output text differs from its input. Useful evidence is a static proof (label `proven`) or a passing `synthetic_results` check (agreement on synthetic data) on a result that did not fail; each output counts once. Agreement is evidence, not proof, so `pct_proof` and `synthetic_agreed` are reported separately from the headline `useful_evidence`. Planner-only results are reported separately (`planner_only`, plus `planner.passed` and `planner.failed` counts that overlap the label buckets) and never count toward the headline percentage. The label counts `proven`, `planner_checked`, `unproven` and `failed` are disjoint and sum to `changed`. The summary contains counts and percentages only, built from labels and check outcomes, never from SQL, names, paths or free-text reasons. Percentages are `None` when fewer than `min_changed` (default 5) outputs changed. It raises `ValueError` if unchanged text carries a changed label or a changed output lacks an evidence label. `summary.to_json()` gives the serializable form. From the command line, `python -m kumosql evidence-summary PATH... --rule RULE [--min-changed N]` applies the rules to every `.sql` or `.sqlx` file (directories are searched recursively) and prints only the aggregate JSON, with no file names or SQL. The UI's `/changes` page shows the same numbers as a "Useful evidence" line in the Evidence coverage panel.

When the structural prover cannot canonicalize a change that touches only WHERE, HAVING, QUALIFY or JOIN ON conditions (for example a redundant or subsumed conjunct), `verify_rewrite` tries the SMT prover described below. A proof is reported as `proven` with an `smt_proof` check listing its assumptions (no NaN, runtime errors not modeled, result column types not compared). A counterexample, an unsupported construct such as an outer join, or a missing `z3-solver` install leaves the result `unproven` and the reason appears in `verification.details`. Other changes never reach SMT unless the equivalence solver below is on. Pass `smt_timeout_ms` to `verify_rewrite` to change the 5000 ms solver limit.

Verification is per statement. For `CREATE ... AS` and `INSERT ... SELECT`, the text around the query must be unchanged and the queries must be proven equivalent; any change to a final `ORDER BY` is unproven. Other statements must render identically. For SQLX, config/js/operations blocks must be identical and each interpolation is treated as an opaque fragment identified by its text.

A change to layout only is proven for any statement, including ones sqlglot cannot parse (`LOAD DATA`, `REPEAT ... UNTIL`) or keeps as an opaque command (`ALTER SCHEMA`, `CALL`, `CREATE ROW ACCESS POLICY`). Both texts are tokenized completely and must have the same tokens and comments in the same order; strings, quoted names and numbers must match exactly, and only reserved keywords and calls to built-in functions may change case. Unreserved keywords such as `DATE` can be table names and user-defined function names are case sensitive, so their case must match (`layout_equivalence.py`); a function the script creates keeps its case even when its name is quoted or a comment precedes it (a script may create both `abs` and `ABS`), and the statement-by-statement check also refuses a change to the case of such a call. Adjacent string or bytes literals must be separated in both texts or in neither (`'a' 'b'` is one literal; `'a''b'` is not valid GoogleSQL). For the same reason `format_sql` puts back the original case of every call to a function that is not built in (`myUdf(x)` stays `myUdf(x)`). When sqlfluff cannot parse a file as a whole (a `GRANT` or `EXPORT MODEL` among queries), `format_sql` formats each statement it can parse and leaves the others as written (`statements_not_formatted`); a file with no statement it can parse still reports `parse_error`. KumoSQL needs sqlfluff 4.3 or later (`pyproject.toml`): older releases cannot parse some BigQuery statements the syntax-coverage eval formats (pipe syntax with a CTE, `CREATE AGGREGATE FUNCTION`, some comparison operators), so `pip install -e .` upgrades an older copy.

**Pipe syntax.** sqlglot parses `|>` pipe syntax into nested CTEs and subqueries, so a rule would rewrite that translation and print it as standard SQL. Cleanup rules leave pipe-syntax statements as written (`pipe_syntax_kept`); the prover still reads them through the translation. Pipe `SET` and `DROP`, which sqlglot rejects, are read as the `SELECT * REPLACE` and `SELECT * EXCEPT` they stand for, and pipe `WITH` as a `WITH` at the front of its query when no name could change meaning (`bigquery_syntax.py`); `RENAME` is not read.

**Templated SQL.** sqlglot reads a Jinja tag such as `{{ ref('x') }}` as a nested struct literal and would print it back as `STRUCT(STRUCT(ref('x')))`, so every rule except `format_sql` leaves SQL with a `{{`, `{%` or `{#` tag outside strings and comments exactly as written (`templated_sql_kept`).

`inline_single_use_ctes` skips recursive WITH clauses, queries with nested WITH scopes, CTEs with column aliases, references that differ from the CTE name only in case, and references with anything beyond an alias (such as `FOR SYSTEM_TIME`). It, `remove_unused_ctes` and `deduplicate_ctes` also leave a WITH clause alone when a CTE body is not a query (a PostgreSQL data-modifying CTE runs even when nothing reads it).

The cleanup rules only use rewrites that hold in SQL's three-valued logic. `remove_trivial_predicates` drops `TRUE` from `AND` and `FALSE` from `OR` inside WHERE, HAVING, QUALIFY and JOIN conditions, but never applies `x AND FALSE` or `x OR TRUE`, which would discard `x` and any error it raises. It leaves UPDATE, DELETE and MERGE statements unchanged because DML rewrites cannot currently be proven. It keeps `ON TRUE`, keeps `HAVING TRUE` without a GROUP BY, and folds numeric comparisons only between INT64 literals or identical literals. `remove_redundant_parentheses` keeps parentheses around an unaliased projection (BigQuery names the column after it), around a field access like `(a).b`, and around `AND` inside `OR`. `deduplicate_ctes` skips nondeterministic bodies, bodies with LIMIT, and merges that would repeat a relation name in one FROM clause.

**Idempotence.** Running a rule on its own output makes no further change; `tests/test_idempotence.py` checks every registered rule (and `format_sql` with non-default preferences) against the fixture corpus and hand-written edge cases, so a newly registered rule is covered automatically. `canonical_rule_order()` returns the pipeline that is also a fixed point: every rule except `lift_subqueries`, with `format_sql` last. `lift_subqueries` and `inline_single_use_ctes` are inverses, so a pipeline containing both rewrites its own output on every run; run the lifter separately. To check any rule list at run time, call `check_idempotence(names, sql)` or pass `--check-idempotence` to `python -m kumosql rewrite-sql`: it re-runs the rules on their own output, compares the exact text, and names the rules that changed it again (the CLI exits 4 on a violation). It doubles the work, so it is opt in. Formatting last matters because the other rules re-render a statement and discard its layout.

The structural proof no longer refuses a query just because it contains `RAND()`, `GENERATE_UUID()`, `CURRENT_*` or `SESSION_USER()`. Such a call is accepted when the rewrite leaves it identical and in the same number and place (checked before and after normalization); any added, removed, duplicated, merged or modified call stays `not_proven`. Windows, tie-sensitive or order-sensitive aggregates, sampling and `LIMIT`/`OFFSET` still block the proof. A proof that relied on unchanged calls reports how many in its diagnostics.

To add a rule, subclass `RewriteRule`, set `name` and `summary`, implement `rewrite_statement(statement, index)` to edit the statement in place and return `(change_count, diagnostics)`, and decorate the class with `@register_rule`.

```shell
python -m kumosql rewrite-sql input.sqlx --rule inline_single_use_ctes --output output.sqlx
```

`python -m kumosql rewrite-sql` exits 2 when a rule fails, 4 when `--check-idempotence` finds the rules change their own output, and 3 when the output is not proven equivalent (pass `--allow-unproven` to accept it). A rule failure prints a diagnostic and does not write its result.

## Subquery lifting

`kumosql.lift_subqueries()` promotes every relational subquery used in a `FROM` or `JOIN` clause into a uniquely named top-level CTE. It accepts BigQuery SQL and Dataform SQLX. For SQLX, `config`, `js`, `pre_operations`, and `post_operations` blocks are preserved, while `${...}` interpolations are masked during parsing and restored afterward.

Scalar, `EXISTS`, and correlated predicate subqueries are intentionally left in place because changing those into CTEs can change query semantics. So is a FROM or JOIN subquery that a top-level CTE could not express: one with a qualified column, in any branch of a set operation or nested predicate, that names a relation of an enclosing query (a correlated or lateral derived table), and one that reads a name defined by a WITH clause nested around it (`correlated_subquery_kept`, not an error; unqualified columns cannot be resolved without a schema and are not checked). The prover's own normalization (`lift_subqueries(..., rewrite_pipe_syntax=True)`) still lifts these. The result includes diagnostics, and an unrecoverable parse or transform error is never reported as success.

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
