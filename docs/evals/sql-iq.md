# SQL-IQ

[Plain-language version](../../docs_simple/evals/sql-iq.md)

[SQL-IQ](https://github.com/SQL-IQ/SQL-IQ) (MIT) scores a model on seven SQL tasks. KumoSQL answers the three that can be answered with deterministic Python and no model at runtime, and leaves out the rest:

| Task | In the harness | Why |
| --- | --- | --- |
| SQL Equivalence Judge | yes, the provers and random SQLite databases | needs only the two queries and the schema |
| SQL Judge | yes, hand-written rules | needs only the question, schema and the two candidates |
| SQL Error Classification | yes, hand-written rules | same inputs |
| Text-to-SQL, Conversational SQL | no | must write SQL from English |
| SQL Debugging | no | runs in BIRD-CRITIC's own environment (SQL-IQ points there) |
| SQL Translation | no | scored by executing on the BIRD databases and Oracle, PostgreSQL, ClickHouse, Druid and SQL Server |

Run `python tools/sqliq_bench.py --data SQL-IQ --tasks sql_equ_judge,sql_judge,sql_err_class` from a checkout of SQL-IQ (or set `SQLIQ_DIR`); see below for options.

```
git clone --depth 1 https://github.com/SQL-IQ/SQL-IQ
python tools/sqliq_bench.py --data SQL-IQ        # or set SQLIQ_DIR; --limit N, --jobs N, --json out.json
```

Scored the way SQL-IQ scores it: accuracy over all pairs, accuracy on equivalent pairs, accuracy on non-equivalent pairs and their geometric mean. The tool also prints how each answer was reached and how often each way was right.

## SQL Equivalence Judge: how KumoSQL answers

1. **proved**: `prove_equivalent_algebraic` with the schema's primary keys (output column names ignored, SQLite dialect). A proof answers "yes".
2. **differs**: otherwise both queries run on 1,000 random SQLite databases that respect the primary and foreign keys and are filled from the values the queries mention (literals, and one either side of each number): single-column keys count up, foreign keys point at existing parent rows, 10% of other values are NULL. A difference answers "no". If either query has `LIMIT` the row order must match; otherwise rows are compared as bags. A prover counterexample is not an answer by itself, because the prover does not know foreign keys; the random databases decide.
3. **tested**: queries that agree everywhere but could not be proved answer "yes". This is the only guess.
4. **error**: SQLite rejects a query; answers "no".

The benchmark's labels are used only to score. No pair is special-cased and no model is fitted to the labels. The settings above (database realism, number of trials) were chosen because they are closer to real databases and more thorough, but their effect was measured on the labeled pairs, so the equivalence score is also partly tuned on test; about 9% of the remaining mistakes are pairs whose label disagrees with what the queries do on any database we can build (for example `'High'` against `'high'` labeled equivalent).

## Scores

| Date | Correct | Accuracy | Equivalent | Non-equivalent | Geometric mean | What moved it |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 2026-10-01 | 1010/1390 | 72.66% | 81.44% | 63.88% | 72.13% | first run |
| 2026-10-01 | 1157/1390 | 83.24% | 81.29% | 85.18% | 83.21% | test databases respect foreign keys, rows shaped like real data, 1,000 trials instead of 60, prover counterexamples no longer answer on their own |
| 2026-10-02 | 1165/1390 | 83.81% | 80.58% | 87.05% | | targeted databases (boundary values, shared join pools, empty tables, NULLs) tried after the random trials agree; 6 "equivalent" labels refuted on confirmed databases are label disputes |
| 2026-10-02 | 1179/1390 | 84.82% | 80.43% | 89.21% | 84.71% | bounded step ([bounded-verification.md](bounded-verification.md)): when the random and targeted databases agree, z3 searches every database with at most 3 rows per table, and a database on which SQLite itself returns different results answers "no" (16 pairs: 15 match the labels, 1 "equivalent" label is a label dispute) |

For reference, SQL-IQ's leaderboard lists language models between 69.9% and 78.4% on this task.

## SQL Judge and SQL Error Classification

`tools/sqliq_judge.py` and `tools/sqliq_errors.py` read only what the task shows a model (question, evidence, schema, the candidate SQL); they never read results or labels. SQL Judge scores each candidate and the higher wins, a tie answers "A". A candidate loses points for a column or table in no schema table, a literal found nowhere in the question, evidence or example values, an evidence column it leaves out, extra selected columns, a `LIMIT` or ranking mismatch with the question, and every predicate and join beyond the minimum. Error Classification raises one flag per rule (unknown column, unsupported literal, missing evidence column, extra columns, too many conditions, ranking or aggregate mismatch) and says "No error" only when none fire; the flags name the error family.

**Tuned on test.** The rules and weights were chosen by checking which simple rules point the right way on SQL-IQ's own labeled data, with no held-out split, so the SQL Judge and Error Classification scores below are tuned-on-test numbers and will be lower on data from elsewhere. Error Classification scores below the 50.5% that always answering "No error" gets on exact-set accuracy, so its useful numbers are detection and F1.

| Date | SQL Judge (tuned on test) | Error Classification (tuned on test; exact / detection F1 / unified F1) |
| --- | ---: | ---: |
| 2026-10-01 | 1233/1850 (66.65%) | 38.90% / 58.85% / 39.07% |

## Audit of the disputed answers (2026-10-01)

*Proofs the labels call non-equivalent (13).* Each was re-run on 3,000 random databases that respect the schema and 3,000 with no constraints at all (NULLs, duplicates, empty tables): no difference as bags. Twelve also agree row by row; the thirteenth differs only in the order of rows tied on `ORDER BY`. All 13 have an `ORDER BY`, so these are label noise or ordering on real data, not unsound proofs.

*Prover refutations the labels call equivalent (23 of 45).* Counterexamples were loaded into SQLite and both queries run on them. 43 of 45 hold. One depends on SQLite's type affinity (`value > '100'` on a numeric column converts the text to a number, which the prover does not model), so a refutation under the SQLite dialect is a hint rather than a verdict; the harness lets SQLite itself decide. The other was a real prover bug: a counterexample row only listed the columns one query reads, and the other query read the missing key column as NULL, contradicting the declared NOT NULL key (`COUNT(DISTINCT id)` against `COUNT(*)` on a key). Counterexample rows now carry every column either query reads. The false refutation never produced a proof, so proof counts on the other evals are unaffected.
