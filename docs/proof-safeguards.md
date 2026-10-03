# Proof safeguards

A proof is only worth something if it can catch the rewrite's mistake. KumoSQL's structural prover normalizes both sides of a proof with code that rules also use: the subquery lifter, and the predicate folding written once in `cleanup.py` and once in `equivalence.py` (which shared `_literal_compare`). A bug shared by a rule and that normalization makes both sides agree, and the proof approves the wrong output. This page describes the safeguards against that failure, what they guarantee, and what is not covered yet.

## Independent step checks

`kumosql.proof_steps` checks a step from its before and after statements alone. It imports no rule, normalizer, SMT compiler or result comparator, and re-derives every claim. The first family it covers is **predicate cleanup**. A step is accepted only when all of these hold:

| Assumption recorded on the step | How the checker verifies it |
| --- | --- |
| `statement_frame_preserved` | Everything outside WHERE, HAVING, QUALIFY and JOIN ON is unchanged node for node: projections, tables, joins, grouping, ordering, statement targets. The predicate identity then holds for every row, group and join match. |
| `clause_context_preserved` | A JOIN ON is never dropped. HAVING without GROUP BY is never dropped, because it makes the query an aggregate. QUALIFY is dropped only when its query has a window function; without one, GoogleSQL rejects the original. |
| `opaque_predicate_occurrences_preserved` | Every non-constant predicate (a column, a comparison, a call) keeps each occurrence, so nothing that could raise an error is discarded. Each occurrence is its own variable, so two `RAND()` calls are never one value. |
| `exact_literal_domain` | Only these comparisons become constants: two INT64 literals, a numeric literal compared with the same text (`1e309 = 1e309` is TRUE in BigQuery), or two identical string literals without escapes compared with `=` or `!=`. Anything else stays opaque. |
| `sql_three_valued_logic` | SQLite evaluates the old and new predicate for every TRUE/FALSE/NULL combination of their atoms. They must agree on the whole truth value, not only on whether a row passes. |

A change nested inside an atom, such as `x IN (SELECT y FROM u WHERE TRUE)`, is checked the same way one level down. Any failure rejects the step, including an error inside the checker or more than `MAX_PREDICATE_ATOMS` (8) atoms in one clause. There is no sampling fallback. A disagreement reports the atom values that show it (`counterexample`).

The checker runs in two places:

- **Rule acceptance.** `rewrite.INDEPENDENT_CHECK_FAMILIES` maps `remove_trivial_predicates` to the family. The acceptance layer owns this table and keys it by rule name, so neither the rule nor an override passed to `apply_rule` can opt out. The checks come from the actual input and output: each changed statement of SQL, a script or a SQLX section is checked. They don't come from records the rule supplies, so nothing can be missing or forged. A step is `proven` only when the prover and the independent checker both accept it. Each check appears in `verification.checks` as kind `independent_check`, with its family, statement (and SQLX section), assumptions, cases checked and any counterexample. The same checks are in `verification.proof_checks` as `StepCheck` records, and the CLI and the UI's verification report show them.
- **Inside the structural prover.** `equivalence._prepare_query` records its own `_normalize_predicates` transition and has the checker re-derive it before grouping and CTE normalization run. A refused transition makes `prove_equivalent` return `not_proven` ("the independent check of predicate normalization refused a step"). Accepted transitions are listed in `EquivalenceResult.proof_checks`.

**Pipelines.** If an independent check refuses any step of `apply_rules`, or a safeguarded rule's step is not `proven` for any other reason, the pipeline is `unproven`. This holds even when a later step restores the original text or the end-to-end result could be proven, because that end-to-end proof uses the prover the checker guards.

Tests corrupt the rule and the prover's normalizer the same way (`tests/test_proof_steps.py`), and the checker still refuses the result. Matching normalization alone would have certified it.

## Dataform expressions

To prove a SQLX rewrite, each `${...}` is masked with a name derived from its text. That is faithful only for an expression that expands to one relation name: `self()`, or a single `ref(...)` or `resolve(...)` call with any arguments (`${ref({schema: vars.S, name: "t"})}` too), as long as the call ends the expression and its arguments hold no template literal, comment, `/` or backslash. Any other expression, such as `${when(incremental(), "AND ts > ...")}`, `${"x OR y"}`, `${ref("t") + " OR x"}` or a JavaScript constant, can expand to several operators, a whole clause, nothing at all, or a reference to a CTE the masked text never mentions. So a changed statement that holds one outside a string literal is `unproven`, and the reason says to compile the SQLX first. Statements the rewrite left alone are unaffected. An expression inside a string literal (`'${constants.START}'`) is read as part of the literal's text (`sqlx_fragments.py`).

Before this check, these rewrites were labeled `proven`:

| Rule | Input | Output |
| --- | --- | --- |
| `remove_redundant_parentheses` | `WHERE z AND (${"x OR y"})` | `WHERE z AND ${"x OR y"}`, which reads as `(z AND x) OR y` |
| `remove_trivial_predicates` | `WHERE TRUE ${when(incremental(), "AND b > 1")}` | `WHERE ${...}`, which is invalid SQL either way |
| `remove_unused_ctes` | a CTE read only inside `${"x IN (SELECT x FROM c)"}` | the CTE deleted |
| `inline_single_use_ctes` | the same CTE | inlined, leaving the expression's reference dangling |

## Limits

- Only predicate cleanup is independently checked. Other rules (CTE movement and deduplication, parentheses, lifting, formatting) and the SMT-based prover keep their existing basis: the normalized-AST comparison, the layout comparison and the solver.
- sqlglot's parser stays inside the trusted boundary. The checker does not establish BigQuery validity, schemas or parser correctness.
- SQLite is used only for the Boolean and literal model, never to run BigQuery SQL.

## Extending it

The architecture audit (October 2026) proposes the next families, in order:

1. **CTE and relation rewrites:** record scope-resolved binders before and after, free references, source identities (including snapshots), alias and projection maps, and occurrence counts. Check capture avoidance and scope preservation independently, and replace the `__lifted_subquery_*` prefix heuristic with recorded binder provenance.
2. **SMT compiler:** reject every AST argument the compiler does not model, so unmodeled syntax (`* EXCEPT`, a table snapshot) cannot vanish from the formula. Record typed column identities and coercions, and treat solver timeouts and checker failures as unresolved.
3. **Typed values:** check integer bounds, values near 2^53, floating underflow and overflow, NaN, empty groups and duplicates.
4. **Execution evidence:** compare tagged values (`TRUE` is not `1`), post-DML table state and row correlations, and keep agreement labeled as evidence.
5. **Acceptance policy:** move each family to a reviewed checker registry, and retire its legacy path only after its corpus and fault-injection tests pass.

## Audit findings on master

The audit was written against an older checkout. Each finding was re-run on master on 2026-10-03, and the BigQuery behavior it depends on was checked on BigQuery itself.

| Finding | Status |
| --- | --- |
| The lifter reuses a `__lifted_subquery_*` name that a physical table already has, and the prover (which lifts both sides) approves it | Reproduced; tracked as a separate fix |
| A canonical CTE name captures a physical table; CTE references that match only up to case (BigQuery resolves CTE names case-insensitively) | Reproduced; tracked as a separate fix |
| `1e309 = 1e309` folded to TRUE | Not a bug: BigQuery reads `1e309` as infinity, and `1e309 = 1e309` is TRUE |
| An unused CTE with `LIMIT` disappears before the row-selection guard | Not a wrong answer: BigQuery never evaluates an unused CTE |
| `CURRENT_DATETIME()` and `SESSION_USER()` missing from the volatile node types (sqlglot parses them as `CurrentDatetime` and `SessionUser`, not by name) | Fixed: a change that moves, adds or drops one is now refused, as for `CURRENT_DATE()` |
| A rule and the prover's normalizer share a folding bug | Guarded by the independent predicate check (above) |
| A pipeline is trusted after an unproven step is undone, or by an end-to-end proof | Fixed for safeguarded rules: their unaccepted steps block the pipeline. For other rules, an end-to-end proof still covers a step the prover could not prove on its own, because it proves the output that is actually returned. |
| A `${...}` expression is read as one name | Fixed (above) |
| A step that fails mid-mutation is returned as if unchanged | Already fixed on master: the driver restores the statement it copied before the rule ran |
| SQLX restoration reads backslashes in an expression as regex escapes | Reproduced; tracked as a separate fix |
| SMT proves `SELECT * EXCEPT (b) FROM t` equal to `SELECT * FROM t` | Reproduced; tracked as a separate fix |
| SMT and the algebraic prover read `1e-324 < 2e-324` as exact reals (BigQuery: FALSE) | Reproduced; tracked as a separate fix |
| SMT drops `FOR SYSTEM_TIME AS OF` | Already refused on master ("Table.version is not modeled") |
| The synthetic-data comparison treats `TRUE` and `1` as equal | Reproduced; tracked as a separate fix |
