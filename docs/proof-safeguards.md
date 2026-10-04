# Proof safeguards

A proof is only worth something if it can catch the rewrite's mistake. KumoSQL's structural prover normalizes both sides of a proof with code that rules also use: the subquery lifter, and the predicate folding written once in `cleanup.py` and once in `equivalence.py` (which shared `_literal_compare`). A bug shared by a rule and that normalization makes both sides agree, and the proof approves the wrong output. This page describes the safeguards against that failure, what they guarantee, and what is not covered yet.

## Independent step checks

`kumosql.proof_steps` and `kumosql.proof_ctes` check a step from its before and after statements alone. They import no rule, normalizer, SMT compiler or result comparator, and re-derive every claim. Two families are covered: **predicate cleanup** (`proof_steps`) and **CTE binders** (`proof_ctes`, below). A predicate step is accepted only when all of these hold:

| Assumption recorded on the step | How the checker verifies it |
| --- | --- |
| `statement_frame_preserved` | Everything outside WHERE, HAVING, QUALIFY and JOIN ON is unchanged node for node: projections, tables, joins, grouping, ordering, statement targets. The predicate identity then holds for every row, group and join match. |
| `clause_context_preserved` | A JOIN ON is never dropped. HAVING without GROUP BY is never dropped, because it makes the query an aggregate. QUALIFY is dropped only when its query has a window function; without one, GoogleSQL rejects the original. |
| `opaque_predicate_occurrences_preserved` | Every non-constant predicate (a column, a comparison, a call) keeps each occurrence, so nothing that could raise an error is discarded. Each occurrence is its own variable, so two `RAND()` calls are never one value. |
| `exact_literal_domain` | Only these comparisons become constants: two INT64 literals, a numeric literal compared with the same text (`1e309 = 1e309` is TRUE in BigQuery), or two identical string literals without escapes compared with `=` or `!=`. Anything else stays opaque. |
| `sql_three_valued_logic` | SQLite evaluates the old and new predicate for every TRUE/FALSE/NULL combination of their atoms. They must agree on the whole truth value, not only on whether a row passes. |

A change nested inside an atom, such as `x IN (SELECT y FROM u WHERE TRUE)`, is checked the same way one level down. Any failure rejects the step, including an error inside the checker or more than `MAX_PREDICATE_ATOMS` (8) atoms in one clause. There is no sampling fallback. A disagreement reports the atom values that show it (`counterexample`).

The checkers run in two places:

- **Rule acceptance.** `rewrite.INDEPENDENT_CHECK_FAMILIES` maps `remove_trivial_predicates` to the predicate family and `remove_unused_ctes`, `inline_single_use_ctes` and `deduplicate_ctes` to the CTE family. The acceptance layer owns this table and keys it by rule name, so neither the rule nor an override passed to `apply_rule` can opt out. The checks come from the actual input and output: each changed statement of SQL, a script or a SQLX section is checked. They don't come from records the rule supplies, so nothing can be missing or forged. A step is `proven` only when the prover and the independent checker both accept it. Each check appears in `verification.checks` as kind `independent_check`, with its family, statement (and SQLX section), assumptions, cases checked and any counterexample. The same checks are in `verification.proof_checks` as `StepCheck` records, and the CLI and the UI's verification report show them.
- **Inside the structural prover.** `equivalence._prepare_query` records its own `_normalize_predicates` transition and has the predicate checker re-derive it before grouping and CTE normalization run. It also records the whole CTE normalization (renaming to canonical names, reordering, merging identical bodies, dropping unreferenced CTEs) and has the CTE checker re-derive it. A refused transition makes `prove_equivalent` return `not_proven` ("the independent check of a normalization step refused it"). Accepted transitions are listed in `EquivalenceResult.proof_checks`.

**Pipelines.** If an independent check refuses any step of `apply_rules`, or a safeguarded rule's step is not `proven` for any other reason, the pipeline is `unproven`. This holds even when a later step restores the original text or the end-to-end result could be proven, because that end-to-end proof uses the prover the checker guards.

Tests corrupt the rule and the prover's normalizer the same way (`tests/test_proof_steps.py`), and the checker still refuses the result. Matching normalization alone would have certified it.

## CTE binders

A CTE rule is only as good as its answer to one question: which relation does this name in FROM mean? A name can be a CTE, a table with the same name (a CTE is visible only to the CTEs after it, never to itself or an earlier one), a nested WITH's CTE that shadows an outer one, or the same name with different case (BigQuery reads names case-insensitively). A mistake in how a rule or the prover's normalizer finds references would be shared by both, so both sides of a proof would agree on the wrong answer.

`proof_ctes` answers the question once, from the statements alone, and compares by **expansion**: it replaces every CTE reference with the CTE's definition (a subquery with the reference's alias, or the CTE's name when there is none) and drops every WITH clause. The expansions of the before and after statements must be identical node for node, apart from the quoting of plain names. Renaming, reordering, merging, inlining and dropping a CTE nobody reads leave the expansion unchanged. Capturing a name, pointing a reference at another definition, dropping a CTE something still reads, or changing anything else alongside changes it, and the step is refused with the first node that differs.

Assumptions recorded on each step: `cte_scope_resolution`, `reference_equals_inline_subquery`, `volatile_bodies_refused`, `unreferenced_ctes_have_no_effect`, `expanded_statements_identical`. A step is **refused, never guessed**, when:

- a WITH is recursive, repeats a name (up to case), or carries a MATERIALIZED hint;
- a name that matches a visible CTE appears anywhere other than as a FROM or JOIN relation, or that relation carries more than an alias (a snapshot, a sample, a pivot);
- a volatile call (`RAND()`, `GENERATE_UUID()`, the current time) sits in a CTE that the expansion copies more than once, because merging, inlining or splitting its readers changes how many values are drawn;
- a CTE name disappears while a bare identifier spelled like it is still an argument of a function (a table passed by name), because that read cannot be tracked;
- the expansion would copy more than `MAX_EXPANDED_NODES` (50,000) nodes.

A one-part name in FROM means the CTE even when it is also another FROM item's alias. This was checked on BigQuery (`FROM a CROSS JOIN a AS b`, and a subquery reading `a AS b` under an outer `FROM a`, both read the CTE `a`), so the checker does not refuse those, unlike sqlglot 26's parser, which reads a repeated name as an array path.

**What it found.** On its first run over a corpus of CTE queries the checker refused `inline_single_use_ctes` on `WITH b AS (SELECT * FROM a), a AS (SELECT 1 x) SELECT * FROM b`. In that query the `a` inside `b` is a table, because `a` is defined after `b`. The rule inlined `b` first, which moved that read into the main query, where `a` is the CTE, and then inlined `a` into it. The rule now leaves a WITH clause with a forward reference alone (`cte_dependency_errors`, as the other CTE rules already did). The test is in `tests/test_proof_ctes.py`.

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

- Only predicate cleanup and CTE rewrites are independently checked. Other rules (parentheses, lifting, formatting) and the SMT-based prover keep their existing basis: the normalized-AST comparison, the layout comparison and the solver. The lifter that turns FROM subqueries into CTEs is not checked yet, and its `__lifted_subquery_*` name heuristic stays.
- The CTE check is exact on the expanded tree. A rewrite that changes a CTE and the query together in a way that is still equivalent (a filter moved into a CTE, say) is refused here; those rules are proven by the prover alone and are not in the CTE family.
- An unread CTE is assumed to have no effect, as BigQuery never evaluates one (checked 2026-10-03); the checker does not look for errors that dropping it would hide.
- sqlglot's parser stays inside the trusted boundary. The checker does not establish BigQuery validity, schemas or parser correctness.
- SQLite is used only for the Boolean and literal model, never to run BigQuery SQL.

## Extending it

The architecture audit (October 2026) proposes the next families, in order:

1. **CTE and relation rewrites:** done for the three CTE rules and the prover's CTE normalization (above), by expansion. Still open: the lifter (check its output expands back to its input, and replace the `__lifted_subquery_*` prefix heuristic with recorded binder provenance) and rules that rewrite relations without CTEs.
2. **SMT compiler:** reject every AST argument the compiler does not model, so unmodeled syntax (`* EXCEPT`, a table snapshot) cannot vanish from the formula. Record typed column identities and coercions, and treat solver timeouts and checker failures as unresolved.
3. **Typed values:** check integer bounds, values near 2^53, floating underflow and overflow, NaN, empty groups and duplicates.
4. **Execution evidence:** compare tagged values (`TRUE` is not `1`), post-DML table state and row correlations, and keep agreement labeled as evidence.
5. **Acceptance policy:** move each family to a reviewed checker registry, and retire its legacy path only after its corpus and fault-injection tests pass.

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
| A pipeline is trusted after an unproven step is undone, or by an end-to-end proof | Fixed for safeguarded rules: their unaccepted steps block the pipeline. For other rules, an end-to-end proof still covers a step the prover could not prove on its own, because it proves the output that is actually returned. |
| A `${...}` expression is read as one name | Fixed (above), including expressions inside string literals and layout-only changes next to one |
| A masked string literal read as a fixed string (`status = "${vars.paid}" AND status = 'paid'` looks contradictory), and positional `__sqlx_token_N__` names that stand for different expressions in different models | Fixed: every prover entry point refuses them |
| A step that fails mid-mutation is returned as if unchanged | Already fixed on master: the driver restores the statement it copied before the rule ran |
| SQLX restoration reads backslashes in an expression as regex escapes | Reproduced; tracked as a separate fix |
| SMT proves `SELECT * EXCEPT (b) FROM t` equal to `SELECT * FROM t` | Reproduced; tracked as a separate fix |
| SMT and the algebraic prover read `1e-324 < 2e-324` as exact reals (BigQuery: FALSE) | Reproduced; tracked as a separate fix |
| SMT drops `FOR SYSTEM_TIME AS OF` | Already refused on master ("Table.version is not modeled") |
| The synthetic-data comparison treats `TRUE` and `1` as equal | Reproduced; tracked as a separate fix |
