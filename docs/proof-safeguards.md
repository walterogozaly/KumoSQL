# Proof safeguards

A proof is only worth something if it can catch the rewrite's mistake. KumoSQL's structural prover normalizes both sides of a proof with code that rules also use: the subquery lifter, and the predicate folding written once in `cleanup.py` and once in `equivalence.py` (which shared `_literal_compare`). A bug shared by a rule and that normalization makes both sides agree, and the proof approves the wrong output. This page describes the safeguards against that failure, what they guarantee, and what is not covered yet.

## Independent step checks

`kumosql.proof_steps` and `kumosql.proof_ctes` check a step from its before and after statements alone. They import no rule, normalizer, SMT compiler or result comparator, and re-derive every claim. Five families are covered (family names `predicate_cleanup`, `cte_binders`, `parenthesization`, `redundant_distinct`, `qualify_columns`): **predicate cleanup** (`proof_steps`), **CTE binders** (`proof_ctes`, below), **parenthesization** and **redundant DISTINCT** (`proof_syntax`, below) and **column qualification** (`proof_qualify`, below). `kumosql.proof_registry` is the one reviewed table of them (see Acceptance registry). A predicate step is accepted only when all of these hold:

| Assumption recorded on the step | How the checker verifies it |
| --- | --- |
| `statement_frame_preserved` | Everything outside WHERE, HAVING, QUALIFY and JOIN ON is unchanged node for node: projections, tables, joins, grouping, ordering, statement targets. The predicate identity then holds for every row, group and join match. |
| `clause_context_preserved` | A JOIN ON is never dropped. HAVING without GROUP BY is never dropped, because it makes the query an aggregate. QUALIFY is dropped only when its query has a window function; without one, GoogleSQL rejects the original. |
| `opaque_predicate_occurrences_preserved` | Every non-constant predicate (a column, a comparison, a call) keeps each occurrence, so nothing that could raise an error is discarded. Each occurrence is its own variable, so two `RAND()` calls are never one value. |
| `exact_literal_domain` | Only these comparisons become constants: two INT64 literals, a numeric literal compared with the same text (`1e309 = 1e309` is TRUE in BigQuery), or two identical string literals without escapes compared with `=` or `!=`. Anything else stays opaque. |
| `sql_three_valued_logic` | SQLite evaluates the old and new predicate for every TRUE/FALSE/NULL combination of their atoms. They must agree on the whole truth value, not only on whether a row passes. |

A change nested inside an atom, such as `x IN (SELECT y FROM u WHERE TRUE)`, is checked the same way one level down. Any failure rejects the step, including an error inside the checker or more than `MAX_PREDICATE_ATOMS` (8) atoms in one clause. There is no sampling fallback. A disagreement reports the atom values that show it (`counterexample`).

The checkers run in two places:

- **Rule acceptance.** `rewrite.INDEPENDENT_CHECK_FAMILIES` (the registry's `RULE_FAMILIES`) maps `remove_trivial_predicates` to the predicate family, `remove_unused_ctes`, `inline_single_use_ctes` and `deduplicate_ctes` to the CTE family, `remove_redundant_parentheses` to the parenthesization family, `remove_redundant_distinct` to the DISTINCT family and `qualify_columns` to the qualification family. The acceptance layer owns this table and keys it by rule name, so neither the rule nor an override passed to `apply_rule` can opt out. The checks come from the actual input and output: each changed statement of SQL, a script or a SQLX section is checked. They don't come from records the rule supplies, so nothing can be missing or forged. A step is `proven` only when the prover and the independent checker both accept it. Each check appears in `verification.checks` as kind `independent_check`, with its family, statement (and SQLX section), assumptions, cases checked and any counterexample. The same checks are in `verification.proof_checks` as `StepCheck` records, and the CLI and the UI's verification report show them.
- **Inside the structural prover.** `equivalence._prepare_query` records its own `_normalize_predicates` transition and has the predicate checker re-derive it before grouping and CTE normalization run. It also records the whole CTE normalization (renaming to canonical names, reordering, merging identical bodies, dropping unreferenced CTEs) and has the CTE checker re-derive it, and does the same for its grouping-parenthesis stripping and redundant-DISTINCT dropping (recorded as `strip_grouping_parens` and `drop_redundant_distinct`). A refused transition makes `prove_equivalent` return `not_proven` ("the independent check of a normalization step refused it"). Accepted transitions are listed in `EquivalenceResult.proof_checks`.

**Pipelines.** If an independent check refuses any step of `apply_rules`, or a safeguarded rule's step is not `proven` for any other reason, the pipeline is `unproven`. This holds even when a later step restores the original text or the end-to-end result could be proven, because that end-to-end proof uses the prover the checker guards.

Tests corrupt the rule and the prover's normalizer the same way (`tests/test_proof_steps.py`), and the checker still refuses the result. Matching normalization alone would have certified it.

## CTE binders

A CTE rule is only as good as its answer to one question: which relation does this name in FROM mean? A name can be a CTE, a table with the same name (a CTE is visible only to the CTEs after it, never to itself or an earlier one), a nested WITH's CTE that shadows an outer one, or the same name with different case (BigQuery reads names case-insensitively). A mistake in how a rule or the prover's normalizer finds references would be shared by both, so both sides of a proof would agree on the wrong answer.

`proof_ctes` answers the question once, from the statements alone, and compares by **expansion**: it replaces every CTE reference with the CTE's definition (a subquery with the reference's alias, or the CTE's name when there is none) and drops every WITH clause. The expansions of the before and after statements must be identical node for node, apart from the quoting of plain names. Renaming, reordering, merging, inlining and dropping a CTE nobody reads leave the expansion unchanged. Capturing a name, pointing a reference at another definition, dropping a CTE something still reads, or changing anything else alongside changes it, and the step is refused with the first node that differs.

Assumptions recorded on each step: `cte_scope_resolution`, `reference_equals_inline_subquery`, `volatile_bodies_refused`, `unreferenced_ctes_have_no_effect`, `expanded_statements_identical`. A step is **refused, never guessed**, when:

- a WITH is recursive, repeats a name (up to case), or carries a MATERIALIZED hint;
- a name that matches a visible CTE appears anywhere other than as a FROM or JOIN relation, or that relation carries more than an alias or a PIVOT/UNPIVOT (a snapshot, a sample); a PIVOT or UNPIVOT on a reference moves onto the substituted subquery, since it applies to whatever relation the name stands for;
- a volatile call (`RAND()`, `GENERATE_UUID()`, the current time) sits in a CTE that the expansion copies more than once, because merging, inlining or splitting its readers changes how many values are drawn;
- a CTE name disappears while a bare identifier spelled like it is still an argument of a function (a table passed by name), because that read cannot be tracked;
- the expansion would copy more than `MAX_EXPANDED_NODES` (50,000) nodes.

A one-part name in FROM means the CTE even when it is also another FROM item's alias. This was checked on BigQuery (`FROM a CROSS JOIN a AS b`, and a subquery reading `a AS b` under an outer `FROM a`, both read the CTE `a`), so the checker does not refuse those, unlike sqlglot 26's parser, which reads a repeated name as an array path.

**What it found.** On its first run over a corpus of CTE queries the checker refused `inline_single_use_ctes` on `WITH b AS (SELECT * FROM a), a AS (SELECT 1 x) SELECT * FROM b`. In that query the `a` inside `b` is a table, because `a` is defined after `b`. The rule inlined `b` first, which moved that read into the main query, where `a` is the CTE, and then inlined `a` into it. The rule now leaves a WITH clause with a forward reference alone (`cte_dependency_errors`, as the other CTE rules already did). The test is in `tests/test_proof_ctes.py`.

## Parentheses and DISTINCT

Two small rewrites had the same shape of risk: the rule and the prover's normalizer each decide for themselves which parentheses are meaningless (`cleanup._redundant`, `equivalence._paren_is_semantic`), and the DISTINCT rule and the normalizer share `distinct_safety.distinct_is_redundant`. `proof_syntax` re-derives both decisions from the step's SQL text (never from the caller's in-memory trees, which can print differently from how they read: `(t).x` printed as `t.x`).

- **`parenthesization`** (assumptions `parse_structure_unchanged`, `parentheses_carry_no_other_meaning`). The after statement must have the same tree as the before statement once every parenthesis node is removed from both and each chain of `AND` (or of `OR`) is read left to right, because both are associative in three-valued logic and keep their operand order (`a AND (b AND c)` to `a AND b AND c` is accepted; other operators keep their grouping, since `a + (b + c)` can differ from `(a + b) + c` in floating point and on overflow). Equal stripped trees mean every operator groups the same way, so `(a OR b) AND c` to `a OR b AND c`, `-(-x)` to `--x` (a comment) and `(t).x` to `t.x` all fail. Output column names follow the expression, not its parentheses (checked on BigQuery). It trusts sqlglot to read operator precedence as BigQuery does.
- **`redundant_distinct`** (assumptions `statement_frame_preserved`, `group_keys_are_plain_columns`, `group_keys_projected_unchanged`, `distinct_and_group_by_share_equality`). The after statement must equal the before statement with DISTINCT cleared on some SELECTs, and each cleared SELECT must have a plain GROUP BY (columns only: no ROLLUP, CUBE, GROUPING SETS, ALL or totals) whose keys are all projected unchanged. A select alias spelled like a key counts only when it projects that same column, because BigQuery's GROUP BY prefers the alias (checked on BigQuery). DISTINCT ON and DISTINCT without GROUP BY are refused.

## Column qualification

`qualify_columns` rewrites `SELECT id FROM o JOIN c ON ...` to `SELECT o.id ...`. Which FROM item owns a bare column is a decision the rule makes and the algebraic and SMT provers make again when they resolve a bare column with a schema, so a wrong owner, a select alias qualified as if it were a table column, or a name two sources share could be agreed on by both. (The structural prover's `_prepare_query` never qualifies a column, so it has no transition to record; the algebraic and SMT provers' own resolution is not re-derived, and the rule's step is what is checked.) `proof_qualify` re-derives the claim from the step's before and after text and imports no rule, normalizer, lifter or prover (`proof_steps` record types and `proof_syntax._reparse` only). Assumptions recorded: `only_qualifiers_added`, `qualifier_names_the_only_readable_source_with_the_column`, `source_columns_known_exactly`, `output_names_and_value_names_stay_bare`, `supplied_table_columns_complete_and_input_valid`.

1. **Only qualifiers were added.** Walking the two trees together, they must be identical node for node except that some columns, bare before, carry a table qualifier after (no database part). A renamed alias, a changed literal, a dropped clause or a re-qualified column anywhere else is a refusal, so a change hiding inside the step cannot pass.
2. **Each added qualifier names the one readable source that has the column.** Re-derived from the before statement, per column:
   - Every FROM item of the select has known columns: a CTE or derived table whose output names are all explicit (a `*`, an unnamed expression, a duplicate name, `SELECT AS VALUE` or `AS STRUCT`, or a set operation that matches by name is refused), an aliased `UNNEST` (its element is a value, never an owner), or a physical table whose columns the acceptance layer supplies. CTE names resolve case-insensitively with the scope rules of the CTE checker (a CTE shadows a table, sees only earlier ones, a recursive WITH is refused). A table function, `PIVOT`, a NATURAL, SEMI or ANTI join, a table that carries a clause other than a snapshot, sample or hint, or two sources with one name refuses the step.
   - Exactly one source the column can read has the name, and the qualifier is that source's alias, or its table name when it has no alias. A `JOIN ... ON` reads the sources up to and including its own join, and an `UNNEST` argument reads only the sources before it: a later source's column of the same name is an outer reference there (checked on BigQuery with dry runs: `FROM UNNEST(q) AS v CROSS JOIN (SELECT [5] AS q) AS b` under an outer `q` is valid, and `ON x.k = w.z` before `JOIN ... AS w` is `Unrecognized name: w`), so qualifying it changes the query.
   - The name is not a `USING` column, not the name of a source or an `UNNEST` element or offset, and in `GROUP BY`, `HAVING`, `QUALIFY`, `ORDER BY` or a named window not an output name (a select alias, a `* REPLACE` name, the name of a qualified item such as `s.f`, a path). BigQuery reads those first there; in `WHERE`, `ON` and the select list they are not visible (checked on BigQuery, including inside `OVER (...)` in the select list, which does not see an alias, and in `QUALIFY`, which does). A bare unaliased column item of the same name is the column itself and does not block it.
   - It is not inside a star's `EXCEPT`, `REPLACE` or `RENAME`, a lambda, a `PIVOT`, a set operation's or parenthesized query's own `ORDER BY` or `LIMIT`, or the argument of a function while spelled like a date part (sqlglot parses `MONTH` in `FOO(d, MONTH)` as a column).

A struct field with no source column of that name, and a correlated reference to an outer select, have no owner among the select's own sources, so a qualifier on them is refused. A `SELECT AS STRUCT` derived table is refused as a whole, because BigQuery reads the fields of a struct value bare (checked), which this check does not enumerate.

**Facts the SQL cannot give.** The columns of a physical table come from the loaded project and the saved BigQuery catalog. The acceptance layer (`rewrite._independent_checks`, not the rule) reads them and records the ones the statement names on the step (`RewriteStep.known_columns`, also in the step's JSON), so a replay needs no catalog. Without them a plain table is unknown and the step is refused. The check trusts that these column lists are complete and that the original statement runs on BigQuery (an `UNNEST` element's struct field that a source also has would be ambiguous and rejected by the engine; the check does not look for it).

**What it found.** Working out what the check must refuse found cases the rule had wrong, now fixed in `qualify_columns.py`: a column of a later source qualified in an earlier `ON` or in an earlier `UNNEST`; a `SELECT AS VALUE` derived table treated as having columns; `ORDER BY f` rewritten to another source's `f` when the select outputs the field `s.f` (checked on BigQuery: `ORDER BY f DESC` sorted by the output `s.f`, not by the other source's `f`); a date part read as a column in `FOO(d, MONTH)`; a column of a `SEMI` or `ANTI` join's right side treated as readable. Tests (`tests/test_proof_qualify.py`) cover accepted and refused qualifications, an extra change hiding in the step, the corpus (about 1,700 queries from the fixtures and the repository's tests, each with three invented schemas: the rule's output is accepted, and re-qualifying any added qualifier with another source is refused), fault injection (the rule picking the wrong table, ignoring aliases, ignoring ON visibility, reading a value table's columns, or seeing one owner for a shared name, each with the prover's verdict forced to "proven"), an override that cannot opt out and a later step that cannot rescue a refused step.

## Type names BigQuery rejects

sqlglot reads other engines' type names and prints them as BigQuery types (`FLOAT` as `FLOAT64`, `INT32` as `INT64`, `VARCHAR` and `UUID` as `STRING`), so two queries that differ only in such a name parse to one tree, and a proof would credit a pair that BigQuery rejects ("Type not found"). `type_names.invalid_type_name(sql)` reads the source text, which still has the names, and returns the reason for a name BigQuery does not have. The accepted names were confirmed by BigQuery dry run (2026-10-04).

**Where a type is read.** Only places where a type keyword cannot be a column name or an alias (`SELECT a float FROM t` is valid, so a bare keyword elsewhere is not read): the target of `CAST`, `SAFE_CAST`, `TRY_CAST` and `::`; any `ARRAY<...>` or `STRUCT<...>`, including nested ones and typed literals (a struct field named like a type, `STRUCT<text STRING>`, is a name, not a type); `RANGE<...>` inside a cast; a typed literal (`FLOAT '1'`); and, in `CREATE TABLE`, `CREATE FUNCTION` and `CREATE PROCEDURE`, the column and parameter list (`ANY TYPE` is valid there) and the `RETURNS` type, plus `DECLARE`. The first statement of a `BEGIN` block is read too, since the tokenizer keeps it as one string. Before this check, `SELECT ARRAY<FLOAT>[1]`, `STRUCT<x INT32>(1)`, `FLOAT '1'` and `CREATE TABLE d.x (a FLOAT) AS SELECT 1` were each proven equal to their `FLOAT64` and `INT64` spellings by every prover.

**Entry points.** Each refuses before it proves, bounds or searches, and only for the BigQuery dialect (another dialect's names are its own):

| Entry point | Refusal |
| --- | --- |
| `equivalence.prove_equivalent`, `smt_equivalence.prove_equivalent_smt`, `algebraic_equivalence.prove_equivalent_algebraic` | `not_proven` |
| `statement_proof.prove_statements`, `prove_statements_smt` (they read the whole statements, so a `CREATE TABLE` column list is seen) | `not_proven` |
| `sqlsolver_backend.prove_equivalent_sqlsolver` (`prove_equivalent` reaches it, or the others, on every backend) | `not_proven`; the solver is never started |
| `rewrite` acceptance (`_verify_sql`) | `unproven`; a layout-only change (same tokens) is still accepted, since nothing but whitespace and case moved |
| `bounded_equivalence.check_bounded` (and `prover_context.bounded` through it) | `unknown`, no bound reported |
| `counterexample.find_counterexample`, `executed_refutation.search_counterexample` | no search (`False`, `None`): DuckDB would run the query BigQuery rejects |

`prover_context.prove`, the pipeline, model-reuse, containment, optimizer, constraint-dependence and table-minimizer entry points call the algebraic prover and inherit its refusal. Each entry point has a test that the invalid name is refused, the valid spelling still gets its verdict, and the entry point certifies the invalid query again once the check is switched off (`tests/test_type_names.py`).

**Not covered.** `result_equivalence.check_result_equivalence`, `random_check` and the synthetic-data comparison run both queries on generated data and report agreement, not a proof (the evidence labels say so); they do not read type names. A bare type keyword in an unlisted place (`ALTER TABLE ... ADD COLUMN`, a column definition in a statement that is not a `CREATE`) is not read. A function BigQuery does not have (`CONVERT(x, FLOAT)`) is the parser's to refuse, not this check's.

## Acceptance registry

`kumosql.proof_registry` holds `FAMILIES` (each family's name, assumptions, checker and summary), `RULE_FAMILIES` (rule name to family; the acceptance layer reads it, so a rule or an override cannot opt out) and `LEGACY_BASIS` (every rule with no independent checker yet, and what its proof rests on: `format_sql` and `lift_subqueries`). `tests/test_proof_registry.py` fails when a rewrite rule is registered but is in neither table, so adding a rule forces a choice between an independent checker and a named legacy basis, and when a family accepts a step with foreign assumptions. A rule moves from legacy to independent only after the checker has its corpus and fault-injection tests; moving one back is a regression.

## Dataform expressions

To prove a SQLX rewrite, each `${...}` is masked with a name derived from its text. That is faithful only for an expression that expands to one relation name: `self()`, or a single `ref(...)` or `resolve(...)` call with any arguments (`${ref({schema: vars.S, name: "t"})}` too), as long as the call ends the expression and its arguments hold no template literal, comment, `/` or backslash. Any other expression, such as `${when(incremental(), "AND ts > ...")}`, `${"x OR y"}`, `${ref("t") + " OR x"}` or a JavaScript constant, can expand to several operators, a whole clause, nothing at all, or a reference to a CTE the masked text never mentions. So a changed statement that holds one is `unproven`, and the reason says to compile the SQLX first. Statements the rewrite left alone are unaffected.

Three cases are easy to miss, and all three are `unproven` too (`sqlx_fragments.py`, `rewrite._verify_sql`):

- **An expression inside a string literal.** `'${constants.P}'` compiled with `x' = 'x' OR 'x` closes the quote and adds an operator, so removing the parentheses in `('${constants.P}' = 'ok') AND b` changes which rows match. The earlier design read such an expression as plain literal text and was wrong for this.
- **A layout-only change.** Template layout is not compiled layout: an expansion can end in a line comment (`b --`), so moving the newline in `WHERE ${P}\n AND y` to `WHERE ${P} AND y` changes what the comment swallows. A layout-only change in text that holds a non-relation expression is refused. A layout-only change without one stays proven.
- **The loader's masked names.** The loader masks an expression by position (`__sqlx_token_000__`), so two models with different expressions can load to the same text, and a masked string next to `'paid'` looks like a contradiction. Every prover entry point (`equivalence.prove_equivalent`, `smt_equivalence.prove_equivalent_smt`, `algebraic_equivalence.prove_equivalent_algebraic`, `sqlsolver_backend.prove_equivalent_sqlsolver`) refuses text that holds a positional token or a string literal holding a masked expression (`masked_template_problem`).

The cleanup and CTE rules also leave such a statement as written (`keep_sqlx_expressions`), so the verifier is the second layer: it refuses a rule, override or pipeline that does not.

Before this check, these rewrites were labeled `proven`:

| Rule | Input | Output |
| --- | --- | --- |
| `remove_redundant_parentheses` | `WHERE z AND (${"x OR y"})` | `WHERE z AND ${"x OR y"}`, which reads as `(z AND x) OR y` |
| `remove_trivial_predicates` | `WHERE TRUE ${when(incremental(), "AND b > 1")}` | `WHERE ${...}`, which is invalid SQL either way |
| `remove_unused_ctes` | a CTE read only inside `${"x IN (SELECT x FROM c)"}` | the CTE deleted |
| `inline_single_use_ctes` | the same CTE | inlined, leaving the expression's reference dangling |

## Limits

- Predicate cleanup, CTE rewrites, parentheses, redundant DISTINCT and column qualification are independently checked. The other rules (`lift_subqueries` and `format_sql`; see `LEGACY_BASIS`) and the SMT-based prover keep their existing basis: the normalized-AST comparison, the layout comparison and the solver. The lifter that turns FROM subqueries into CTEs is not checked yet, and its `__lifted_subquery_*` name heuristic stays.
- The qualification check takes a physical table's columns from the project and catalog the rule also reads; it trusts those lists to be complete and current (a wrong list can make a wrong qualifier look justified) and trusts that the original statement runs on BigQuery. It refuses a select with any source whose columns it cannot know, so it can refuse a valid qualification: any select that reads a `SELECT AS STRUCT` derived table, a table function, a pivot, or a plain table the catalog does not know. The rule leaves those selects alone, so rule output is not refused for them. An `UNNEST` element is assumed to hold no struct field a source also has (the engine rejects that as ambiguous). The algebraic and SMT provers' own bare-column resolution is not re-derived.
- The DISTINCT check is a sufficient condition, not a complete one: a DISTINCT that is redundant for another reason (a unique key, a one-row table) is not accepted by this checker, so a rule that removes it is `unproven`.
- The CTE check is exact on the expanded tree. A rewrite that changes a CTE and the query together in a way that is still equivalent (a filter moved into a CTE, say) is refused here; those rules are proven by the prover alone and are not in the CTE family.
- An unread CTE is assumed to have no effect, as BigQuery never evaluates one (checked 2026-10-03); the checker does not look for errors that dropping it would hide.
- sqlglot's parse is checked separately, not by this checker: every proof is refused when an independent reading of its text disagrees with sqlglot's ([parser checks](parser-checks.md)). This checker still does not establish BigQuery validity or schemas.
- SQLite is used only for the Boolean and literal model, never to run BigQuery SQL.
- The type-name check is a refusal list for the places listed under [Type names](#type-names-bigquery-rejects); it does not validate types, and data-comparison evidence (`check_result_equivalence`, `random_check`) does not read them.

## Extending it

The architecture audit (October 2026) proposes the next families, in order:

1. **CTE and relation rewrites:** done for the three CTE rules and the prover's CTE normalization (above), by expansion. Still open: the lifter (check its output expands back to its input, and replace the `__lifted_subquery_*` prefix heuristic with recorded binder provenance) and rules that rewrite relations without CTEs.
2. **SMT compiler:** reject every AST argument the compiler does not model, so unmodeled syntax (`* EXCEPT`, a table snapshot) cannot vanish from the formula. Record typed column identities and coercions, and treat solver timeouts and checker failures as unresolved.
3. **Typed values:** check integer bounds, values near 2^53, floating underflow and overflow, NaN, empty groups and duplicates.
4. **Execution evidence:** compare tagged values (`TRUE` is not `1`), post-DML table state and row correlations, and keep agreement labeled as evidence.
5. **Acceptance policy:** done as `proof_registry` (above). `qualify_columns` has moved out of `LEGACY_BASIS` (column qualification, above). Still open: moving `lift_subqueries` out, and `format_sql`, whose layout-only comparison is its own basis.
6. **Column qualification, further:** re-derive the algebraic and SMT provers' own bare-column resolution, take columns from a typed schema record rather than the catalog's name lists, and let the check accept a select whose `SELECT AS STRUCT` fields it can enumerate.

## Audit findings on master

The audit was written against an older checkout. Each finding was re-run on master on 2026-10-03, and the BigQuery behavior it depends on was checked on BigQuery itself.

| Finding | Status |
| --- | --- |
| The lifter reuses a `__lifted_subquery_*` name that a physical table already has, and the prover (which lifts both sides) approves it | Reproduced; tracked as a separate fix |
| A canonical CTE name captures a physical table; CTE references that match only up to case (BigQuery resolves CTE names case-insensitively) | Reproduced; tracked as a separate fix. The CTE checker refuses a prover normalization that captures a name or misses a case-different reference, so a repeat would be `not_proven` |
| `inline_single_use_ctes` replaces a table read by an earlier CTE's body once that body is inlined (found by the CTE checker) | Fixed (above) |
| `1e309 = 1e309` folded to TRUE | Not a bug: BigQuery reads `1e309` as infinity, and `1e309 = 1e309` is TRUE |
| An unused CTE with `LIMIT` disappears before the row-selection guard | Not a wrong answer: BigQuery never evaluates an unused CTE |
| `CURRENT_DATETIME()` and `SESSION_USER()` missing from the volatile node types (sqlglot parses them as `CurrentDatetime` and `SessionUser`, not by name) | Fixed: a change that moves, adds or drops one is now refused, as for `CURRENT_DATE()` |
| A rule and the prover's normalizer share a folding bug | Guarded by the independent predicate check (above) |
| A rule and the prover's normalizer share a CTE reference bug | Guarded by the independent CTE check (above) |
| A rule and the prover's normalizer share a parenthesis or redundant-DISTINCT decision | Guarded by the independent checks in `proof_syntax` (above) |
| A rule and the prover's column resolution share a wrong owner for a bare column (the qualifier a column gets, a select alias read as a source column, a name two sources share) | Guarded by the independent check in `proof_qualify` (above); its design found five cases the rule had wrong, fixed |
| A pipeline is trusted after an unproven step is undone, or by an end-to-end proof | Fixed for safeguarded rules: their unaccepted steps block the pipeline. For other rules, an end-to-end proof still covers a step the prover could not prove on its own, because it proves the output that is actually returned. |
| A `${...}` expression is read as one name | Fixed (above), including expressions inside string literals and layout-only changes next to one |
| A masked string literal read as a fixed string (`status = "${vars.paid}" AND status = 'paid'` looks contradictory), and positional `__sqlx_token_N__` names that stand for different expressions in different models | Fixed: every prover entry point refuses them |
| A step that fails mid-mutation is returned as if unchanged | Already fixed on master: the driver restores the statement it copied before the rule ran |
| SQLX restoration reads backslashes in an expression as regex escapes | Reproduced; tracked as a separate fix |
| SMT proves `SELECT * EXCEPT (b) FROM t` equal to `SELECT * FROM t` | Reproduced; tracked as a separate fix |
| The provers treat type names BigQuery rejects as aliases: `CAST(x AS FLOAT)`, `INT32` and `UUID` were proven equal to `FLOAT64`, `INT64` and `STRING` | Fixed: `type_names.py` refuses them in the structural, SMT, algebraic and SQLSolver provers, the statement proofs, the bounded check, the counterexample searches and rewrite acceptance; it also reads `ARRAY<...>`, `STRUCT<...>`, typed literals and a script's column lists, not only casts ([Type names](#type-names-bigquery-rejects), [Rewrite rules](rewrite-rules.md#inputs-bigquery-would-reject)) |
| SMT and the algebraic prover read `1e-324 < 2e-324` as exact reals (BigQuery: FALSE) | Reproduced; tracked as a separate fix |
| SMT drops `FOR SYSTEM_TIME AS OF` | Already refused on master ("Table.version is not modeled") |
| The synthetic-data comparison treats `TRUE` and `1` as equal | Reproduced; tracked as a separate fix |
| sqlglot's parse is trusted: bitwise operator precedence in BigQuery, comparison chains and `XOR` in MySQL, `~`, `IS` and `INTERSECT` in DuckDB and PostgreSQL, or a dropped `NOT`, give the provers a query nobody wrote | Fixed: every proof is refused when an independent reading of its text disagrees ([parser checks](parser-checks.md)) |
