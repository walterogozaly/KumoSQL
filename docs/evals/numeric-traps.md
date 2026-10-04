# Numeric traps (SMT prover)

[Plain-language version](../../docs_simple/evals/numeric-traps.md)

71 hand-written query pairs around the places where a rational model of numbers disagrees with BigQuery: integers past 2**53, INT64 overflow in a pushed-down expression, NaN grouping and ordering, `-0.0`, NUMERIC rounding, INT64 to FLOAT64 conversion in `CASE` and `UNION`, `DIV` against `/`, a division moved ahead of a filter, `SAFE_CAST` and `SAFE_DIVIDE`, and the literals of the October 2 audit. The pairs are in `tests/fixtures/numeric_traps/cases.jsonl`, one fixed schema (`t` with INT64 `x`, `y`, FLOAT64 `f`, `g`, NUMERIC `n`, STRING `s` and BOOL `c`). Results file: `numeric-traps`. The prover's side of this is in [the SMT prover section of Equivalence provers](../provers.md#numbers-and-runtime-errors).

```
python tools/numeric_traps_bench.py                  # a few seconds
python tools/numeric_traps_bench.py --split dev
python tools/numeric_traps_bench.py --write-results
```

## Labels

A label says what holds on every database, by BigQuery's documented rules (the case's `source` names the GoogleSQL page; `unverified` says what the author could not confirm there):

| Label | Meaning | Cases |
| --- | --- | --- |
| `equivalent` | the same rows, and neither query can raise an error the other cannot | 38 |
| `not_equivalent` | some database where both queries succeed with different rows | 23 |
| `refines` | the same rows wherever the original succeeds, the rewrite never raises an error the original cannot, and the original errors somewhere the rewrite returns rows (a `SAFE_` function, `NULLIF` or a `CASE` guard added) | 5 |
| `introduces_error` | the same rows wherever both succeed, but the rewrite raises an error on a database where the original returns rows (a `CASE` guard dropped, an operation moved ahead of a filter, `SAFE_CAST` to `CAST`) | 5 |

BigQuery promises no evaluation order between `WHERE`, `ON` and the select list, so an operation that can fail is exposed to every row of its `FROM`: a filter does not protect it. Only `CASE`, `IF`, `COALESCE` and `NULLIF` branches and the `SAFE_` functions do.

Ten cases (the ones DuckDB can run faithfully through [Running BigQuery SQL on DuckDB](../bigquery-on-duckdb.md)) carry a witness database with the rows each query returns, or `error`; `tests/test_numeric_traps_bench.py` replays each one. Four cases use decimal literals, which DuckDB reads as DECIMAL, and keep their witness as documentation only.

## How the prover is scored

The prover decides each pair without its label:

1. **proven**: `prove_equivalent_smt` proves the pair, with the error verdict it reports. A rewrite that can fail where the original succeeds (verdict `introduces`) is not proven; the proof is withheld and the verdict says why.
2. **refuted**: it finds a database on which the rows differ.
3. **assumed**: proven, but only under an assumption the case violates and the result still lists (a case's `violates` field names it; no case uses it now that NaN is modelled, the three NaN pairs that did are refuted). It is disclosed, not hidden, and it never counts as a proof of a differing pair.
4. **unknown**: neither.

**Wrong** is a proof or refutation that contradicts the label: an `equivalent` pair refuted, or proven with a verdict that says the rewrite can fail; a `not_equivalent` pair proven without disclosing the violated assumption; an error pair refuted, or classified as safe. An error case counts as **classified** when its verdict is the expected one (`refines` with a proof, `introduces` with the proof withheld).

## Scores

2026-10-04, all 71 cases (the NaN modelling change; the earlier numeric value layer took the suite from 18/33 and 0/16 to 27/33 and 1/16 on the first 59):

| | before NaN modelling (first 59 cases) | with NaN modelling (all 71) |
| --- | --- | --- |
| equivalent pairs proved | 27/33 | 31/38 |
| differing pairs refuted | 1/16 (0 proved, 3 assumed) | 7/23 (0 proved, 0 assumed) |
| error cases classified | 10/10 | 10/10 |
| wrong | 0 | 0 |

On the first 59 cases the same 27 equivalent pairs are proved and the three NaN pairs (`nan-self-equal`, `nan-negated-less`, `nan-not-equal-split`) went from `assumed` (proved under "FLOAT64 values are never NaN") to refuted, each with a database holding a NaN; no pair that was proved before became unknown. The twelve added cases are all NaN cases (comparison, `IN`, joins, `CASE`, `IS_NAN`, `IEEE_DIVIDE`, a string cast, `SUM`, `ABS`, `COUNT(DISTINCT)`). Four equivalent ones are proved (`nan-is-nan-filter`, `nan-join-self`, `nan-ieee-divide-literal`, `nan-not-in-list`) and three differing ones refuted (`nan-excluded-middle`, `nan-case-branch`, `nan-order-complement`). Five stay unknown, for reasons the prover can state: `nan-ieee-divide-column`, `nan-cast-string`, `nan-sum-keeps-nan` and `nan-abs` involve a function of a NaN or an unknown quotient, which is left free, and a counterexample is not reported when an uninterpreted function stands in for the value; `nan-count-distinct` compares a `COUNT(DISTINCT)` with a count over a derived table, and the prover reads a derived table only when it is joined on all of its columns.

Held out (every fourth case from the fourth, 17 cases, no error cases among them): 17 pairs, 0 wrong; see the results file for the score. Development used the other 54 cases. **Tuned on test:** two of the original held-out cases, `nan-distinct-group` and `nan-not-equal-split`, were the stated target of the NaN work and were run while it was developed, so the held-out score is not independent for them; no rule was adjusted to a single case. The three held-out cases added with this change (`nan-is-nan-filter`, `nan-ieee-divide-column`, `nan-order-complement`) were labelled before the prover ran on them and the prover was not changed afterwards; one development case (`nan-case-branch`) had its text corrected after its first run because the pair also differed on NULL, with the same label.

The soundness of the NaN rules is also checked outside this suite: `tests/test_smt_nan.py` compares the prover with a reference evaluator of three-valued logic with NaN on random predicate pairs over two FLOAT64 columns (a proof of a pair that differs at some row, or a refutation of a pair that agrees on every row, fails the test). On master, the same probes proved seven NaN-sensitive pairs equal that differ on a NaN (`f <= 1 OR f > 1` against `f IS NOT NULL`, `NOT (f > 0)` against `f <= 0`, `SUM(f)` under `f > 1` against under `NOT (f <= 1)`, `NULLIF(f, f)` against NULL, `CAST(s AS FLOAT64) < 5 OR ... >= 5` against `s IS NOT NULL`, and others); none is proved now.

## What stays unknown

* `slash-times-half`, the numeric literal canonicalisation pairs and the NUMERIC rounding pairs: the prover keeps NUMERIC and FLOAT64 as rationals, so it cannot reason about scaled rounding or about the double a `/` returns. Unknown is the answer.
* NaN where the prover cannot say what BigQuery does: a function, aggregate or arithmetic of a FLOAT64 that may be a NaN is left free (its result may be a NaN, so a comparison on it proves nothing), `INTERSECT`, `EXCEPT` and `IS [NOT] DISTINCT FROM` over a FLOAT64 are declined, and a counterexample that rests on an uninterpreted function is not reported.
* The sign of zero: the prover treats `-0.0` and `0.0` as one value, so a query that reads the sign (`IEEE_DIVIDE`, `ATAN2` and `SIGN` on a value that can be FLOAT64) is declined, as is `CAST(f AS STRING)` of a FLOAT64.
* INT64 to FLOAT64 conversion past 2**53 is modelled only where no FLOAT64 value is in the queries (a big INT64 literal is declined otherwise).

## Limits

* The cases were written by the author of the value layer from the issue's trap list. They were fixed before the prover changed and a quarter was held out, but the prover's rules were written knowing the traps, so this is a regression and honesty check, not an independent benchmark. The held-out quarter contains no error-labelled case, so the error verdicts are scored on development cases only.
* Labels follow the GoogleSQL documentation. Nine cases rest on a behaviour the author could not confirm there and say so in their `unverified` field (the four numeric ones below, and `nan-is-nan-filter`, `nan-count-distinct`, `nan-cast-string`, `nan-sum-keeps-nan` and `nan-abs` for NaN rules: that `NaN <> NaN` is TRUE, that `COUNT(DISTINCT)` counts all NaNs once, that `CAST('NaN' AS FLOAT64)` is accepted, that `SUM` and `ABS` of a NaN are NaN). The four numeric ones: that BigQuery accepts a literal below FLOAT64's range (`1e-324`), that it reads `1e309` as infinity instead of rejecting it, that a NUMERIC literal rounds as `CAST(string AS NUMERIC)` does, and that NUMERIC compared with FLOAT64 is converted to FLOAT64. None of the four is counted against the prover if it answers unknown.
* The witnesses run on DuckDB with BigQuery's guards, not on BigQuery. They check that the label is consistent with a faithful executor, not that BigQuery agrees.
