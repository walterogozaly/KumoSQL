# Proof safeguards

A proof is only worth something if it can catch the rewrite's mistake. KumoSQL's structural prover normalizes both sides of a proof with code that rules also use: the subquery lifter, and the predicate folding written once in `cleanup.py` and once in `equivalence.py` (which shared `_literal_compare`). A bug shared by a rule and that normalization makes both sides agree, and the proof approves the wrong output. This page describes the safeguards against that failure, what they guarantee, and what is not covered yet.

## Independent step checks

`kumosql.proof_steps`, `kumosql.proof_ctes`, `kumosql.proof_syntax` and `kumosql.proof_lift` check a step from its before and after statements alone. They import no rule, normalizer, lifter, SMT compiler or result comparator, and re-derive every claim. Five families are covered (family names `predicate_cleanup`, `cte_binders`, `parenthesization`, `redundant_distinct`, `subquery_lift`): **predicate cleanup** (`proof_steps`), **CTE binders** (`proof_ctes`, below), **parenthesization** and **redundant DISTINCT** (`proof_syntax`, below), and **subquery lifting** (`proof_lift`, below). `kumosql.proof_registry` is the one reviewed table of them (see Acceptance registry). A predicate step is accepted only when all of these hold:

| Assumption recorded on the step | How the checker verifies it |
| --- | --- |
| `statement_frame_preserved` | Everything outside WHERE, HAVING, QUALIFY and JOIN ON is unchanged node for node: projections, tables, joins, grouping, ordering, statement targets. The predicate identity then holds for every row, group and join match. |
| `clause_context_preserved` | A JOIN ON is never dropped. HAVING without GROUP BY is never dropped, because it makes the query an aggregate. QUALIFY is dropped only when its query has a window function; without one, GoogleSQL rejects the original. |
| `opaque_predicate_occurrences_preserved` | Every non-constant predicate (a column, a comparison, a call) keeps each occurrence, so nothing that could raise an error is discarded. Each occurrence is its own variable, so two `RAND()` calls are never one value. |
| `exact_literal_domain` | Only these comparisons become constants: two INT64 literals, a numeric literal compared with the same text (`1e309 = 1e309` is TRUE in BigQuery), or two identical string literals without escapes compared with `=` or `!=`. Anything else stays opaque. |
| `sql_three_valued_logic` | SQLite evaluates the old and new predicate for every TRUE/FALSE/NULL combination of their atoms. They must agree on the whole truth value, not only on whether a row passes. |

A change nested inside an atom, such as `x IN (SELECT y FROM u WHERE TRUE)`, is checked the same way one level down. Any failure rejects the step, including an error inside the checker or more than `MAX_PREDICATE_ATOMS` (8) atoms in one clause. There is no sampling fallback. A disagreement reports the atom values that show it (`counterexample`).

The checkers run in two places:

- **Rule acceptance.** `rewrite.INDEPENDENT_CHECK_FAMILIES` (the registry's `RULE_FAMILIES`) maps `remove_trivial_predicates` to the predicate family, `remove_unused_ctes`, `inline_single_use_ctes` and `deduplicate_ctes` to the CTE family, `remove_redundant_parentheses` to the parenthesization family, `remove_redundant_distinct` to the DISTINCT family and `lift_subqueries` to the lift family. The acceptance layer owns this table and keys it by rule name, so neither the rule nor an override passed to `apply_rule` can opt out. The checks come from the actual input and output: each changed statement of SQL, a script or a SQLX section is checked. They don't come from records the rule supplies, so nothing can be missing or forged. A step is `proven` only when the prover and the independent checker both accept it. Each check appears in `verification.checks` as kind `independent_check`, with its family, statement (and SQLX section), assumptions, cases checked and any counterexample. The same checks are in `verification.proof_checks` as `StepCheck` records, and the CLI and the UI's verification report show them.
- **Inside the structural prover.** `equivalence._prepare_query` records its own `_normalize_predicates` transition and has the predicate checker re-derive it before grouping and CTE normalization run. It also records the whole CTE normalization (renaming to canonical names, reordering, merging identical bodies, dropping unreferenced CTEs) and has the CTE checker re-derive it, and does the same for its grouping-parenthesis stripping and redundant-DISTINCT dropping (recorded as `strip_grouping_parens` and `drop_redundant_distinct`). Before any of those, `_prepare_query` has the lift check re-derive what the lifter did to the query's text (recorded as `lift_subqueries`). A refused transition makes `prove_equivalent` return `not_proven` ("the independent check of a normalization step refused it"). Accepted transitions are listed in `EquivalenceResult.proof_checks`.

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

## Subquery lifting

`lift_subqueries` moves every FROM or JOIN subquery into a CTE with a generated name (`__lifted_subquery_001`, ...). The prover lifts both sides of a proof with the same lifter, so a lifter bug (a name a physical table already has, a name captured by a nested WITH, a body that read the query around it, a subquery dropped or changed) was approved by both sides. `proof_lift` (family `subquery_lift`) answers from the step's before and after text alone and imports no rule, lifter or prover. It reads both texts again and requires all of these:

| Assumption recorded on the step | How the checker verifies it |
| --- | --- |
| `lifted_names_fresh` | The lifted CTEs are the CTEs of the after statement whose name occurs nowhere in the before statement (a table, alias, column or CTE spelled the same, in any case), each defined once, none with a column list. A generated name a table already has is therefore not a lifted CTE, and the extra CTE it makes fails the next row. |
| `lifted_ctes_restore_exactly` | Writing each lifted CTE back as a derived table (`(body) AS alias`, carrying its PIVOT, sample and lateral) and dropping those CTEs reproduces the before statement node for node. Every original CTE and every other node is unchanged, and each lifted CTE is read exactly once, by a FROM or JOIN relation. A dropped or changed subquery, a changed alias, a lost PIVOT, a CTE read twice (two subqueries deduplicated) or not at all, and any other edit fail here. |
| `cte_scope_resolution` | `proof_ctes`'s expansion replaces every CTE reference by its definition under BigQuery's scope rules, on both statements, and the trees must be identical. A body that read a name a nested WITH defines but now reads the table or outer CTE of that name, a lifted CTE defined after the CTE that reads it, and a CTE placed where its reference cannot see it differ. An unaliased derived table becomes a relation named like its CTE, which is not a change because that name occurs nowhere in the before statement. |
| `lifted_bodies_closed` | Each lifted body is checked where it stood: no qualified column in it may name a relation of an enclosing query that the body does not define, because a CTE cannot see the query that reads it. Unqualified columns cannot be resolved without a schema and are not checked. |
| `volatile_bodies_not_repeated` | A lifted body that calls `RAND()`, `GENERATE_UUID()` or the current time is refused when its reference sits inside an expression subquery (scalar, EXISTS, IN, ARRAY), where a derived table is evaluated per outer row and a CTE need not be. |

Both texts are printed and read once by sqlglot before they are compared, because sqlglot's printer rewrites some constructs and reads the result as a different tree (`INT` as `INT64`, `<>` as `!=`, `DISTINCT ON` as a window function, a CTE column list as projection aliases). Both sides get the same print, so only what the lifter changed remains. The check trusts sqlglot's printer to keep meaning, the way every proof trusts its parser. Pipe syntax needs nothing special: sqlglot translates it to CTEs when it parses the before text, and the lifter works on that translation. `proof_ctes` cannot expand a recursive CTE, so in a statement with a recursive WITH both sides are expanded as if every WITH were sequential, which is the real reading except for a recursive WITH's own names (a CTE may read itself, and perhaps CTEs after it) and for a body evaluated once per iteration. A lifted body is therefore refused if it calls a volatile function there, or reads a CTE of a recursive WITH other than the ones before it in its own WITH clause (a recursive WITH inside the body moves with it). A step is refused, never guessed, for a MATERIALIZED hint, a duplicate CTE name, a CTE name read outside FROM and JOIN, a reference carrying a snapshot, anything `proof_ctes` cannot follow, and any error in the checker.

**What it found.** Run over the lifter on the 2,894 queries in the test fixtures that have a FROM or JOIN subquery (SQL from the benchmark corpora; no label or score was consulted), the first version of the checker refused 46 rule outputs and 325 prover lifts. Four causes:

- **The prover lifted correlated subqueries and subqueries that read a name a nested WITH defines** (80 queries; the rule had stopped doing so, the prover's `rewrite_pipe_syntax=True` mode had not). Its normal form moved `t.a` out of the query that defines `t`, or pointed a nested WITH's name at a table. This is how the prover proved `SELECT * FROM a, (SELECT * FROM b WHERE b.x = a.x) AS s` equal to its CTE form (a known false proof, now a passing test in `tests/test_sqlfluff_refusals_bench.py`). The prover's lift now leaves such a subquery in place, as the rule does, and `_prepare_query` accepts the `correlated_subquery_kept` diagnostic.
- **The lifter dropped the column list of `(...) AS t (a, b)`** (about 35 queries; sqlglot reads it, BigQuery does not have it). It keeps the list now.
- **The lifter made a CTE of `FROM (t)` and `FROM (VALUES ...)`** (about 45 queries), which is `WITH l AS (t) ...`, not SQL. It lifts a query only now.
- **sqlglot's printing** (251 prover lifts, before both texts were printed the same way).

A fifth cause was the checker's own: it refused every statement that holds a recursive WITH (2 of the corpus, and the sqlfluff fixture `with_recursive_fail_no_fix`, which the refusal eval proves), which the recursive-WITH reading above now handles. After the fixes the checker accepts 2,506 changed statements of the rule and 2,441 lifts of the prover, and refuses none. The corpora's queries were read while fixing, so any eval over them is tuned on test for this change, though the fixes are general.

## Parentheses and DISTINCT

Two small rewrites had the same shape of risk: the rule and the prover's normalizer each decide for themselves which parentheses are meaningless (`cleanup._redundant`, `equivalence._paren_is_semantic`), and the DISTINCT rule and the normalizer share `distinct_safety.distinct_is_redundant`. `proof_syntax` re-derives both decisions from the step's SQL text (never from the caller's in-memory trees, which can print differently from how they read: `(t).x` printed as `t.x`).

- **`parenthesization`** (assumptions `parse_structure_unchanged`, `parentheses_carry_no_other_meaning`). The after statement must have the same tree as the before statement once every parenthesis node is removed from both and each chain of `AND` (or of `OR`) is read left to right, because both are associative in three-valued logic and keep their operand order (`a AND (b AND c)` to `a AND b AND c` is accepted; other operators keep their grouping, since `a + (b + c)` can differ from `(a + b) + c` in floating point and on overflow). Equal stripped trees mean every operator groups the same way, so `(a OR b) AND c` to `a OR b AND c`, `-(-x)` to `--x` (a comment) and `(t).x` to `t.x` all fail. Output column names follow the expression, not its parentheses (checked on BigQuery). It trusts sqlglot to read operator precedence as BigQuery does.
- **`redundant_distinct`** (assumptions `statement_frame_preserved`, `group_keys_are_plain_columns`, `group_keys_projected_unchanged`, `distinct_and_group_by_share_equality`). The after statement must equal the before statement with DISTINCT cleared on some SELECTs, and each cleared SELECT must have a plain GROUP BY (columns only: no ROLLUP, CUBE, GROUPING SETS, ALL or totals) whose keys are all projected unchanged. A select alias spelled like a key counts only when it projects that same column, because BigQuery's GROUP BY prefers the alias (checked on BigQuery). DISTINCT ON and DISTINCT without GROUP BY are refused.

## Acceptance registry

`kumosql.proof_registry` holds `FAMILIES` (each family's name, assumptions, checker and summary), `RULE_FAMILIES` (rule name to family; the acceptance layer reads it, so a rule or an override cannot opt out) and `LEGACY_BASIS` (every rule with no independent checker yet, and what its proof rests on: `format_sql`, `qualify_columns`). `tests/test_proof_registry.py` fails when a rewrite rule is registered but is in neither table, so adding a rule forces a choice between an independent checker and a named legacy basis, and when a family accepts a step with foreign assumptions. A rule moves from legacy to independent only after the checker has its corpus and fault-injection tests; moving one back is a regression.

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

- Predicate cleanup, CTE rewrites, parentheses, redundant DISTINCT and subquery lifting are independently checked. The other rules (`qualify_columns`, `format_sql`; see `LEGACY_BASIS`) and the SMT-based prover keep their existing basis: the normalized-AST comparison, the layout comparison and the solver. The prover's refusal of a query that reads a table named like a generated CTE (`__lifted_subquery_*`, `__canonical_cte_*`) stays as a second guard; the lift check does not depend on it.
- The lift check cannot see an unqualified column that a lifted body reads from the query around it, as that needs a schema (the lifter has the same limit). It reads a recursive WITH as a sequential one (see above), so it refuses a lift that reads a recursive WITH's names from a place it does not see them the same way. It trusts sqlglot's printer and parser.
- The DISTINCT check is a sufficient condition, not a complete one: a DISTINCT that is redundant for another reason (a unique key, a one-row table) is not accepted by this checker, so a rule that removes it is `unproven`.
- The CTE check is exact on the expanded tree. A rewrite that changes a CTE and the query together in a way that is still equivalent (a filter moved into a CTE, say) is refused here; those rules are proven by the prover alone and are not in the CTE family.
- An unread CTE is assumed to have no effect, as BigQuery never evaluates one (checked 2026-10-03); the checker does not look for errors that dropping it would hide.
- sqlglot's parse is checked separately, not by this checker: every proof is refused when an independent reading of its text disagrees with sqlglot's ([parser checks](parser-checks.md)). This checker still does not establish BigQuery validity or schemas.
- SQLite is used only for the Boolean and literal model, never to run BigQuery SQL.

## Extending it

The architecture audit (October 2026) proposes the next families, in order:

1. **CTE and relation rewrites:** done for the three CTE rules, the prover's CTE normalization and the lifter (above), by expansion. Still open: rules that rewrite relations without CTEs, and expanding recursive WITH clauses exactly.
2. **SMT compiler:** reject every AST argument the compiler does not model, so unmodeled syntax (`* EXCEPT`, a table snapshot) cannot vanish from the formula. Record typed column identities and coercions, and treat solver timeouts and checker failures as unresolved.
3. **Typed values:** check integer bounds, values near 2^53, floating underflow and overflow, NaN, empty groups and duplicates.
4. **Execution evidence:** compare tagged values (`TRUE` is not `1`), post-DML table state and row correlations, and keep agreement labeled as evidence.
5. **Acceptance policy:** done as `proof_registry` (above). Still open: moving `qualify_columns` (and `format_sql`, which compares layout only) out of `LEGACY_BASIS`.

## Audit findings on master

The audit was written against an older checkout. Each finding was re-run on master on 2026-10-03, and the BigQuery behavior it depends on was checked on BigQuery itself.

| Finding | Status |
| --- | --- |
| The lifter reuses a `__lifted_subquery_*` name that a physical table already has, and the prover (which lifts both sides) approves it | Guarded by the independent lift check (above): a CTE named like anything in the before statement is not a lifted CTE, so the step is refused |
| The prover's lift moves a correlated derived table, or one that reads a nested WITH's name, into a top-level CTE where it reads something else; the rule had stopped doing this, the prover had not | Fixed (found by the lift check): the prover's lift keeps such a subquery in place, and the check refuses a lifter that does not |
| The lifter drops the column list of `(...) AS t (a, b)` and makes a CTE of `FROM (t)` or `FROM (VALUES ...)` | Fixed (found by the lift check) |
| A canonical CTE name captures a physical table; CTE references that match only up to case (BigQuery resolves CTE names case-insensitively) | Reproduced; tracked as a separate fix. The CTE checker refuses a prover normalization that captures a name or misses a case-different reference, so a repeat would be `not_proven` |
| `inline_single_use_ctes` replaces a table read by an earlier CTE's body once that body is inlined (found by the CTE checker) | Fixed (above) |
| `1e309 = 1e309` folded to TRUE | Not a bug: BigQuery reads `1e309` as infinity, and `1e309 = 1e309` is TRUE |
| An unused CTE with `LIMIT` disappears before the row-selection guard | Not a wrong answer: BigQuery never evaluates an unused CTE |
| `CURRENT_DATETIME()` and `SESSION_USER()` missing from the volatile node types (sqlglot parses them as `CurrentDatetime` and `SessionUser`, not by name) | Fixed: a change that moves, adds or drops one is now refused, as for `CURRENT_DATE()` |
| A rule and the prover's normalizer share a folding bug | Guarded by the independent predicate check (above) |
| A rule and the prover's normalizer share a CTE reference bug | Guarded by the independent CTE check (above) |
| A rule and the prover's normalizer share a parenthesis or redundant-DISTINCT decision | Guarded by the independent checks in `proof_syntax` (above) |
| A pipeline is trusted after an unproven step is undone, or by an end-to-end proof | Fixed for safeguarded rules: their unaccepted steps block the pipeline. For other rules, an end-to-end proof still covers a step the prover could not prove on its own, because it proves the output that is actually returned. |
| A `${...}` expression is read as one name | Fixed (above), including expressions inside string literals and layout-only changes next to one |
| A masked string literal read as a fixed string (`status = "${vars.paid}" AND status = 'paid'` looks contradictory), and positional `__sqlx_token_N__` names that stand for different expressions in different models | Fixed: every prover entry point refuses them |
| A step that fails mid-mutation is returned as if unchanged | Already fixed on master: the driver restores the statement it copied before the rule ran |
| SQLX restoration reads backslashes in an expression as regex escapes | Reproduced; tracked as a separate fix |
| SMT proves `SELECT * EXCEPT (b) FROM t` equal to `SELECT * FROM t` | Reproduced; tracked as a separate fix |
| The provers treat type names BigQuery rejects as aliases: `CAST(x AS FLOAT)`, `INT32` and `UUID` were proven equal to `FLOAT64`, `INT64` and `STRING` | Fixed: `type_names.py` refuses casts to names BigQuery does not have in the structural, SMT and algebraic provers and in rewrite acceptance (see [Rewrite rules](rewrite-rules.md#inputs-bigquery-would-reject)) |
| SMT and the algebraic prover read `1e-324 < 2e-324` as exact reals (BigQuery: FALSE) | Reproduced; tracked as a separate fix |
| SMT drops `FOR SYSTEM_TIME AS OF` | Already refused on master ("Table.version is not modeled") |
| The synthetic-data comparison treats `TRUE` and `1` as equal | Reproduced; tracked as a separate fix |
| sqlglot's parse is trusted: bitwise operator precedence in BigQuery, comparison chains and `XOR` in MySQL, `~`, `IS` and `INTERSECT` in DuckDB and PostgreSQL, or a dropped `NOT`, give the provers a query nobody wrote | Fixed: every proof is refused when an independent reading of its text disagrees ([parser checks](parser-checks.md)) |
