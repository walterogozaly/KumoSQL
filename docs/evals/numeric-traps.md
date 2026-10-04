# Numeric traps (SMT prover)

[Plain-language version](../../docs_simple/evals/numeric-traps.md)

59 hand-written query pairs around the places where a rational model of numbers disagrees with BigQuery: integers past 2**53, INT64 overflow in a pushed-down expression, NaN grouping and ordering, `-0.0`, NUMERIC rounding, INT64 to FLOAT64 conversion in `CASE` and `UNION`, `DIV` against `/`, a division moved ahead of a filter, `SAFE_CAST` and `SAFE_DIVIDE`, and the literals of the October 2 audit. The pairs are in `tests/fixtures/numeric_traps/cases.jsonl`, one fixed schema (`t` with INT64 `x`, `y`, FLOAT64 `f`, `g`, NUMERIC `n`, STRING `s` and BOOL `c`). Results file: `numeric-traps`. The prover's side of this is in [the SMT prover section of Equivalence provers](../provers.md#numbers-and-runtime-errors).

```
python tools/numeric_traps_bench.py                  # a few seconds
python tools/numeric_traps_bench.py --split dev
python tools/numeric_traps_bench.py --write-results
```

## Labels

A label says what holds on every database, by BigQuery's documented rules (the case's `source` names the GoogleSQL page; `unverified` says what the author could not confirm there):

| Label | Meaning | Cases |
| --- | --- | --- |
| `equivalent` | the same rows, and neither query can raise an error the other cannot | 33 |
| `not_equivalent` | some database where both queries succeed with different rows | 16 |
| `refines` | the same rows wherever the original succeeds, the rewrite never raises an error the original cannot, and the original errors somewhere the rewrite returns rows (a `SAFE_` function, `NULLIF` or a `CASE` guard added) | 5 |
| `introduces_error` | the same rows wherever both succeed, but the rewrite raises an error on a database where the original returns rows (a `CASE` guard dropped, an operation moved ahead of a filter, `SAFE_CAST` to `CAST`) | 5 |

BigQuery promises no evaluation order between `WHERE`, `ON` and the select list, so an operation that can fail is exposed to every row of its `FROM`: a filter does not protect it. Only `CASE`, `IF`, `COALESCE` and `NULLIF` branches and the `SAFE_` functions do.

Ten cases (the ones DuckDB can run faithfully through [Running BigQuery SQL on DuckDB](../bigquery-on-duckdb.md)) carry a witness database with the rows each query returns, or `error`; `tests/test_numeric_traps_bench.py` replays each one. Four cases use decimal literals, which DuckDB reads as DECIMAL, and keep their witness as documentation only.

## How the prover is scored

The prover decides each pair without its label:

1. **proven**: `prove_equivalent_smt` proves the pair, with the error verdict it reports. A rewrite that can fail where the original succeeds (verdict `introduces`) is not proven; the proof is withheld and the verdict says why.
2. **refuted**: it finds a database on which the rows differ.
3. **assumed**: proven, but only under an assumption the case violates and the result still lists (for example `FLOAT64 values are never NaN` on a NaN case). It is disclosed, not hidden, and it never counts as a proof of a differing pair.
4. **unknown**: neither.

**Wrong** is a proof or refutation that contradicts the label: an `equivalent` pair refuted, or proven with a verdict that says the rewrite can fail; a `not_equivalent` pair proven without disclosing the violated assumption; an error pair refuted, or classified as safe. An error case counts as **classified** when its verdict is the expected one (`refines` with a proof, `introduces` with the proof withheld).

## Scores

2026-10-04, all 59 cases:

| | master (before) | this change |
| --- | --- | --- |
| equivalent pairs proved | 18/33 | 27/33 |
| differing pairs refuted | 0/16 (2 proved, 3 assumed) | 1/16 (0 proved, 3 assumed) |
| error cases classified | 0/10 | 10/10 |
| wrong | 3 | 0 |

The three wrong answers on master: `negative-zero-column` and `negative-zero-literal` were proved equal though `IEEE_DIVIDE` tells `0.0` from `-0.0` (false proofs, now declined as unknown), and `float-literal-underflow` was refuted though `1e-324` and `2e-324` are the same FLOAT64 (a false refutation, now proved because literals are read as the double nearest their text).

Held out (every fourth case from the fourth, 14 cases, no error cases among them): 14 pairs, 0 wrong; see the results file for the score. Development used the 45 other cases. The three NaN pairs are `assumed`: the prover still carries the "no NaN" assumption for a FLOAT64 column and does not model NaN.

## What stays unknown

* `slash-times-half`, the numeric literal canonicalisation pairs and the NUMERIC rounding pairs: the prover keeps NUMERIC and FLOAT64 as rationals, so it cannot reason about scaled rounding or about the double a `/` returns. Unknown is the answer.
* The sign of zero: the prover treats `-0.0` and `0.0` as one value, so a query that reads the sign (`IEEE_DIVIDE`, `ATAN2` and `SIGN` on a value that can be FLOAT64) is declined, as is `CAST(f AS STRING)` of a FLOAT64.
* INT64 to FLOAT64 conversion past 2**53 is modelled only where no FLOAT64 value is in the queries (a big INT64 literal is declined otherwise).

## Limits

* The cases were written by the author of the value layer from the issue's trap list. They were fixed before the prover changed and a quarter was held out, but the prover's rules were written knowing the traps, so this is a regression and honesty check, not an independent benchmark. The held-out quarter contains no error-labelled case, so the error verdicts are scored on development cases only.
* Labels follow the GoogleSQL documentation. Four cases rest on a behaviour the author could not confirm there and say so in their `unverified` field: that BigQuery accepts a literal below FLOAT64's range (`1e-324`), that it reads `1e309` as infinity instead of rejecting it, that a NUMERIC literal rounds as `CAST(string AS NUMERIC)` does, and that NUMERIC compared with FLOAT64 is converted to FLOAT64. None of the four is counted against the prover if it answers unknown.
* The witnesses run on DuckDB with BigQuery's guards, not on BigQuery. They check that the label is consistent with a faithful executor, not that BigQuery agrees.
