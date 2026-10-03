# Singh & Bedathur LeetCode equivalence pairs

[Plain-language version](../../docs_simple/evals/singh-bedathur.md)

Rajat Singh and Srikanta Bedathur (IIT Delhi), "Can the Rookies Cut the Tough Cookie? Exploring the Use of LLMs for SQL Equivalence Checking" ([arXiv 2412.05561](https://arxiv.org/abs/2412.05561), [repository](https://github.com/rajatb115/LLMs-for-SQL-Equivalence-Checking)), studied language models as SQL equivalence judges. KumoSQL answers the same question with no model at run time: the algebraic prover, a few extra rewrites, and counterexample databases checked in DuckDB.

## Which data is public

| Set in the paper | Pairs | Available? |
| --- | ---: | --- |
| LeetCode fine-tuning pairs (`finetune/dataset`, 2,000 train + 800 eval) | 2,800 | Yes, in the repository. **Scored here.** |
| SQLEquiQuest (student submissions) | 404 | Only by request through a Google Form |
| Spider + DIN-SQL | 1,034 | Only by request through a Google Form |
| Calcite | 232 | The same pairs as SQLSolver's Calcite suite, already scored in [sqlsolver.md](sqlsolver.md#benchmark-coverage) |

The repository has no licence file, so the pairs are not copied into KumoSQL. `tools/singh_bedathur_bench.py` downloads the two files from a pinned commit (`930569d894c950c0b64f2150214394b52a40e11b`) into `~/.cache/kumosql-bench` (or `$KUMOSQL_BENCH_DATA`) and checks their SHA-256 digests. Credit for the data belongs to the authors.

**Overlap.** Every one of the 2,800 pairs (both queries, same text) is also in [VeriEQL](https://github.com/VeriEQL/VeriEQL)'s LeetCode benchmark. The difference is what comes with them: these files list only table and column names (their "Primary Keys" and "Foreign Keys" lines are a fixed placeholder naming tables the pairs never use), while VeriEQL keeps each problem's types, keys and foreign keys. This eval is therefore the *original*, constraint-free reading of the pairs; nothing is adapted.

## How a pair is decided

Nothing reads the published label while deciding; labels are only compared afterwards.

1. **Proved equivalent.** `prove_equivalent_algebraic` runs on the pair as written (MySQL dialect, output names ignored, no keys). When it finds no proof, both queries go through `kumosql.canonical_rules` and the prover runs again. Every proof is then re-run on 900 random DuckDB databases; a database that separates the queries would make the pair **wrong**.
2. **Proved different.** A counterexample is a database on which DuckDB returns different result bags. Candidates are the prover's own counterexample, when it proposes one, and 300 random databases (half plain, half skewed towards one or two values per column so groups, duplicates and ties appear, with exact duplicate rows in some of them; table sizes reach past any `COUNT(..) >= n` threshold in the queries; values include every literal and its neighbours, the day before and after each date literal, a literal times each constant the queries divide or multiply by (`duration / 60 >= 5` gets 299, 300 and 301), and decimals with three places so `ROUND(x, 2)` differs from `x`). When those find nothing, 300 more databases come from a stream seeded by the pair itself, with tables of up to 10 rows, so pairs of one shape do not all miss on the same databases. Each candidate is checked by running both queries; a database already checked in the same search (small tables repeat) is not run again, since its answer would repeat. MySQL's `ORDER BY NULL` (no sorting) is dropped before DuckDB runs the query.
3. **Unknown** otherwise. Unknown is always preferred to wrong.

DuckDB runs with MySQL's NULL ordering and case-insensitive string comparison. A pair never gets "different" where DuckDB and MySQL could disagree: a `LIMIT` whose `ORDER BY` does not cover every output column, `LOWER()` (`LIKE` ignores case only in MySQL), or a non-deterministic function. Column types are not given, so they are inferred from use: compared with a string, a string; used as a date, a date; summed, averaged or rounded, a decimal that may hold fractions; otherwise an integer.

### Canonical rewrites

`kumosql.canonical_rules.canonicalize` keeps every result bag on every database, NULLs included, and uses no constraints:

- `ORDER BY` without `LIMIT` is dropped.
- `SELECT DISTINCT` over a `GROUP BY` that selects every group key is the plain grouped select.
- `HAVING COUNT(*) > 0` (or `>= 1`) under a `GROUP BY` is always true.
- A select that only projects or filters one derived table is merged into it; the filter joins the inner `WHERE`, or its `HAVING` when the inner select is grouped (one row per group).
- `(a, b) IN (SELECT a2, agg FROM t GROUP BY a2)` in a top-level `WHERE` conjunct is an inner join with that grouped table, which holds at most one row per `a2`; correlated subqueries are left alone.

`tests/test_canonical_rules.py` checks each rule on random DuckDB databases and pins the near misses it must leave alone.

## Scores

Measured 2026-10-03 over all 2,800 pairs (`python tools/singh_bedathur_bench.py`, about 30 minutes on 4 cores):

**2736/2800, 0 wrong**: 862 proved equivalent, 1,874 proved different, 64 unknown.

| Outcome | Pairs |
| --- | ---: |
| Proven equivalent | 862 |
| Refuted (counterexample) | 1,874 |
| Unknown | 60 |
| Unsupported (the prover cannot read a query, and no counterexample) | 4 |
| Timeout | 0 |
| Error | 0 |
| Wrong | 0 |

Supported subset: 2736/2796. Held-out fifth (pairs whose text hash is divisible by 5): **560/580, 0 wrong**. The split was made partway through, after the first rewrite rules, and later full-corpus runs were still read while tuning the counterexample search, so this is a weak check. From now on development runs use `--split dev`.

The last step was developed on dev pairs only (the held-out fifth went from 509 to 524 without being looked at). It also fixed two ways the harness could see a difference that is not one: DuckDB returns `DECIMAL` results as Python `Decimal` and `DOUBLE` ones as `float`, and `0.33` never equals `Decimal("0.33")`, so numbers are now compared as floats rounded to six places; and every fraction the generator draws is exact in binary (eighths), because DuckDB averages decimals in floating point and a value like `1.005` lands on the other side of a `ROUND(.., 2)` midpoint from MySQL's exact result. Neither had produced a published refutation. Every dev pair labelled equivalent that this step newly refutes was checked by hand: they hinge on a NULL inside `NOT IN`, duplicate rows, or an inclusive `BETWEEN` against a half-open range.

What moved the score:

| Step | Decided |
| --- | ---: |
| Prover alone, random search on small tables | 2,211 |
| + prover counterexamples, canonical rewrites, bigger tables for `COUNT` thresholds | 2,317 |
| + skewed databases, fractional decimals, `LIMIT` over a full ordering, MySQL NULL order and case rules | 2,434 |
| + a second search stream seeded per pair with tables up to 10 rows, duplicate rows, three-decimal values, date and divisor neighbours, `ORDER BY NULL` | 2,513 |
| + `LEFT JOIN` read as inner under a NULL-rejecting `WHERE`, `WHERE` pushed into `UNION` branches (2,533 on the same master without them) | 2,581 |
| + identity casts, `x * 1.0`, `ROUND(x)` as `ROUND(x, 0)`, `NATURAL JOIN` (`cast_rules.py`; held-out fifth unchanged at 534) | 2,586 |
| + aggregate facts in the SMT model and the set-of-values reduction ([provers.md](../provers.md)), developed on dev pairs only (held-out fifth 534 to 538) | 2,614 |
| + derived tables holding an outer join read through (`outer_join_flatten.py`), developed on dev pairs only (held-out fifth 539) | 2,626 |
| + structural aggregate rewrites (`aggregate_rules.py`: shared filters into `WHERE`, filters into `HAVING`, expressions lifted out of grouped derived tables), developed on dev pairs only (held-out fifth 542), master 80ac740 | 2,650 |
| + `DISTINCT` queries split into unions (`set_split_rules.py`: `CASE` join keys, derived unions, `IN`/`EXISTS` over unions), `OR` splits in set containment, self-join symmetry; developed on dev pairs only (held-out fifth 557), master 696c6c8 | 2,728 |
| + `ABS(x - y)` facts and `x <> y` split into `x < y` and `y < x` in set containment ([provers.md](../provers.md)), developed on dev pairs only; also includes prover PRs merged since 696c6c8 (held-out fifth 560), master 30eb0cb | 2,736 |

### Against the published labels

| Verdict | Label "Equivalent" | Label "Non Equivalent" |
| --- | ---: | ---: |
| Proved equivalent | 862 | 0 |
| Proved different | 476 | 1,398 |
| Unknown | 62 | 2 |

No proof contradicts a label. The 476 pairs labelled equivalent that get a counterexample are equivalent only under LeetCode's constraints, which the files drop: most rely on a key (`UNION` versus `UNION ALL`), a NOT NULL column (`NOT IN` versus an anti-join) or a foreign key. A few differ outright, for example a typo inside a string literal (`'15 OR MORE AS BIN'`). Each comes with its counterexample: `--show-disagreements` prints them.

## Running it

```bash
pip install --user -e ".[dev]"
python tools/singh_bedathur_bench.py                          # all pairs
python tools/singh_bedathur_bench.py --split dev --sample 200 # quick development run
python tools/singh_bedathur_bench.py --show-unknown --show-disagreements --json verdicts.json
```

`tests/test_singh_bedathur_benchmark.py` runs a fixed sample of 120 pairs (floor 100 decided, 0 wrong) and, marked `slow`, all 2,800 (floor 2,420). Both skip when the data cannot be downloaded. A pair that ever comes out wrong is a soundness bug: fix the rule or prover and add the shape to `tests/test_canonical_rules.py` or the prover's tests as a regression case.


Bounded verification (3 rows per table, [bounded-verification.md](bounded-verification.md#results)) on the 1,006 pairs the prover does not already refute: 824 bounded-equivalent, 71 refuted with a replayed database, 0 wrong. Not a proof.
