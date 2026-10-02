# Singh & Bedathur LeetCode equivalence pairs

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
2. **Proved different.** A counterexample is a database on which DuckDB returns different result bags. Candidates are the prover's own counterexample, when it proposes one, and 300 random databases (half plain, half skewed towards one or two values per column so groups, duplicates and ties appear; table sizes reach past any `COUNT(..) >= n` threshold in the queries; values include every literal and its neighbours). Each candidate is checked by running both queries.
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

Measured 2026-10-02 over all 2,800 pairs (`python tools/singh_bedathur_bench.py`, about 25 minutes on 4 cores):

**2442/2800, 0 wrong**: 648 proved equivalent, 1,794 proved different, 358 unknown.

| Outcome | Pairs |
| --- | ---: |
| Proven equivalent | 648 |
| Refuted (counterexample) | 1,794 |
| Unknown | 321 |
| Unsupported (the prover cannot read a query, and no counterexample) | 37 |
| Timeout | 0 |
| Error | 0 |
| Wrong | 0 |

Supported subset: 2442/2763. Held-out fifth (pairs whose text hash is divisible by 5): **509/580, 0 wrong**. The split was made partway through, after the first rewrite rules, and later full-corpus runs were still read while tuning the counterexample search, so this is a weak check. From now on development runs use `--split dev`.

What moved the score:

| Step | Decided |
| --- | ---: |
| Prover alone, random search on small tables | 2,211 |
| + prover counterexamples, canonical rewrites, bigger tables for `COUNT` thresholds | 2,317 |
| + skewed databases, fractional decimals, `LIMIT` over a full ordering, MySQL NULL order and case rules | 2,434 |
| + identity casts, `x * 1.0`, `ROUND(x)` as `ROUND(x, 0)`, `NATURAL JOIN` (`cast_rules.py`) | 2,442 |

### Against the published labels

| Verdict | Label "Equivalent" | Label "Non Equivalent" |
| --- | ---: | ---: |
| Proved equivalent | 640 | 0 |
| Proved different | 461 | 1,333 |
| Unknown | 299 | 67 |

No proof contradicts a label. The 461 pairs labelled equivalent that get a counterexample are equivalent only under LeetCode's constraints, which the files drop: most rely on a key (`UNION` versus `UNION ALL`), a NOT NULL column (`NOT IN` versus an anti-join) or a foreign key. A few differ outright, for example a typo inside a string literal (`'15 OR MORE AS BIN'`). Each comes with its counterexample: `--show-disagreements` prints them.

## Running it

```bash
pip install --user -e ".[dev]"
python tools/singh_bedathur_bench.py                          # all pairs
python tools/singh_bedathur_bench.py --split dev --sample 200 # quick development run
python tools/singh_bedathur_bench.py --show-unknown --show-disagreements --json verdicts.json
```

`tests/test_singh_bedathur_benchmark.py` runs a fixed sample of 120 pairs (floor 100 decided, 0 wrong) and, marked `slow`, all 2,800 (floor 2,420). Both skip when the data cannot be downloaded. A pair that ever comes out wrong is a soundness bug: fix the rule or prover and add the shape to `tests/test_canonical_rules.py` or the prover's tests as a regression case.
