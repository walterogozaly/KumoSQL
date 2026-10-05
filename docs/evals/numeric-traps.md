# Numeric traps (SMT prover)

[Plain-language version](../../docs_simple/evals/numeric-traps.md)

124 hand-written query pairs around the places where a rational model of numbers disagrees with BigQuery: integers past 2**53, INT64 overflow in a pushed-down expression, NaN grouping and ordering, `-0.0`, NUMERIC rounding, INT64 to FLOAT64 conversion in `CASE` and `UNION`, `DIV` against `/`, a division moved ahead of a filter, a `SUM` over a group that `HAVING` or `WHERE` keeps or drops, `SAFE_CAST` and `SAFE_DIVIDE`, the literals of the October 2 audit, the order a FLOAT64 `SUM` or `AVG` adds in, NUMERIC scale and rounding (a product, a quotient, `CAST`, `ROUND` and a NUMERIC overflow), and (the last 12) NaN in a declared FLOAT64 column: a comparison, `IN`, a join, `CASE`, `IS_NAN`, `IEEE_DIVIDE`, a string cast, `SUM`, `ABS` and `COUNT(DISTINCT)`. The pairs are in `tests/fixtures/numeric_traps/cases.jsonl`, one fixed schema (`t` with INT64 `x`, `y`, FLOAT64 `f`, `g`, NUMERIC `n`, `m`, STRING `s` and BOOL `c`). Results file: `numeric-traps`. The prover's side of this is in [the SMT prover section of Equivalence provers](../provers.md#numbers-and-runtime-errors).

```
python tools/numeric_traps_bench.py                  # a few seconds
python tools/numeric_traps_bench.py --split dev
python tools/numeric_traps_bench.py --write-results
```

## Labels

A label says what holds on every database, by BigQuery's documented rules (the case's `source` names the GoogleSQL page; `unverified` says what the author could not confirm there):

| Label | Meaning | Cases |
| --- | --- | --- |
| `equivalent` | the same rows, and neither query can raise an error the other cannot | 70 |
| `not_equivalent` | some database where both queries succeed with different rows | 33 |
| `refines` | the same rows wherever the original succeeds, the rewrite never raises an error the original cannot, and the original errors somewhere the rewrite returns rows (a `SAFE_` function, `NULLIF` or a `CASE` guard added, or a `HAVING` on a grouping key moved into `WHERE` so a group is no longer summed) | 10 |
| `introduces_error` | the same rows wherever both succeed, but the rewrite raises an error on a database where the original returns rows (a `CASE` guard dropped, an operation moved ahead of a filter, `SAFE_CAST` to `CAST`, a `WHERE` moved into `HAVING` so a group is summed that was not) | 11 |

BigQuery promises no evaluation order between `WHERE`, `ON` and the select list, so an operation that can fail is exposed to every row of its `FROM`: a filter does not protect it. Only `CASE`, `IF`, `COALESCE` and `NULLIF` branches and the `SAFE_` functions do.

**Held-out split.** A quarter of the cases is held out; develop on `--split dev`. By default the held-out cases are every fourth case from the fourth, by position in `cases.jsonl`. A case record may carry an optional boolean `held_out` field that overrides the position: `true` holds the case out, `false` develops on it, and a case without the field follows the position rule (`load_cases` in `tools/numeric_traps_bench.py`). The 12 NaN cases at the end of the file set it explicitly, so that appending them to the 112 cases that were already there (a number that is not a multiple of four) did not change which earlier case is held out, and the three of them held out are the ones that were chosen before the prover answered. The held-out set is 31 cases: the 28 of the position rule over the first 112, and `nan-is-nan-filter`, `nan-ieee-divide-column` and `nan-order-complement`.

Ten cases (the ones DuckDB can run faithfully through [Running BigQuery SQL on DuckDB](../bigquery-on-duckdb.md)) carry a witness database with the rows each query returns, or `error`; `tests/test_numeric_traps_bench.py` replays each one. Four cases use decimal literals, which DuckDB reads as DECIMAL, and keep their witness as documentation only.

## How the prover is scored

The prover decides each pair without its label:

1. **proven**: `prove_equivalent_smt` proves the pair, with the error verdict it reports. A rewrite that can fail where the original succeeds (verdict `introduces`) is not proven; the proof is withheld and the verdict says why.
2. **refuted**: it finds a database on which the rows differ.
3. **assumed**: proven, but the result still lists an assumption the case violates (its `violates` field; no case uses it now that NaN is modelled: the three NaN pairs that did are refuted) or one the case says does not apply (its `discharged` field: `SUM and AVG are treated as independent of row order` on a pair that adds the same values by the same plan, or only INT64 values). It is disclosed, not hidden, and it never counts as a proof of a differing pair.
4. **unknown**: neither.

**Wrong** is a proof or refutation that contradicts the label: an `equivalent` pair refuted, or proven with a verdict that says the rewrite can fail; a `not_equivalent` pair proven without disclosing the violated assumption; an error pair refuted, or classified as safe. An error case counts as **classified** when its verdict is the expected one (`refines` with a proof, `introduces` with the proof withheld).

## Scores

2026-10-04, the first 59 cases (the 20 order-of-sum cases and then the 16 NUMERIC cases are scored in the sections below):

| | master (before) | first change |
| --- | --- | --- |
| equivalent pairs proved | 18/33 | 27/33 |
| differing pairs refuted | 0/16 (2 proved, 3 assumed) | 1/16 (0 proved, 3 assumed) |
| error cases classified | 0/10 | 10/10 |
| wrong | 3 | 0 |

The three wrong answers on master: `negative-zero-column` and `negative-zero-literal` were proved equal though `IEEE_DIVIDE` tells `0.0` from `-0.0` (false proofs, now declined as unknown), and `float-literal-underflow` was refuted though `1e-324` and `2e-324` are the same FLOAT64 (a false refutation, now proved because literals are read as the double nearest their text).

With the 20 order-of-sum cases, all 79 (master before this change, then this change; the new cases are 16 `equivalent` and 4 `not_equivalent`):

| | master | this change |
| --- | --- | --- |
| equivalent pairs proved | 27/49 | 31/49 |
| differing pairs refuted | 2/20 (0 proved, 3 assumed) | 2/20 (0 proved, 3 assumed) |
| error cases classified | 10/10 | 10/10 |
| wrong | 0 | 0 |

The four new proofs are the identical `SUM`, grouped `SUM` and `AVG` written twice and the INT64 `SUM` beside a FLOAT64 filter. On master the prover made the same proofs but listed the order assumption, so under the rule above they were `assumed`, not proved. The one refutation among the new cases is `SUM(f)` against `SUM(g)`, on master too. No proof was lost.

Held out (every fourth case from the fourth, the 19 of the first two changes: no error cases among them): 19 pairs, 0 wrong. Development used the 60 other cases. At that point the three NaN pairs were `assumed` (the prover carried the "no NaN" assumption for a FLOAT64 column); see [NaN modelling](#nan-modelling).

## The order of a FLOAT64 sum

Twenty cases (`float-sum-*`, `float-avg-*`, `int-sum-*`; fifteen development and five held out) are about the assumption that `SUM` and `AVG` do not depend on row order. FLOAT64 addition is not associative, so the same values added in a different order, or regrouped, can differ in the last digits; this repo's own notes say a FLOAT64 sum has no fixed order in BigQuery ([pipeline equivalence](pipeline-equivalence.md) compares floats to six places for that reason), and the GoogleSQL aggregate page was not consulted, so every label that rests on it carries an `unverified` field. The prover's rule is in [Equivalence provers](../provers.md#numbers-and-runtime-errors) (`src/kumosql/float_sum_order.py`):

| Case shape | Example | Expected answer |
| --- | --- | --- |
| the same `SUM`/`AVG` written twice | `SELECT SUM(f) FROM t` against `select sum(f) from t`; grouped; `AVG` | proved; the order assumption is replaced by "an identical FLOAT64 SUM or AVG over the same rows returns the same value on both sides" |
| the same rows by another plan | `y > 0` against `y >= 1`; a CTE against a derived table; `UNION ALL` branches swapped; `WHERE x = 1` against `IF(x = 1, f, NULL)`; `ORDER BY` in a subquery | proved at most with the order assumption listed (`assumed`), or unknown |
| `AVG(f)` against `SUM(f) / COUNT(f)` | | `assumed` at most |
| regrouped additions | a sum of per-group sums, `SUM(f) + SUM(g)` against `SUM(f + g)`, an extra filter, another column | never proved (`not_equivalent`; the first two have a database that differs in every row order) |
| an exact sum | `SUM(x)` over INT64 with a FLOAT64 column in the filter | proved with no order assumption |

The two floor tests are that no differing pair is proved and that the four identical-plan or exact-type cases are proved clean. Not recovered: `SUM` over a column of a derived table over a `UNION` (its type is not visible to the compiler, so the INT64 swap case keeps the assumption and counts as `assumed`), and the INT64 pre-aggregation and split cases, which need the sum of partial sums to be reasoned about (the aggregate values stay free), so they are unknown. Held-out outcomes matched what was expected before the prover was run on them.

## NUMERIC scale and rounding

The last 16 cases (`numeric-*`) came with the NUMERIC rounding model (`src/kumosql/smt_numeric.py`; the first column below is master with the 20 order-of-sum cases, the second adds the model):

| all 95 cases | master (79 cases) | with NUMERIC scale and rounding (95 cases) |
| --- | --- | --- |
| equivalent pairs proved | 31/49 | 42/58 |
| differing pairs refuted | 2/20 (0 proved, 3 assumed) | 7/25 (0 proved, 3 assumed) |
| error cases classified | 10/10 | 12/12 |
| wrong | 0 | 0 |

The model decides the two numeric literal pairs that were unknown (`numeric-literal-zeros`, `numeric-literal-rounding`) and 9 of the 16 new equivalent pairs, and refutes 5 of the 5 new differing pairs when the solver has its time (the refuted count moved between 4 and 6 of the NUMERIC cases across runs on a loaded machine, because a nonlinear NUMERIC product can reach the 5 second limit; an unfinished refutation is reported as unknown). Both new error cases are classified (`numeric-overflow-guard` is a NUMERIC overflow error site, `numeric-div-guard-dropped` a NUMERIC division by zero). `numeric-versus-float-literal` (a NUMERIC against a FLOAT64) stays unknown. Merging the two changes moved no answer: every case has the same outcome as on its own branch.

Held out, all cases (every fourth from the fourth): 23 cases, the 19 above plus 4 numeric ones (`numeric-double-round`, `numeric-product-casts-wider`, `numeric-add-product-twice`, `numeric-div-guard-dropped`, the last an error case), 0 wrong; see the results file for the score.

## Sums over groups

The last 17 cases (`group-sum-*`, `sum-*`, `distinct-sum-*`, `window-sum-*`; twelve development and five held out, positions 95, 99, 103, 107 and 111 of the file, so in the same phase as the rest) are about a `SUM` that overflows on a whole group (`src/kumosql/smt_group_sums.py`). Before, every `SUM` of the same argument was one site whatever its group, so a `WHERE` moved into `HAVING` (the rewrite then adds up groups the original never did) was reported `same`.

| the 17 group-sum cases | master | this change |
| --- | --- | --- |
| equivalent pairs proved | 5/7 | 5/7 |
| differing pairs refuted | 1/1 | 1/1 |
| error cases classified | 0/9 | 7/9 |
| wrong | 7 | 0 |

All 112 cases: 47/65 equivalent proved, 7 or 8/26 differing refuted (0 proved, 3 assumed; the NUMERIC product pairs can hit the solver's time limit, so the count moves by one between runs), 19/21 error cases classified, 0 wrong (master: 12/21 classified, 7 wrong). Merging the NUMERIC and float-sum changes moved no answer of the 17 cases. Held out, all 112 cases: 28 cases (the 23 above plus the five group-sum ones; the held-out set of the earlier cases is unchanged by the merge): 11/15 equivalent proved, 2/7 differing refuted (0 proved, 1 assumed), 5/6 error cases classified, 0 wrong. The two cases not classified are the window pairs (`window-sum-filter-below`, `window-sum-where-to-outer`): a filter moved across a window is not modelled, so the pair is not proven equal and no verdict is reached. The five held-out group-sum cases were run once, after the comparison was written, and nothing was changed in response (no "tuned on test"); `tests/test_smt_group_sums.py` checks the verdicts against DuckDB on every database of up to three rows over extreme values, with the `HAVING` removed so every group is summed. Two cases keep a documentation-only witness, because DuckDB moves a `HAVING` on a key below the aggregation and never adds up the dropped group, which BigQuery may do.

## NaN modelling

The prover models a NaN in a declared FLOAT64 column and in what is computed from one ([Equivalence provers](../provers.md#numbers-and-runtime-errors), the NaN paragraph), instead of assuming "FLOAT64 values are never NaN". The three NaN pairs of the first change (`nan-self-equal`, `nan-negated-less`, `nan-not-equal-split`) had been proved under that assumption and counted as `assumed`; they are now refuted, each with a database holding a NaN. Twelve NaN cases were added at the end of the file (the last twelve; seven differ on a NaN, five are equivalent). Measured on the merged tree, 2026-10-05, against master before this change (112 cases, then all 124):

| | master (112 cases) | with NaN modelling (all 124) |
| --- | --- | --- |
| equivalent pairs proved | 47/65 | 51/70 |
| differing pairs refuted | 7/26 (0 proved, 3 assumed) | 14/33 (0 proved, 0 assumed) |
| error cases classified | 19/21 | 19/21 |
| wrong | 0 | 0 |

On the 112 cases master already had, the same 47 equivalent pairs are proved and every error verdict is unchanged; the three NaN pairs went from `assumed` to refuted, and `numeric-mul-distributes` was refuted in some runs and unknown in others (the table above shows the 14 of the recorded run; a run where it times out shows 13; a nonlinear NUMERIC product that reaches the solver's time limit on a loaded machine, as the NUMERIC section says; it is not an effect of the NaN work). No pair that was proved before became unknown. Of the 12 added cases, four equivalent ones are proved (`nan-is-nan-filter`, `nan-join-self`, `nan-ieee-divide-literal`, `nan-not-in-list`) and three differing ones refuted (`nan-excluded-middle`, `nan-case-branch`, `nan-order-complement`). Five stay unknown, for reasons the prover can state: `nan-ieee-divide-column`, `nan-cast-string`, `nan-sum-keeps-nan` and `nan-abs` involve a function of a NaN or an unknown quotient, which is left free, and a counterexample is not reported when an uninterpreted function stands in for the value; `nan-count-distinct` compares a `COUNT(DISTINCT)` with a count over a derived table, and the prover reads a derived table only when it is joined on all of its columns.

Held out, all 124 cases: 31 cases, 12/16 equivalent proved, 4/9 differing refuted (0 proved, 0 assumed), 5/6 error cases classified, 0 wrong (master's 28: 11/15, 2/7 with 1 assumed, 5/6). The held-out NaN cases of this change (`nan-is-nan-filter`, `nan-ieee-divide-column`, `nan-order-complement`) were chosen, and their labels written, before the prover answered, and the prover was not changed afterwards; one development case (`nan-case-branch`) had its text corrected after its first run because the pair also differed on NULL, with the same label. **Tuned on test:** two held-out cases of the first 59, `nan-distinct-group` and `nan-not-equal-split`, were the stated target of the NaN work and were run while it was developed, so the held-out score is not independent for them; no rule was adjusted to a single case.

The soundness of the NaN rules is also checked outside this suite: `tests/test_smt_nan.py` compares the prover with a reference evaluator of three-valued logic with NaN on random predicate pairs over two FLOAT64 columns (a proof of a pair that differs at some row, or a refutation of a pair that agrees on every row, fails the test). On master, the same probes proved seven NaN-sensitive pairs equal that differ on a NaN (`f <= 1 OR f > 1` against `f IS NOT NULL`, `NOT (f > 0)` against `f <= 0`, `SUM(f)` under `f > 1` against under `NOT (f <= 1)`, `NULLIF(f, f)` against NULL, `CAST(s AS FLOAT64) < 5 OR ... >= 5` against `s IS NOT NULL`, and others); none is proved now.

## What stays unknown

* `slash-times-half`: the prover keeps FLOAT64 as a rational, so it cannot reason about the double a `/` returns. Unknown is the answer.
* NUMERIC and BIGNUMERIC arithmetic is scaled and rounded (`src/kumosql/smt_numeric.py`, the pairs named `numeric-*`) only over values whose number of decimal places is known: a column declared INT64, NUMERIC or BIGNUMERIC, an integer literal, a plain-decimal string literal cast to NUMERIC or BIGNUMERIC, and `+ - * /`, `CAST` and `ROUND` of those. `numeric-versus-float-literal` (a NUMERIC compared with a FLOAT64) and anything over an aggregate, a `CASE`, a FLOAT64 or `CAST(f AS NUMERIC)` stay unknown, and a product of three or more factors can exceed the solver's time limit (a refutation is then reported as unknown).
* NaN where the prover cannot say what BigQuery does: a function, aggregate or arithmetic of a FLOAT64 that may be a NaN is left free (its result may be a NaN, so a comparison on it proves nothing), `INTERSECT`, `EXCEPT` and `IS [NOT] DISTINCT FROM` over a FLOAT64 are declined, and a counterexample that rests on an uninterpreted function is not reported.
* The sign of zero: the prover treats `-0.0` and `0.0` as one value, so a query that reads the sign (`IEEE_DIVIDE`, `ATAN2` and `SIGN` on a value that can be FLOAT64) is declined, as is `CAST(f AS STRING)` of a FLOAT64.
* A regrouped sum (a pre-aggregation in a derived table, a finer or coarser `GROUP BY`, a redundant grouping key), a `SUM` over a join in the direction that needs a witness database, a FLOAT64 or NUMERIC sum, the sum inside an `AVG`, and a `SUM` under a `CASE` branch: the error verdict is `unknown` (see [the error section of the SMT prover](../provers.md#numbers-and-runtime-errors)). A window `SUM` is only matched with the same window relation.
* INT64 to FLOAT64 conversion past 2**53 is modelled only where no FLOAT64 value is in the queries (a big INT64 literal is declined otherwise).

## Limits

* The cases were written by the author of the value layer from the issue's trap list. They were fixed before the prover changed and a quarter was held out, but the prover's rules were written knowing the traps, so this is a regression and honesty check, not an independent benchmark. Before the `numeric-*` cases the held-out quarter contained no error-labelled case.
* Labels follow the GoogleSQL documentation. Several cases rest on a behaviour the author could not confirm there and say so in their `unverified` field: that BigQuery accepts a literal below FLOAT64's range (`1e-324`), that it reads `1e309` as infinity instead of rejecting it, that a NUMERIC literal rounds as `CAST(string AS NUMERIC)` does, that NUMERIC compared with FLOAT64 is converted to FLOAT64, and (the `numeric-*` cases that multiply or divide) that NUMERIC `*` and `/` round the result to nine decimal digits, half away from zero. The prover models that last rule, so a refutation that depends on it is only as good as the rule; the rounding of `ROUND` and of a conversion is the documented one. The last digit of BIGNUMERIC's range and the behaviour of `ROUND` with a negative digit count past the range are unverified too. None of these is counted against the prover if it answers unknown.
* Five NaN cases rest on a behaviour the author could not confirm in the documentation and say so in their `unverified` field: `nan-is-nan-filter` (that `NaN <> NaN` is TRUE), `nan-count-distinct` (that `COUNT(DISTINCT)` counts all NaNs once), `nan-cast-string` (that `CAST('NaN' AS FLOAT64)` is accepted) and `nan-sum-keeps-nan` and `nan-abs` (that `SUM` and `ABS` of a NaN are NaN). Also unverified, and marked so in the prover: what `IS_NAN(NULL)` returns and how `MIN`, `MAX`, `INTERSECT`, `EXCEPT` and `IS DISTINCT FROM` treat a NaN (the prover answers unknown rather than guess).
* The 12 NaN cases were written by the author of the NaN model, and the first 59 cases' two held-out NaN pairs were run during its development (see NaN modelling above), so the NaN part of the held-out score is not independent.
* The 16 `numeric-*` cases were added with the rounding model. Four of them are held out (every fourth, in the same phase as the rest), chosen and labelled before the prover ran on them. Their answers were seen while developing (the first run showed them unknown because the schema lacked the NUMERIC column `m`, which was then added), and the prover was changed afterwards for a time-limit reason on a development case, so record this as tuned on test: no rule was changed because of a held-out answer, but the answers were not unseen. The four include one error-labelled case, so the held-out quarter now scores an error verdict.
* The group-sum cases rest on unverified rules, each marked in the case's `unverified` field: that BigQuery adds up a group that a `HAVING` on its key then drops (an optimizer may move the filter below the aggregation, as DuckDB does), that `SUM(DISTINCT x)` and a window `SUM` raise an error when the sum leaves INT64, as `SUM` does. There is no case for a regrouped sum: whether BigQuery fails on a partial sum that overflows when the total does not is unverified, so the prover answers unknown there. The five held-out group-sum cases were written by the author of the comparison they test.
* The witnesses run on DuckDB with BigQuery's guards, not on BigQuery. They check that the label is consistent with a faithful executor, not that BigQuery agrees.
