# DLBench

[DLBench](https://github.com/DLBenchll/DLBench) (Apache-2.0, ASE 2025) pairs a query with its translation into another database's dialect: BIRDTrans translates 3,206 of BIRD's SQLite queries and BUTTERTrans 3,196 queries from MySQL's and PostgreSQL's own test suites, into MySQL, MariaDB, PostgreSQL, ClickHouse, MonetDB and DuckDB. Its labels (6,199 "exact", 203 "approximate" equivalence) come from language models and human review. KumoSQL uses it for cross-dialect coverage: does each side parse in its own dialect, and can the translation be proved equal to its source? The repository pins 807 pairs: every approximate pair and one exact pair in ten ([source, subset rule and overlap](../../tests/fixtures/dlbench/README.md)). Results file: `dlbench`.

```
python tools/dlbench_bench.py                     # the pinned subset, about a minute on 4 cores
python tools/dlbench_bench.py --data DLBench      # all 6,402 pairs from a checkout
python tools/dlbench_bench.py --write-results
```

## How a pair is decided

1. **parsed**: sqlglot reads the source in its dialect and the translation in the target's. sqlglot has no MariaDB or MonetDB dialect, so they are read as MySQL and PostgreSQL.
2. **proven**: the translation's renamed columns are mapped back to the source's names (BIRDTrans renames some, such as `Type` to `_Type`), identifiers are lower-cased, sqlglot writes the translation in the source's dialect, and `prove_equivalent_algebraic` proves it equal to the source (no keys, output names ignored). A source that ends in `ORDER BY` is compared as a list, so the translation must keep the order.
3. **unknown** otherwise, including every **dialect gap**: the two databases read the same text differently and sqlglot does not translate the difference, so no proof is taken.

| Dialect gap | Rule |
| --- | --- |
| LIKE and case | `LIKE` ignores case in SQLite and MySQL, not in the others |
| MySQL string comparison | MySQL and MariaDB compare strings without case (default collation); between a MySQL-family query and another dialect only queries with no string literal and no column are proved |
| integer division | `/` on integers truncates in PostgreSQL and MonetDB, not in MySQL |
| division by zero | NULL in SQLite and MySQL, infinity in DuckDB and ClickHouse (an error elsewhere, which proofs exclude) |
| 32-bit float | `REAL` (and `FLOAT` in most targets) is 32-bit; sqlglot writes it as SQLite's 64-bit `REAL` |
| decimal arithmetic | `1.0` is an exact decimal in PostgreSQL, MonetDB and MySQL, a float in SQLite |
| ClickHouse defaults | ClickHouse returns 0 for `SUM` of no rows and fills outer-join gaps with default values, not NULL |
| SQLite type conversion | a text column compared with a number (see [LLM-SQL-Solver](llm-sql-solver.md)) |

A proof therefore says the two queries mean the same under sqlglot's reading of each dialect, outside these gaps. MariaDB-specific and MonetDB-specific behaviour is not modelled, and PostgreSQL is assumed to sort text bytewise (the C collation). Every proof of a BIRDTrans pair is re-run: source and translation on 300 random SQLite databases (lists when ordered, with a reverse-order reload so ties never count), and a DuckDB translation also natively in DuckDB on the same data (a difference confirmed with the optimizer off). A difference makes the proof **wrong**.

## Scores

Pinned subset, 2026-10-03: **109/604 exact pairs proved, 0 wrong; 799/807 parsed**. Held out (103 exact pairs): 19 proved. Approximate pairs proved: 1/203 (BIRDTrans PostgreSQL 108: `STRFTIME('%Y', date)` against `TO_CHAR(date, 'YYYY')` on a column that is text in SQLite and a date in PostgreSQL; sqlglot reads both as the same date formatting).

| Dataset | Target | Pairs | Parsed | Proved | Dialect gap |
| --- | --- | ---: | ---: | ---: | ---: |
| BIRDTrans | ClickHouse | 57 | 57 | 18 | 32 |
| BIRDTrans | DuckDB | 77 | 77 | 15 | 41 |
| BIRDTrans | MariaDB | 91 | 91 | 0 | 91 |
| BIRDTrans | MonetDB | 83 | 83 | 7 | 38 |
| BIRDTrans | MySQL | 117 | 117 | 0 | 117 |
| BIRDTrans | PostgreSQL | 68 | 66 | 16 | 18 |
| BUTTERTrans | ClickHouse | 60 | 59 | 4 | 52 |
| BUTTERTrans | DuckDB | 62 | 59 | 7 | 44 |
| BUTTERTrans | MariaDB | 67 | 67 | 35 | 2 |
| BUTTERTrans | MonetDB | 56 | 55 | 0 | 47 |
| BUTTERTrans | MySQL | 3 | 3 | 0 | 3 |
| BUTTERTrans | PostgreSQL | 66 | 65 | 8 | 54 |

Unparsed: 2 BIRDTrans PostgreSQL translations and 6 BUTTERTrans queries (one MySQL source, five translations).

**Baseline.** The first run on master, with only the LIKE, MySQL, integer-division and SQLite type rules, proved 112 pairs, and the native DuckDB re-run showed two of them (both "approximate") return different results: `CAST(.. AS REAL)` is 32-bit in DuckDB, and dividing by zero gives infinity there. The 32-bit float, division-by-zero, decimal and ClickHouse rules were added after that run, with the whole subset (held-out pairs included) in view, so they count as **tuned on test**.

## Limits

* Most BUTTERTrans queries test one function on constants (`SELECT hex(concat(regexp_instr('a', 'a')))` against `SELECT HEX(CONCAT(POSITION('a' IN 'a')))`); the prover treats such functions as opaque, so they stay unknown unless the two sides are the same function.
* The MySQL rule is deliberately coarse: it blocks every BIRDTrans translation into MySQL and MariaDB.
* Labels are not proofs: "exact" pairs can still differ on edge cases (time zones in `UNIX_TIMESTAMP`, the `SUBSTR('Last Updated', -4)` string literal in BIRD's own gold query). Only a proof contradicted by running both queries counts as wrong.
