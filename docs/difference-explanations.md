# Difference explanations: "equivalent except when P"

[Plain-language version](../docs_simple/difference-explanations.md)

When two queries are not equivalent, a counterexample database shows that they differ but not why. A **difference explanation** names the rows they disagree on as a short SQL predicate `P` that KumoSQL has checked, so a reviewer can decide (accept the change, or add a NOT NULL assertion) without reading a database dump.

```text
SELECT id FROM t WHERE status = 'a'      vs      SELECT id FROM t WHERE status = 'a' OR status IS NULL
not_equivalent
  except when: status IS NULL
  exact: yes
```

## What the predicate means

`P` is a predicate over the columns of one table. It comes with four fields:

| Field | Meaning |
| --- | --- |
| `sql` | The predicate, for example `status IS NULL` |
| `atoms` | The comparisons it is built from, as SQL text |
| `tables` | The lower-case tables whose columns it reads |
| `exact` | `true` when the two queries disagree exactly where `P` holds; `false` when `P` covers every disagreement but the queries may still agree on some rows where it holds |

The claim is "equivalent except when P": once the rows where `P` holds are filtered out of every table the queries read, the two queries are proved equivalent. A predicate is shown only after it has passed two checks, which live in `src/kumosql/difference_explanation.py`:

- a witness database with a row satisfying `P` on which the queries really differ, replayed on DuckDB through the same guards as other executed counterexamples;
- a proof, by both the algebraic and the SMT prover, that the pair is equivalent when each table is filtered by `P`.

When no predicate passes both checks, nothing is shown. The absence of a predicate says nothing about the pair: the queries are still different, and the predicate search may simply not have found a short description.

## Where it appears

The explanation is opt-in everywhere, so every existing output and eval answer is unchanged until it is asked for.

- **API.** `POST /api/prove-queries` accepts `"explain": true`. A refuted pair then carries `except_when: {sql, atoms, tables, exact}` next to its `counterexample`. In Python, `pipeline_equivalence.prove_queries(left, right, explain=True)`. Only a pair whose final status is `not_equivalent` is searched; an equivalent, conditional or unknown pair never gets the field.
- **Command line.** `python -m kumosql prove-sql-equivalent left.sql right.sql --explain-difference` prints `except when: <predicate>` and `exact: yes|no` after the verdict when the pair is not proven. `python -m kumosql prove-sql-smt left.sql right.sql --explain-difference` adds an `except_when` object to its JSON when the pair is refuted. The exit codes do not change.
- **Settings → Solver → Compare queries.** The page asks for the explanation and, when there is one, shows **Equivalent except when `<predicate>`** above the counterexample.
- **Change reports.** `build_change_report(..., explain_differences=True)` and `python -m kumosql change-report BASE HEAD --explain-differences` add `except_when` to each modified query model whose rewrite is unproven and has a verified predicate. The change report page and the review comment of `ci-check` ignore the field unless it is present; the page shows it under the model's behavior.

## Limits

- A predicate is a description of where the queries differ on the databases KumoSQL tried and proved over, not a statement about the data in the warehouse. Whether any row satisfies `P` there is for the reader to check, for example with `SELECT COUNT(*) FROM t WHERE <P>`.
- `exact: false` means `P` is sufficient but not necessary: it may be wider than the real difference.
- The predicate describes one table at a time. A difference that only a condition across several tables describes (`a.x > b.y`) has no predicate.
- The search has a time limit (the solver time limit from Settings in the API and the report, `--timeout-ms` in `prove-sql-smt`). A search that runs out of time shows nothing, never a guess.
- It costs extra solver time per refuted pair, which is why change reports and the CLI only run it when asked.

How often a verified predicate is found on public refuted pairs is measured by the eval for this feature; the scores live in the scoreboard in the [README](../README.md), not on this page.
