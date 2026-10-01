# SQL-IQ: SQL Equivalence Judge

[SQL-IQ](https://github.com/SQL-IQ/SQL-IQ) (MIT) scores a model on seven SQL tasks. One of them, the **SQL Equivalence Judge**, gives two SQLite queries and a schema and asks whether they return identical results. 1,390 pairs, half labeled equivalent. It needs no database file and no language model, so KumoSQL's provers can answer it directly. The other tasks need an English question (Text-to-SQL, Conversational SQL, SQL Judge, Error Classification), BIRD-CRITIC's environment (Debugging) or running Oracle, PostgreSQL, ClickHouse, Druid and SQL Server (Translation); they are not part of this harness.

```
git clone --depth 1 https://github.com/SQL-IQ/SQL-IQ
python tools/sqliq_bench.py --data SQL-IQ        # or set SQLIQ_DIR; --limit N, --jobs N, --json out.json
```

Scored the way SQL-IQ scores it: accuracy over all pairs, accuracy on equivalent pairs, accuracy on non-equivalent pairs and their geometric mean. The tool also prints how each answer was reached and how often each way was right.

## How KumoSQL answers

1. **proved**: `prove_equivalent_algebraic` with the schema's primary keys (output column names ignored, SQLite dialect). A proof answers "yes", a counterexample "no".
2. **differs**: otherwise both queries run on 60 random SQLite databases that respect the keys and are filled from the values the queries mention (literals, and one either side of each number). A difference answers "no". If either query has `LIMIT` the row order must match; otherwise rows are compared as bags.
3. **tested**: queries that agree everywhere but could not be proved answer "yes". This is the only guess.
4. **error**: SQLite rejects a query; answers "no".

The benchmark's labels are used only to score. No pair is special-cased and nothing is learned from the labels.

## Scores

| Date | Correct | Accuracy | Equivalent | Non-equivalent | Geometric mean | What moved it |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 2026-10-01 | 1010/1390 | 72.66% | 81.44% | 63.88% | 72.13% | first run |

For reference, SQL-IQ's leaderboard lists language models between 69.9% and 78.4% on this task.
