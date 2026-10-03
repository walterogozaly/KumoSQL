# LLM-SQL-Solver

[Plain-language version](../../docs_simple/evals/llm-sql-solver.md)

[LLM-SQL-Solver](https://github.com/ZhaoFuheng/LLM-SQL-Solver) (MIT; Zhao et al., arXiv 2312.10321) pairs Spider's gold queries with queries written by DAIL-SQL. KumoSQL uses two of its files, pinned in `tests/fixtures/llm_sql_solver` ([source, licence and overlap](../../tests/fixtures/llm_sql_solver/README.md)):

| Set | Pairs | Label | Results file |
| --- | ---: | --- | --- |
| Negatives (`semantic_inequivalent.jsonl`) | 180 | results differ on the Spider databases | `llm-sql-solver-negatives` |
| Relaxed (`relaxed.jsonl`) | 70 | expert majority vote: 52 equivalent, 18 inequivalent | `llm-sql-solver-relaxed` |

The negatives are a **must-not-prove** set: a proof of any of them is a soundness bug. The source's third file (232 equivalent pairs) is SQLSolver's Calcite set, already scored in [sqlsolver.md](sqlsolver.md).

```
python tools/llm_sql_solver_bench.py                    # dev pairs of both sets (the default), about 10 seconds on 4 cores
python tools/llm_sql_solver_bench.py --show proven,unknown
python tools/llm_sql_solver_bench.py --split all        # every pair: dev, held-out and all reported apart
python tools/llm_sql_solver_bench.py --write-results    # every pair; update both results files and the scoreboard
```

## How a pair is decided

1. **proven**: `prove_equivalent_algebraic` (SQLite dialect, output names ignored). No keys are given: Spider lists only the first column of a composite primary key, so a listed key may not be unique. Results are compared the way Spider compares them: as lists when the first query ends in `ORDER BY` (the prover then reads `ORDER BY` as `ORDER BY .. LIMIT 1000000000`, so both orders must match), as bags otherwise. A proof is not taken when a query compares a text column with a number (`t.id = u.ref` on an integer and a text column): SQLite converts one side, so the comparison holds while the two columns return `3` and `'3'`, and the prover reasons about typed values.
2. **refuted**: both queries run in SQLite on 1,000 random databases that respect the listed keys and foreign keys (a listed key is part of the true key, so these are valid Spider databases), with Spider's column types (number columns hold integers). When a LIMIT or the order decides which rows come back, each database is loaded twice in opposite row orders and used only if each query returns the same both times, so ties never count as a difference. Without LIMIT or ordering, the targeted databases and the z3 bounded check follow, as in [SQL-IQ](sql-iq.md).
3. **unsupported**: SQLite rejects a query. **unknown**: anything else.

`wrong` is a proof of a pair labelled inequivalent, except a pair whose two queries are the same once parsed (a label error: one query cannot return two results on one database). The labels are only read to score.

**Held out.** One pair in five, by a hash of its two queries (57 of 250: 40 negatives, 17 relaxed), is held out. A run without `--split` reads only the 193 dev pairs; `--split held-out` is for final scoring, and `--split all` prints the dev, held-out and combined counts on separate lines. `--write-results` scores every pair (it needs `--split all` or no split), so the results files' command regenerates the published numbers: the headline is over all pairs and each file's `held_out` field is the held-out pairs alone. `--show` prints a held-out pair's SQL only under `--split held-out`; in other runs it gives the pair's id and outcome. The first baseline printed every pair, held-out ones included; the fixes below come from dev pairs.

## Scores

| Date | Negatives | Relaxed | What moved it |
| --- | --- | --- | --- |
| 2026-10-03 | 172/180 refuted, **7 proved** | 22/70 agree | baseline on master (bags only, no type check) |
| 2026-10-03 | 177/180 refuted, 2 proved (both label errors), 0 wrong; dev 139/140 refuted, 1 proved (a label error); held out 38/40 refuted, 1 proved (a label error) | 20/70 agree, 0 wrong; dev 18/53 agree; held out 2/17 agree | the fixes below |

Relaxed agreement fell from 22 to 20: relaxed-023 (one output column against two, an "equivalent" label) was a proof through the bug below and is now refuted, and relaxed-038 (`ORDER BY rating` against `ORDER BY Rating DESC`) is now a list comparison and refuted.

## The seven baseline proofs

| Pair | What happened | Now |
| --- | --- | --- |
| negatives-124, 125 | `SELECT Name, Population .. ORDER BY Population DESC LIMIT 1` proved equal to `SELECT Name .. ORDER BY Population DESC LIMIT 1`: the order key that is not an output became an extra column of the query without the LIMIT, and took Population's place. A prover bug, fixed in `smt_equivalence.prove_equivalent_smt` (the column counts must match) with a case in `tests/test_soundness_regressions.py` | refuted |
| negatives-060 | `visit.visitor_ID` (text) joined to `visitor.ID` (number); one query returns the text column, the other the number | refuted (mixed-type check) |
| negatives-099, 147 | the same rows in a different order (`ORDER BY .. ASC` against `DESC`, or another sort key) | refuted (list comparison) |
| negatives-063, 064 | the two queries are the same text up to spaces | proved; label errors |

## Disputes and limits

* 44 of the 52 pairs the experts call equivalent are refuted on a valid database. Most choose columns in another order, compare `'north america'` with `'North America'`, count a nullable column instead of `*`, or ignore an `ORDER BY` the gold query has; the experts judged intent on realistic data, a refutation needs one valid database.
* 3 relaxed pairs are unsupported because the source pairs them with the wrong database (two) or the query names a column the table lacks (one).
* negatives-055 stays unknown: `HAVING count(*) > 10` against `>= 10` needs a group of exactly ten rows, larger than the random databases.
* Spider's own databases are not bundled, so refutations come from databases KumoSQL builds.
