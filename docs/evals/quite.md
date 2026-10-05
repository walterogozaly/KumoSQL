# QUITE's LLM query rewrites

[QUITE](https://github.com/Yuyang-Song/QUITE) (Song et al., "QUITE: A Query Rewrite System Beyond Rules with LLM Agents") compares LLM-based query rewriters on TPC-H, DSB, Calcite and SQLStorm (StackOverflow) queries. Its `experiments_results/` folder publishes every rewrite the systems produced, each with an `equivalence` flag from running the original and the rewrite on the authors' PostgreSQL instance. KumoSQL reads those pairs with no model at run time: the algebraic prover, then databases on which both queries are replayed in DuckDB.

```
python tools/quite_bench.py                    # every distinct pair (hours: see Scores)
python tools/quite_bench.py --split dev        # the pairs used while developing
python tools/quite_bench.py --sample 48        # a pinned stratified sample (the test draws it from the development pairs)
python tools/quite_bench.py --workers 2        # fewer processes on a shared machine
python tools/quite_bench.py --log run.jsonl    # append verdicts as they land; a rerun resumes
python tools/quite_bench.py --write-results    # quite-rewrites and quite-negatives (whole corpus only)
python tools/quite_bench.py --contains "fetch first" --no-fetch-as-limit   # the baseline reading of FETCH, on the pairs that use it
python tools/quite_bench.py --overlap          # which originals other evals hold (fetch them first, below)
```

## Source

The repository has no licence, so nothing from it is copied into KumoSQL. `tools/quite_bench.py` downloads the 52 flagged result files and the four schema files from commit `0cffd7ce412dcd5c46bf272cd6464a00120db91c` into `~/.cache/kumosql-bench/quite` (or `$KUMOSQL_BENCH_DATA`) and checks each file's SHA-256. Credit for the data belongs to the authors.

| Benchmark | Queries | Systems | Rewrites | Distinct pairs | Flagged equal | Flagged unequal |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| TPC-H | 65 | 13 | 819 | 591 | 764 | 55 |
| DSB | 174 | 13 | 2,028 | 1,545 | 1,764 | 264 |
| Calcite | 59 | 13 | 754 | 583 | 618 | 136 |
| SQLStorm | 41 | 13 | 559 | 455 | 427 | 132 |
| Total | 339 | 13 | 4,160 | 3,174 | 3,573 | 587 |

The 13 systems are QUITE and QUITE with hints, LLM-R2, R-Bot and a plain LLM agent on several models (Claude 3.7, DeepSeek R1 and V3, GPT-4o), and LearnedRewrite. A fourteenth file per benchmark (`EXP_QUITE_*`) holds QUITE's raw output without a flag and is not used. "Queries" counts distinct originals: the files spell some queries differently (`interval '30 days'` against `interval '30' day`, quoted and unquoted names), number them differently, and a few systems store an original with a renamed alias (`total_value` against `value`), so a query is identified by its parsed, case-folded text. That is why the counts exceed the 63, 156, 58 and 43 queries named in the file names. Identical (original, rewrite) texts are scored once, with the number of systems that produced them kept; 324 of the 3,174 distinct pairs have a rewrite identical to the original up to spaces (the system found nothing to change).

## What a flag means

The flags are execution results on one database, not semantic labels. The authors document one rewrite (TPC-H Q2, `documents/equivalence_definition_and_known_cases.md`) that returns the same rows on their instance but drops a `region` filter, and they set its flag to false by hand. Their evaluation script also flags a pair unequal when either query hits the 300-second timeout or the rewrite fails to run. Each flagged-unequal rewrite is classified from its recorded times:

| Why it was flagged unequal | How it is recognised | Distinct pairs |
| --- | --- | ---: |
| `rows`: both queries ran and the results differed | the rewrite's recorded time differs from the original's | 214 |
| `error`: the rewrite failed to run on the authors' instance | its recorded time equals the original's | 197 |
| `timeout`: either query hit the 300-second limit | a recorded time of exactly 300 s | 85 |
| `documented`: the TPC-H Q2 case above | named in the authors' document (QUITE and QUITE with hints) | 2 |

## How a pair is decided

Nothing reads the flag while deciding.

1. **Proven.** `prove_equivalent_algebraic` with the published schema (types, primary keys, NOT NULL, foreign keys), PostgreSQL dialect, output names ignored, results compared as bags. One proof attempt is capped at 60 seconds.
2. **Refuted.** A database on which DuckDB returns different bags for the two queries. Candidates are the prover's counterexample, KumoSQL's targeted databases (`kumosql.refute`, 8 seconds per pair), the database the authors describe for their Q2 case (for TPC-H pairs), and a TPC-H instance at scale factor 0.01 generated with `tpchgen-cli` when it is installed. Every candidate is repaired to respect the keys and foreign keys (composite ones included), rejected if it still breaks one, and replayed: the difference counts only when DuckDB's unoptimized plan agrees (`run_unoptimized`). On the TPC-H instance that unoptimized run is capped at 30 seconds, and one that does not finish is not a refutation.
3. **Unknown**, **unsupported** (sqlglot cannot parse a query), **timeout** or **error** otherwise.

Every proof goes through step 2 as well; a proof with a replayed difference is **wrong**.

DuckDB runs with PostgreSQL's integer division and NULL ordering, `now()` and `current_date` read one fixed instant on both sides, and `char(n)` padding is ignored. Queries with `random()` are never refuted. When a query's rows can depend on input order (a `LIMIT` whose `ORDER BY` does not cover every output column, `ROW_NUMBER`, `LAG`, `STRING_AGG`, `DISTINCT ON`, a `ROWS` frame), a difference counts only if both queries return the same bags with every table loaded in three row orders, and the generated instance (whose row order cannot be changed) is not used.

Two harness-side readings, no KumoSQL change: identifiers are folded to lower case, as PostgreSQL folds unquoted ones (the Calcite queries quote `"EMP"` against tables created as `emp`; 330 pairs needed a quoted name folded and are counted apart below), and `FETCH FIRST n ROWS ONLY` is read as `LIMIT n`, which the prover handles.

**Held out.** One query in five, by SHA-1 of `quite/<benchmark>/<query hash>`, is held out with all its rewrites; `--split dev` runs the rest. No KumoSQL rule was written for this eval.

## Scores

Measured on 2026-10-04 over the whole corpus (3,174 distinct pairs from 4,160 rewrites), with `tpchgen-cli` installed so the TPC-H pairs also run on the generated instance. **0 wrong** in both parts. The run took a few hours with two worker processes on a shared machine; the development pairs and the held-out pairs were run separately and merged with `--log`.

### Flagged equal (`quite-rewrites`)

A pair scores proved when the prover shows it equivalent; it leaves the denominator when a replayed database separates the queries (the flag is then an instance-label disagreement, not an error of either side).

| Benchmark | Pairs | Proved | Refuted | Unknown | Timeout or error | Score |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| TPC-H | 543 | 378 | 3 | 159 | 3 | 378/540 |
| DSB | 1,319 | 603 | 0 | 694 | 22 | 603/1,319 |
| Calcite | 477 | 333 | 29 | 115 | 0 | 333/448 |
| SQLStorm | 335 | 38 | 33 | 264 | 0 | 38/302 |
| **All** | **2,674** | **1,352** | **65** | **1,232** | **25** | **1,352/2,609, 0 wrong** |

* **Held out** (one query in five, 564 pairs, 70 queries): 280/555 proved, 0 wrong, 9 refuted. The development pairs score 1,072/2,054, 0 wrong, 56 refuted, so the held-out fifth behaves like the rest. Two more pairs, flagged equal by one system and unequal by another, are not scored.
* **Original and adapted apart.** 271 of the pairs needed a quoted name folded to lower case (the Calcite queries): 146 proved, 19 refuted, 104 unknown, 2 timeouts. The other 2,403 are scored as published: 1,206 proved, 46 refuted, 1,128 unknown, 23 timeouts or errors.
* **Errors.** Three TPC-H pairs (rewrites of one query) make the prover raise a z3 exception (`model is not available`); they count as errors, never as proofs. They are reported on the workstream issue, not worked around.
* **Why unknown.** Across all 1,530 unknown pairs the commonest reasons are that no row-preserving mapping between the queries was found (827), an aggregate over an outer join (148, declined), a query sqlglot cannot print back faithfully in PostgreSQL (111), and a subquery expression the prover does not support (94). The unknowns were not classified further.

### Flagged unequal (`quite-negatives`)

| Why it was flagged | Pairs | Refuted | Proved | Unknown | Unsupported or timeout |
| --- | ---: | ---: | ---: | ---: | ---: |
| rows differed | 214 | 108 | 14 | 92 | 0 |
| rewrite failed to run | 197 | 5 | 1 | 153 | 38 |
| a query timed out | 85 | 20 | 7 | 52 | 6 |
| documented TPC-H Q2 | 2 | 2 | 0 | 0 | 0 |
| **All** | **498** | **135** | **22** | **297** | **44** |

135 of 498 are refuted by a replayed database; held out, 31 of 106 (8 proved, 0 wrong). **No pair is proved on grounds that a replayed database contradicts.** The 22 proofs are not errors, and each is accounted for:

* 14 pairs flagged for different rows are `ORDER BY ... LIMIT` queries whose sort key has ties (Calcite's `ORDER BY SAL`), so the authors' PostgreSQL happened to cut the tied rows differently for the two queries. The prover proves them equal on the stated assumption that tied rows are cut alike ([ORDER BY, LIMIT and OFFSET](sqlsolver.md#order-by-limit-and-offset)); no replayed database separates them.
* 8 pairs were flagged only because a query timed out (7) or the rewrite failed to run (1): a rewrite that fails on PostgreSQL (one has an output alias spelled `0`) can still be equivalent as written.

The two documented TPC-H Q2 rewrites are refuted on the database the authors describe for the case (a supplier outside EUROPE that ties the EUROPE minimum cost). The same rewrite from a third system (DeepSeek R1), flagged equal, is refuted there too: one of the 65 refuted flagged-equal pairs.

### Before and after

The first run of this harness was the baseline; nothing was tuned on it. One harness reading was then changed, on development pairs only: `FETCH FIRST n ROWS ONLY` is read as `LIMIT n`, because DuckDB cannot replay `FETCH` and the prover's tie check cannot see it. On the 100 development pairs that use `FETCH` (81 flagged equal) the baseline proved 16/67 and the final harness proves 7/67, both 0 wrong: 13 identical-text pairs are no longer proved because the prover declines an aggregate over an outer join under a row cap, and 4 pairs are gained. The lower number is the honest one. A second change bounds the unoptimized replay of a TPC-H instance to 30 seconds, because a six-table comma join becomes a cross product with DuckDB's optimizer off; a difference there is counted only when that run finishes and agrees, so a timeout is an unknown, never a refutation.

**Exposure.** The held-out pairs were run once and never read one by one; the two changes above came from development pairs, and the held-out TPC-H pairs were rerun after the second. No KumoSQL rule was changed for this eval, so the score is not tuned on test.

Results files: `benchmarks/results/quite-rewrites.json` and `benchmarks/results/quite-negatives.json`. The pinned-sample test (`tests/test_quite_bench.py`) runs 51 stratified development pairs without the generated instance: 26 proved, 2 refuted, 0 wrong, with floors of 24 and 2.

## Overlap with other evals

`python tools/benchmark_corpora.py fetch llm-r2 sqlstorm dsb`, then `python tools/quite_bench.py --overlap`, matches each QUITE original against the other evals' queries, as the same text (case, quotes and layout ignored) and as the same shape (every literal masked, so another instance of the same template):

| Other eval's queries | QUITE queries | Same text | Same shape |
| --- | ---: | ---: | ---: |
| R-Bot's Calcite pairs | 59 (Calcite) | 12 | 12 |
| SQLSolver's Calcite pairs | 59 (Calcite) | 1 | 1 |
| SQLSolver's TPC-H pairs | 65 (TPC-H) | 0 | 29 |
| LLM-R2 train, TPC-H (3,772 queries) | 65 (TPC-H) | 29 | 42 |
| LLM-R2 test, TPC-H (500 queries) | 65 (TPC-H) | 10 | 42 |
| LLM-R2 train, DSB (1,635 queries) | 174 (DSB) | 3 | 108 |
| LLM-R2 test, DSB (500 queries) | 174 (DSB) | 1 | 93 |
| Analytical coverage, DSB (156 queries) | 174 (DSB) | 0 | 120 |
| Analytical coverage, SQLStorm v1.0 StackOverflow (18,251 queries) | 41 (SQLStorm) | 5 | 5 |
| Analytical coverage, SQLStorm v1.0 TPC-H (17,036 queries) | 65 (TPC-H) | 0 | 0 |
| Analytical coverage, SQLStorm v0.0 TPC-H (44,000 queries) | 65 (TPC-H) | 47 | 62 |

The TPC-H and DSB originals are template instances of the kind [LLM-R2](llmr2-bench.md) and [analytical coverage](analytical-sql-coverage.md) already run: 62 of the 65 TPC-H originals have the shape of a SQLStorm v0.0 TPC-H query (47 verbatim), and 120 of the 174 DSB originals have the shape of a DSB query already scored. What is new here is the rewrites and their flags, which no other eval has. The Calcite originals overlap [R-Bot's Calcite pairs](sqlsolver.md#r-bots-calcite-pairs) for 12 of 59 queries, but the rewrites differ (R-Bot's are Calcite's rule outputs). Five of the 41 SQLStorm queries are verbatim SQLStorm v1.0 StackOverflow queries; the others are not in SQLStorm v1.0 as published. So source queries recur across evals, but no pair does.

## Limits

* The authors' database instances are not public, so a refutation comes from a database KumoSQL builds; a flagged-equal pair that is refuted is an instance-label disagreement, not an error of either side.
* Results are compared as bags. The authors compare as lists when the original has an outer `ORDER BY`; 37 distinct pairs drop the original's top-level `ORDER BY`.
* PostgreSQL and DuckDB can still differ in places the harness does not cover (string collation, numeric precision of `AVG` beyond six decimal places); a replayed difference is shown with its database, so each can be checked by hand.
