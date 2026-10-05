# How KumoSQL checks that it read your query correctly

[All simple guides](README.md) · [Full reference](../docs/parser-checks.md)

Every proof starts from how a parser (sqlglot) reads the SQL text. If the parser groups the operators differently from the database, the proof is about a query nobody wrote. Take `SELECT a | b & c`. In BigQuery, `&` binds tighter than `|`, so it means `a | (b & c)`. sqlglot reads it as `(a | b) & c`. So `a | b & c` and `(a | b) & c` look identical to it, and KumoSQL used to prove them equal. They are not: `2 | 1 & 0` is 2 on BigQuery.

KumoSQL now reads each query a second time with its own small parser, built from each database's grammar, and compares the two readings. If they disagree, the result is `not_proven` with the reason "parser disagreement". The check can only take a proof away. It never makes a proof.

## What it checks

- **A second reading.** For BigQuery, MySQL, PostgreSQL and DuckDB, the grouping of operators, `NOT`, `NULL` tests, `BETWEEN`, `IN`, `CASE`, `DESC` and `DISTINCT` must match sqlglot's tree. Some text a database simply rejects (BigQuery refuses `a > 10 IS TRUE`) and is declined for that reason. Text in a dialect it has no table for, or syntax it does not know, is left as it was read.
- **A round trip.** sqlglot prints its tree and reads it back; the grouping must come back the same.
- **Two builds of the supported version.** An optional tool reads the same queries under normal and compiled SQLGlot 30.21.0 and lists any that differ. Routine CI runs the full suite once on the compiled build; older version comparisons in the reference are historical.

## Does the second reading itself get checked?

Yes, against real databases. A tool generates thousands of small expressions, runs each on a real MySQL server and on DuckDB, and runs sqlglot's reading and the second reading, each with its grouping spelled out. The second reading gave the database's own answer every time. sqlglot's did not on thousands of them, and the check caught every such case. It also injects faults (a moved operator, a lost `NOT`, a flipped `DESC`) into sqlglot's tree and requires the check to notice all of them. BigQuery has no local engine, so a smaller set of 40 cases was run there by hand.

## What it cost

Some proofs the benchmarks counted are gone, because they rested on a text BigQuery rejects or reads differently: 18 cases of the BigQuery edge-case eval. The other benchmarks kept their scores in the runs listed in the reference.

## Limits

- It checks the text a prover receives. A step that prints a tree and proves the printed text is checked on that text.
- It does not check counterexamples, only proofs.
- Constructs it does not know (such as `LATERAL` and `PIVOT`) and other dialects are unchecked, which means sqlglot's reading is trusted as before. The exception is BigQuery's `MATCH_RECOGNIZE` clause: a proof over a query that uses it is always refused ([MATCH_RECOGNIZE](match-recognize.md)).
- The random expressions show the tables are right; they do not measure how often real queries are misread.

The [full reference](../docs/parser-checks.md) has the misreads found, the numbers, how the cases were triaged and the commands.
