# Spider's hand-labelled ESM false negatives

[Plain-language version](../../docs_simple/evals/spider-esm.md)

Spider's official metric, exact set match (ESM), compares the parsed pieces of a predicted query with the gold query and marks a lot of correct queries wrong. Zhong, Yu and Klein ("Semantic Evaluation for Text-to-SQL with Distilled Test Suites", EMNLP 2020) went through a sample of those failures by hand and published the ones they judged equivalent to the gold query as [`ESMFalseNegatives.tsv`](https://github.com/ruiqi-zhong/TestSuiteEval): 558 rows of (database, gold query, predicted query, reason). The reasons are their words ("redundant join with a parent and primary key", "max is a descending sort with limit 1", "different joining order and aliases"). KumoSQL reads the rows with no model: the algebraic prover first, then a search on databases it builds from Spider's schemas.

```
python tools/spider_esm_bench.py                          # the 293 development pairs (about 10 minutes with two workers)
python tools/spider_esm_bench.py --show proven,refuted    # list those outcomes (a held-out pair only by id)
python tools/spider_esm_bench.py --split all              # development, held-out and all, on separate lines
python tools/spider_esm_bench.py --write-results          # every pair; update benchmarks/results/spider-esm.json
python tools/spider_esm_bench.py --check-sources          # the pinned files match their digests and counts
```

## Source

TestSuiteEval has no licence, so nothing is copied into KumoSQL. `tools/spider_data.py` downloads the files at run time from pinned commits into `~/.cache/kumosql-bench/spider` (or `$KUMOSQL_BENCH_DATA/spider`), checks each SHA-256, and the tests skip when GitHub cannot be reached. Credit for the labels belongs to the authors.

| File | Repository and commit | SHA-256 |
| --- | --- | --- |
| `ESMFalseNegatives.tsv` | `ruiqi-zhong/TestSuiteEval` @ `7bf637883cf0` | `6367141ab7d4c7290dba3ba45361c558e5ef986d2f78af647378974ffa4c7f80` |
| `tables.json` (Spider's schemas) | `taoyds/spider` @ `b7b5b8c890cd` (Apache-2.0 code, CC BY-SA 4.0 data) | `61bb20aa401f03164e2d7f3b16509b7b5f79cc9c943ca7bd159046df1159e2ed` |

The 558 rows repeat pairs (several models wrote the same prediction), so each of the 359 distinct (database, gold, prediction) pairs is decided once; a row count is kept beside each. Spider's SQLite databases and the authors' test-suite databases are on Google Drive and the Yale site, which this environment cannot reach, so every database an eval uses is one KumoSQL builds from `tables.json`: the declared tables and types (number columns hold integers), the listed primary keys (unique, never NULL) and the foreign keys (every non-NULL reference points at an existing row; a column a foreign key points at is unique).

## How a pair is decided

Every pair is decided as `tools/llm_sql_solver_bench.py` decides a Spider pair ([LLM-SQL-Solver](llm-sql-solver.md)):

1. **proven**: `prove_equivalent_algebraic` (SQLite dialect, output names ignored). No keys are given: `tables.json` lists only the first column of a composite primary key (the real `singer_in_concert` key is `(concert_ID, Singer_ID)`; the file lists `concert_ID`), so a listed key may not be unique. A listed key is part of the true key, so a database where it is unique is a valid Spider database: the database builder uses every listed key, the prover none. Results are compared as lists when the gold query ends in `ORDER BY` and as bags otherwise. A proof is not taken when a query compares a text column with a number. Every proof is rerun on 1,000 random databases that respect the schema; a difference there makes the proof **wrong**.
2. **refuted**: SQLite returns different results on a database KumoSQL builds (1,000 random databases, then the targeted databases and the z3 bounded check for unordered queries, each replayed in SQLite). A comparison that depends on how ties are stored (an `ORDER BY` with ties, a `LIMIT`) is only made on a database where neither query's `ORDER BY` has ties, so a tie the join order decides can never count as a difference. The database is shrunk row by row and a cause is recorded (below).
3. **unknown**: anything else, including a pair that runs past 300 seconds or crashes z3 (each pair runs in its own process). **unsupported**: SQLite rejects a query on the schema.

**A refutation here is not a KumoSQL answer.** The authors call the pair equivalent; a database that separates the queries means the label needs a constraint the schema does not declare, or the authors' own metric conventions. It is counted as a *label dispute* and never as a score for or against the prover.

### Value placeholders

Many predictions hold placeholders (`'terminal'`, `"value"`, `1`): the models did not predict values, and TestSuiteEval plugs the gold query's values into the prediction's value slots and accepts a prediction that matches under any plug-in. Those 124 pairs are **plugged** and scored apart from the 235 taken as published. The prediction as published is always tried first; a plugged pair is proven when one plug-in is proven and refuted when every plug-in is refuted (`LIMIT` and `OFFSET` counts are not slots; a prediction with more than 64 plug-ins is unknown). `> =` is read as `>=` first, as TestSuiteEval does.

### Why a labelled-equivalent pair is refuted

The cause is the fewest of TestSuiteEval's conventions under which the two queries agree on the databases tried:

| Cause | Meaning |
| --- | --- |
| `null` | only databases without NULL; the schema allows NULL in the column (a join with a parent drops a child whose foreign key is NULL; `count(col)` against `count(*)`) |
| `distinct` | `DISTINCT` removed from both queries, which the authors' metric does by default |
| `nonempty` | only databases where every table has a row and the gold query returns one (`max` over no rows is NULL; a join without a condition on an empty table) |
| `columns` | results compared up to the order of columns (their comparison permutes them) |
| `values` | the gold's values plugged into a placeholder prediction |
| `other` | none of these makes the two agree on the databases tried |

## Scores

Measured on 2026-10-06 over all 359 distinct pairs (558 rows). **0 wrong.**

| Part | Pairs | Proved | Refuted | Unknown | Unsupported |
| --- | ---: | ---: | ---: | ---: | ---: |
| As published | 235 | 23 | 152 | 59 | 1 |
| Plugged | 124 | 10 | 69 | 44 | 1 |
| **All** | **359** | **33** | **221** | **103** | **2** |
| Development (293) | 293 | 26 | 174 | 91 | 2 |
| **Held out (66)** | 66 | 7 | 47 | 12 | 0 |

Refuted causes: `null` 133, `distinct+null` 37, `nonempty` 24, `distinct` 12, `columns+null` 5, `columns` 3, `values+null` 2, `values` 2, `other` 3 (three pairs whose prediction has a `LIKE` pattern or a value that no plug-in of the gold's values makes equal, such as `LIKE '%korea%'` against `= 'Korea'`).

* **Baseline.** The first development run is the published score: 26/293 proved, 0 wrong. Nothing was tuned afterwards, so the held-out pairs (7/66 proved, 11%, a rate like the development pairs' 9%) were scored once, in the final run.
* **Why so few proofs.** About 60% of the pairs are refutable on a valid database because the hand labels assume facts Spider's own data has (foreign keys never NULL, rows in every table) and its schema does not declare. KumoSQL is never given a key or NOT NULL here, so the "redundant join with a parent and primary key" family is unknown or refuted, not proved. A run with the constraints the authors rely on would prove more; that is not an honest score on a schema that declares nothing.
* **Held out.** One distinct pair in five, by the SHA-1 of `spider-esm`, the database, the gold and the prediction, is held out (66 of 359). A run without `--split` reads only the 293 development pairs; `--show` prints a held-out pair's SQL only under `--split held-out`. The harness was written by an earlier, interrupted session whose log is not available, so "tuned on test: no" cannot be vouched for beyond the code's own guard; the held-out pairs were first scored in the run above.

## Limits

* No Spider database or test-suite database is used. Every refutation is on a database KumoSQL builds, so a refuted pair is a statement about the declared schema.
* The prover gets no key and no NOT NULL: a key it could use is never declared in a form the data supports.
* The 558 rows are the authors' false negatives of ESM, not a random sample of predictions: they say nothing about how many wrong predictions a prover accepts. The "must never prove" side of Spider is [LLM-SQL-Solver's negatives](llm-sql-solver.md).
* Spider lists only the first column of a composite key. The ESM pairs were never given composite keys.
