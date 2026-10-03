# Proof re-check

[Plain-language version](../docs_simple/proof-recheck.md)

The evals count a pair as proven when the prover says the two queries are equal, then run a few dozen to a thousand random databases as a cross-check. A proof the prover gets wrong survives that cross-check when the database that separates the queries is rare. The proof re-check is a developer tool that runs a much heavier executed search on every pair an eval counts as proven, to find those false proofs. It changes no score and no prover rule. A false proof it finds is a bug in the prover, filed against the rule that makes the proof go through.

```sh
python tools/proof_recheck.py --list
python tools/proof_recheck.py qed-calcite --jobs 2 --budget 3000 --seconds 150 --out proof-recheck/raw
python tools/proof_recheck.py singh --pairs KEY1,KEY2 --budget 20000
python tools/proof_recheck.py verieql --every 40 --budget 150 --seconds 30 --out /tmp/rc
```

`--out` takes a folder for one `<eval>.jsonl` per eval, one record per pair; a rerun skips pairs already written unless `--fresh`. A machine with 4 cores is oversubscribed by more than about `--jobs 2` of the heavier evals. The raw output is large, so it is not committed.

## The search

`tools/recheck/engine.py` takes a `Case`: the DuckDB SQL of both sides, the tables (columns with a kind, NOT NULL, keys, foreign keys, allowed values), an optional `legal(db)` callback for other declared constraints such as CHECKs, `setup` statements (collations, the BigQuery-on-DuckDB settings and macros), and a comparison `mode` (`bag`, `set`, `list`, `contained`, `contained-set`). `engine.recheck(case, budget=..., seconds=...)` returns one JSON record.

- **Databases.** Exhaustive tiny databases (0 to 3 rows per table, each column drawn from NULL and two values, foreign keys from the parent's chosen rows); edge databases (all empty, each table empty in turn, all-NULL rows, doubled rows); and random databases whose profile varies (0 to 20 rows, NULL rates to 80%, duplicate-heavy rows, ties, values next to the queries' literals, boundary numbers such as 2^24+1 and 2^53+1, strings that differ in case, trailing spaces or Unicode case mapping, dates on month and year boundaries). Every database honours the declared keys, NOT NULL columns, foreign keys and `legal`.
- **What counts as a difference.** The bags differ after floats are rounded to 9 significant digits; DuckDB's unoptimized plan returns the same results (DuckDB 1.5.6 has optimizer bugs); and neither query changes when every table's rows are reversed, rotated or shuffled. A difference that fails a check is counted in the record's `notes` (`optimizer`, `nondeterministic`, `float-noise`) and the pair still `survived`. The witness is shrunk row by row.
- **Float noise.** A result that differs only in float digits is noise. So is an integer beside a double: DuckDB's `AVG` returns DOUBLE, so over one row the value 2^53+1 comes back as 2^53, where MySQL's exact decimal keeps it. The check reads integers in a column that holds a float on either side as doubles before comparing at 6 digits. Integers in columns without a float still compare exactly, so an integer-only difference is never hidden.
- **Verdicts.** `survived`, `differs`, `not-proven` (the eval does not count the pair), `unrunnable` (an engine rejects both queries), `timeout` and `search-error`. Each record also lists order-sensitive constructs (`LIMIT`, `ANY_VALUE`, `DISTINCT ON`, `ROW_NUMBER`, unordered `ARRAY_AGG`).

## Adapters

An adapter proves each pair exactly as its eval does (same entry point, options and constraints) and returns the `Case` that eval's own executed check runs. Each module in `tools/recheck/` exposes an `ADAPTERS` dict.

| Evals | Module |
| --- | --- |
| `sqlsolver-calcite`, `sqlsolver-spark`, `sqlsolver-tpch`, `sqlsolver-tpcc`, `qed-calcite`, `calcite-mined`, `rbot-calcite`, `cosette`, `spes-only` | `calcite_family.py` |
| `singh`, `singh-fractions`, `singh-leetcode-types` | `singh.py` |
| `verieql` (pairs are named `<suite>:<VeriEQL index>`) | `verieql.py` |

`singh-fractions` widens every whole-number column to `DECIMAL(18,3)` (the files give no column types, so a proof claims every typing); `singh-leetcode-types` takes each column's type from VeriEQL's copy of the same LeetCode problem. `recheck/singh_mysql.py` runs the Singh pairs' original text on a MySQL 8 server instead of a DuckDB translation, to see what the translation hides (a string compared with a number, inexact decimal division, accent-insensitive `LIKE`). It needs a server started with `--lower-case-table-names=1` and `pip install pymysql`:

```sh
python tools/recheck/singh_mysql.py singh --socket /tmp/mysql.sock --jobs 4 --out proof-recheck/raw
```

Adapters for more evals (fuzzing and rewrite evals, view reuse and containment, DLBench and LLM-SQL-Solver, pipelines and refactors, bounded verification) were drafted but are not reviewed against their harnesses and are not in the repository.

## Triaging a `differs` record

1. **Is it really proven the eval's way?** Rerun the eval's harness on the pair.
2. **Dialect gap?** The prover reads the eval's dialect (MySQL for the Calcite family, LeetCode and VeriEQL; BigQuery elsewhere) while DuckDB runs a translation. Suspect string case and trailing spaces, integer and decimal division, NULL ordering, implicit casts, date functions, integer overflow and float precision. Confirm on a second engine where possible.
3. **Ties.** A `LIMIT` with tied rows or a hash-ordered pick is nondeterministic, not a false proof.
4. **Minimize** the pair and the witness, then name the rule that makes the proof go through (`module.py:function`, found by switching rules off one at a time).
5. **Fix or route.** A false proof gets a regression test, and the eval's results file and scoreboard are corrected in the same pull request. Verdict words: `false proof`, `dialect gap`, `nondeterministic`, `harness`, `already fixed`.

## Rules for the tool

- Held-out pairs run only to record verdicts. Never derive a fix from one; a forced fix is recorded as "tuned on test".
- 0 wrong always; unknown beats wrong.
- New search code goes in new modules under `tools/recheck/`. The tool does not edit `kumosql.result_equivalence`, the `kumosql.counterexample` searcher or `DatasetRunner`.
- A difference counts only if DuckDB's unoptimized plan agrees and no query depends on row order.

## What the first runs found

Recorded 2026-10-03 (VeriEQL on master `38cb88c`), 3,000 databases per pair (Singh: about 18,000 per pair across the DuckDB and MySQL runs):

| Eval | Proven pairs re-checked | Result |
| --- | --- | --- |
| QED (Calcite) | 177 | all survived |
| Singh and Bedathur | 862 (163 held-out) | all survived about 15.5 million databases, on DuckDB and on MySQL 8.0 |
| VeriEQL | 7,062 (Literature 25, Calcite-397 318, LeetCode 6,719) | 0 false proofs; one `differs` record, the integer-beside-a-double case above |

The search finds 48 of the 50 false proofs from earlier audits, most within a few dozen databases. One of the misses needs DuckDB INT32 literal overflow handling; the other has catalog-qualified table names in the audit's schema.

The Singh runs also found a rule the proofs use that is unsound for MySQL: a string literal is treated as never equal to a number, so `WHERE '2' <> 2` is proven equal to no filter, while MySQL reads `'2' <> 2` as false. No counted pair rests on it. It is tracked in [#542](https://github.com/walterogozaly/KumoSQL/issues/542).

Not yet done: a rerun of the QED, mined Calcite, SQLSolver, R-Bot, Cosette and SPES families end to end with the committed adapters, and the remaining evals listed above. Those runs and the raw results are tracked in [#497](https://github.com/walterogozaly/KumoSQL/issues/497).

## Tests

`tests/test_proof_recheck.py` covers the engine: it finds a duplicate-sensitive difference, lets an equivalent pair survive, keeps databases legal under keys, NOT NULL and foreign keys, honours each comparison mode, ignores row-order dependence and integer-versus-double noise, and keeps an integer-only difference. Run it with `python tools/run_tests.py --label "proof recheck" --target tests/test_proof_recheck.py tests/test_proof_recheck.py`.
