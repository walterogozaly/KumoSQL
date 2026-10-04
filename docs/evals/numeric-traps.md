# Numeric traps (SMT prover)

[Plain-language version](../../docs_simple/evals/numeric-traps.md)

75 hand-written query pairs around the places where a rational model of numbers disagrees with BigQuery: integers past 2**53, INT64 overflow in a pushed-down expression, NaN grouping and ordering, `-0.0`, NUMERIC rounding, INT64 to FLOAT64 conversion in `CASE` and `UNION`, `DIV` against `/`, a division moved ahead of a filter, `SAFE_CAST` and `SAFE_DIVIDE`, and the literals of the October 2 audit; the last 16 are NUMERIC scale and rounding (a product, a quotient, `CAST` and `ROUND`, and a NUMERIC overflow). The pairs are in `tests/fixtures/numeric_traps/cases.jsonl`, one fixed schema (`t` with INT64 `x`, `y`, FLOAT64 `f`, `g`, NUMERIC `n`, `m`, STRING `s` and BOOL `c`). Results file: `numeric-traps`. The prover's side of this is in [the SMT prover section of Equivalence provers](../provers.md#numbers-and-runtime-errors).

```
python tools/numeric_traps_bench.py                  # a few seconds
python tools/numeric_traps_bench.py --split dev
python tools/numeric_traps_bench.py --write-results
```

## Labels

A label says what holds on every database, by BigQuery's documented rules (the case's `source` names the GoogleSQL page; `unverified` says what the author could not confirm there):

| Label | Meaning | Cases |
| --- | --- | --- |
| `equivalent` | the same rows, and neither query can raise an error the other cannot | 42 |
| `not_equivalent` | some database where both queries succeed with different rows | 21 |
| `refines` | the same rows wherever the original succeeds, the rewrite never raises an error the original cannot, and the original errors somewhere the rewrite returns rows (a `SAFE_` function, `NULLIF` or a `CASE` guard added) | 5 |
| `introduces_error` | the same rows wherever both succeed, but the rewrite raises an error on a database where the original returns rows (a `CASE` guard dropped, an operation moved ahead of a filter, `SAFE_CAST` to `CAST`) | 7 |

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

2026-10-04, all 75 cases (the first 59 are the cases of the first change; the 16 `numeric-*` cases were added with the NUMERIC rounding model):

| | before the first change (59 cases, master) | after it (the same 59 cases) | with NUMERIC scale and rounding (75 cases) |
| --- | --- | --- | --- |
| equivalent pairs proved | 18/33 | 27/33 | 38/42 |
| differing pairs refuted | 0/16 (2 proved, 3 assumed) | 1/16 (0 proved, 3 assumed) | 6/21 (0 proved, 3 assumed) |
| error cases classified | 0/10 | 10/10 | 12/12 |
| wrong | 3 | 0 | 0 |

The rounding model decides the two numeric literal pairs that were unknown (`numeric-literal-zeros`, `numeric-literal-rounding`) and 9 of the 16 new equivalent pairs, and refutes 5 of the 5 new differing pairs when the solver has its time (the refuted count moved between 4 and 6 across runs on a loaded machine, because a nonlinear NUMERIC product can reach the 5 second limit; an unfinished refutation is reported as unknown). Both new error cases are classified (`numeric-overflow-guard` is a NUMERIC overflow error site, `numeric-div-guard-dropped` a NUMERIC division by zero). `numeric-versus-float-literal` (a NUMERIC against a FLOAT64) stays unknown.

The three wrong answers on master: `negative-zero-column` and `negative-zero-literal` were proved equal though `IEEE_DIVIDE` tells `0.0` from `-0.0` (false proofs, now declined as unknown), and `float-literal-underflow` was refuted though `1e-324` and `2e-324` are the same FLOAT64 (a false refutation, now proved because literals are read as the double nearest their text).

Held out (every fourth case from the fourth, 18 cases: the 14 of the first change plus 4 numeric ones, one of them an error case): 0 wrong; see the results file for the score. Development used the 57 other cases. The three NaN pairs are `assumed`: the prover still carries the "no NaN" assumption for a FLOAT64 column and does not model NaN.

## What stays unknown

* `slash-times-half`: the prover keeps FLOAT64 as a rational, so it cannot reason about the double a `/` returns. Unknown is the answer.
* NUMERIC and BIGNUMERIC arithmetic is scaled and rounded (`src/kumosql/smt_numeric.py`, the pairs named `numeric-*`) only over values whose number of decimal places is known: a column declared INT64, NUMERIC or BIGNUMERIC, an integer literal, a plain-decimal string literal cast to NUMERIC or BIGNUMERIC, and `+ - * /`, `CAST` and `ROUND` of those. `numeric-versus-float-literal` (a NUMERIC compared with a FLOAT64) and anything over an aggregate, a `CASE`, a FLOAT64 or `CAST(f AS NUMERIC)` stay unknown, and a product of three or more factors can exceed the solver's time limit (a refutation is then reported as unknown).
* The sign of zero: the prover treats `-0.0` and `0.0` as one value, so a query that reads the sign (`IEEE_DIVIDE`, `ATAN2` and `SIGN` on a value that can be FLOAT64) is declined, as is `CAST(f AS STRING)` of a FLOAT64.
* INT64 to FLOAT64 conversion past 2**53 is modelled only where no FLOAT64 value is in the queries (a big INT64 literal is declined otherwise).

## Limits

* The cases were written by the author of the value layer from the issue's trap list. They were fixed before the prover changed and a quarter was held out, but the prover's rules were written knowing the traps, so this is a regression and honesty check, not an independent benchmark. Before the `numeric-*` cases the held-out quarter contained no error-labelled case.
* Labels follow the GoogleSQL documentation. Several cases rest on a behaviour the author could not confirm there and say so in their `unverified` field: that BigQuery accepts a literal below FLOAT64's range (`1e-324`), that it reads `1e309` as infinity instead of rejecting it, that a NUMERIC literal rounds as `CAST(string AS NUMERIC)` does, that NUMERIC compared with FLOAT64 is converted to FLOAT64, and (the `numeric-*` cases that multiply or divide) that NUMERIC `*` and `/` round the result to nine decimal digits, half away from zero. The prover models that last rule, so a refutation that depends on it is only as good as the rule; the rounding of `ROUND` and of a conversion is the documented one. The last digit of BIGNUMERIC's range and the behaviour of `ROUND` with a negative digit count past the range are unverified too. None of these is counted against the prover if it answers unknown.
* The 16 `numeric-*` cases were added with the rounding model. Four of them are held out (every fourth from the 59th), chosen and labelled before the prover ran on them. Their answers were seen while developing (the first run showed them unknown because the schema lacked the NUMERIC column `m`, which was then added), and the prover was changed afterwards for a time-limit reason on a development case, so record this as tuned on test: no rule was changed because of a held-out answer, but the answers were not unseen. The four include one error-labelled case, so the held-out quarter now scores an error verdict.
* The witnesses run on DuckDB with BigQuery's guards, not on BigQuery. They check that the label is consistent with a faithful executor, not that BigQuery agrees.
