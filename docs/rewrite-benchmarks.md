# Query rewriting benchmarks

KumoSQL rewrites a query only when its prover proves the rewrite returns the same rows. `kumosql.query_optimizer.optimize(sql, catalog)` applies general relational rules, keeps a rewrite only when it is proven, and otherwise answers "no rewrite". Nothing is learned from the benchmarks' answers and no language model runs at evaluation time. These pages measure how often that produces a useful, verified rewrite.

Scores keep four things apart:

* **Correctness**: rewrites that change a query's result. Every emitted rewrite is proven first; the benchmarks then execute it and compare rows, and a single difference would be reported here.
* **Coverage**: how many queries were changed, and how many of the changed queries were verified (all of them, by construction).
* **Usefulness**: how often a verified rewrite is faster or simpler.
* **Performance**: measured speedups, kept separate from proof results.

## How rewrites are found and proven

The rules (`src/kumosql/query_optimizer.py`) are identities that hold for any data, applied until none fires:

| Rule | What it does |
| --- | --- |
| `unused_cte` | drops a CTE nothing reads |
| `passthrough` | replaces a CTE or derived table that is only `SELECT * FROM x` by `x`, and a statement that is only `SELECT * FROM (q)` by `q` |
| `one_row_join` | drops a cross join with an aggregate that has no `GROUP BY` (always exactly one row) whose columns are only used in conditions that are always true for it, such as `count >= 0` |
| `predicates` | drops conditions that are always true (`x IS NULL OR x IS NOT NULL`), repeated conjuncts and repeated `IN` list items |
| `merge_filter` | turns an inner-joined `(SELECT * FROM t WHERE p) a` into `t AS a` with `p` in the outer `WHERE` |
| `merge_projection` | merges `SELECT f(x.c) FROM (SELECT e AS c FROM ... WHERE p) x` into one query |
| `inner_order` | drops `ORDER BY` without `LIMIT` inside a derived table or CTE |
| `shared_sums` | `SUM(x + 1), SUM(x + 2), ...` become `SUM(x) + k * COUNT(x)`, so many aggregates become two |
| `constant_group_keys` | drops a `GROUP BY` item that names a constant |
| `inline_cte` | writes a CTE that is read once in place (tried with and without) |

After the rules, a search tries deleting one thing at a time (a `DISTINCT`, a `GROUP BY` or one of its keys, a join whose columns are not used, a `WHERE`/`ON`/`HAVING` conjunct) and keeps each deletion the prover proves, until none is left or the time budget (20 s) runs out. A deletion that would leave a column outside `GROUP BY` (proven equivalent, but PostgreSQL rejects it) is not tried.

**Cost guard.** When the caller passes `cost` (the benchmarks pass PostgreSQL's `EXPLAIN` total cost), a proven rewrite or deletion is kept only if the estimate does not rise by more than 2%, and a rewrite the engine cannot plan is dropped. The estimate decides only which proven rewrite to emit; it never stands in for a proof.

**Proof.** A candidate is compared with the input by `prove_equivalent_algebraic` (bag semantics, same columns in the same order, declared NOT NULL columns and primary keys used). If the whole rewrite is not proven, its steps are proven one after another and the longest proven prefix is kept; equivalence is transitive, so the result is still proven against the input. Stars are expanded from the catalog when that helps the prover. Each proof attempt is stopped after 60 s and the whole query after 120 s; a stopped attempt counts as not proven. The prover's standing assumptions (floats are never NaN, runtime errors are not modeled, `ORDER BY .. LIMIT` ties) are listed with each proof.

Three prover additions came with this work: a statement that is only `SELECT * FROM (q)` is read as `q` (so a query whose `ORDER BY .. LIMIT` sits inside such wrappers is compared with its limit at the top); a derived aggregate without `GROUP BY` is read as exactly one row whose values are free, except that a `COUNT` is a number of at least zero (no counterexample is reported from such free values); and `SUM(x + c)` is read as `SUM(x) + c * COUNT(x)` when `x` is a declared integer or decimal column. SQLSolver's Calcite, Spark, TPC-H and TPC-C scores are unchanged by them, with 0 wrong.

The SQL-RewriteBench run also exposed a false proof that was already on master: sqlglot 30.21 parses `x IS NOT NULL` (and `NOT LIKE`, `NOT ILIKE`) as the positive node with a `negate` flag, which the prover ignored, so `IS NOT NULL` was read as `IS NULL`. A join query with `s.y = w.q AND s.y IS NOT NULL` was then "always empty" and equal to anything empty; the optimizer used that to drop five join predicates from PG-AWARE-030, and execution showed different rows. `ast_utils.canonical_negation` now writes these as `NOT (...)` right after parsing, and `tests/test_query_optimizer.py` keeps the case.

## SQL-RewriteBench

[SQL-RewriteBench](https://github.com/SQL-RewriteBench/benchmark) (Apache-2.0, commit `025cf3d`) has 180 PostgreSQL rewrite cases over TPC-DS and DSB in four pools (EQUIV, PERF, ROBUST, PG-AWARE). A method sees each case's input SQL and schema profile and returns a rewrite or nothing. The benchmark executes both, requires identical results, and scores each case with CGOQ: speedup plus a little credit for a simpler statement (its SCS), with unsafe, non-executable and unchanged cases scoring 0. The headline is CGOQ@N over all 180 cases. `tools/rewrite_bench.py` reimplements the runner and calls the benchmark's own SCS code.

```
git clone https://github.com/SQL-RewriteBench/benchmark
# TPC-DS (gregrahn/tpcds-kit) and DSB (microsoft/dsb) data generated with dsdgen at scale factor 1,
# loaded into PostgreSQL databases tpcds and dsb with each kit's CREATE TABLE script, then ANALYZE
python tools/rewrite_bench.py --bench benchmark --method reference --db tpcds_sf10=tpcds --db dsb=dsb   # the benchmark's own rewrites
python tools/rewrite_bench.py --bench benchmark --method kumosql --cost --jobs 3 --db tpcds_sf10=tpcds --db dsb=dsb
```

`--no-exec` proves rewrites without a database, `--split held-out` scores only the held-out cases, and `--rewrites FILE` re-times rewrites saved with `--out`.

| Measure | KumoSQL | Reference rewrites |
| --- | ---: | ---: |
| CGOQ@N (all 180) | **30.72** | 40.42 (paper: 41.00) |
| Rewritten | 142 (all proven, all return the same rows) | 180 |
| Unsafe / non-executable | 0 / 0 | 0 / 0 |
| Geometric-mean speedup over rewritten cases | 1.90x | 2.04x |
| At least 10% faster | 54 | |
| Held-out split (35 cases) | 27.61 | |

| Pool | Cases | Rewritten | CGOQ |
| --- | ---: | ---: | ---: |
| PERF | 44 | 40 | 81.03 |
| PG-AWARE | 30 | 13 | 18.82 |
| ROBUST | 58 | 53 | 15.82 |
| EQUIV | 48 | 36 | 10.06 |

History: 28.69 (130 rewritten) in the first run; 30.72 after the prover kept grouped window queries whole and matched `ORDER BY .. LIMIT` bodies written differently on the two sides, and the runner completed each case's schema profile from the database catalog (the profiles omit some tables, so stars over them could not be expanded). Two cases lost a rewrite in that run (PG-AWARE-018, ROBUST-021) through prover changes merged on master in between. PG-AWARE-018 is proven again after two fixes in this step: the outer-join wrapping rule kept a bare output column's name, and a constant `INTERVAL` reads as one fixed value. ROBUST-021 still gets only a smaller rewrite. The table above is from the run before those fixes.

Data is scale factor 1, not the benchmark's 10, so speedups differ from the paper's machine. Each statement ran once to warm up, then five times alternating with the other, and the medians are compared, so drift in the machine's speed affects both alike (the reference run predates alternation). Cases numbered 4, 9, 14, ... in each pool were held out while writing the rules and never inspected.

What moved the score: the PERF and PG-AWARE pools wrap the real query in layers of `SELECT *` CTEs and cross joins with `COUNT(*)` aggregates whose only use is `count >= 0`. Removing those (`passthrough`, `unused_cte`, `one_row_join`) is where the large speedups come from (up to 66x). Most other rewrites only shorten the statement and run at the same speed. One rewrite is slower (ROBUST-028, 0.74x: an inner `ORDER BY` that PostgreSQL used for a cheaper plan is dropped).

The 38 cases left unchanged are mostly ones the prover cannot read yet: window functions over plain rows, `LIMIT` without a full `ORDER BY`, `ROLLUP`/`GROUPING`, `STDDEV`, dates written `'2001-5-01'`, and others, or rewrites whose proof did not finish.

With `--cost`, the runner also reads `information_schema` (columns, types, NOT NULL, primary keys) for tables a case's profile leaves out.


## WeTune's 50 GitHub performance issues

[WeTune](https://github.com/WeTune/WeTune-code) (Wang et al., SIGMOD 2022, Apache-2.0) collected 50 slow queries from Discourse, GitLab, Spree, Redmine, Lobsters, Solidus and Diaspora, each with the rewrite the project's developers committed (`wtune_data/issues/issues`). `tools/wetune_bench.py` reads them with each application's schema dump (columns, NOT NULL, primary keys, unique indexes whose columns are NOT NULL; foreign keys are not used yet) and reports, per issue:

* whether the prover verifies the developers' rewrite (proven), refutes it (a counterexample), or leaves it unknown;
* whether KumoSQL's own proven rewrite of the original *reproduces* the developers' improvement: for each structural feature the developers reduced (joins, subqueries, `DISTINCT`, `GROUP BY`, `ORDER BY`, predicates, `OR`), KumoSQL's rewrite has at most as many. Rewrites that add structure (OR to UNION) count only when KumoSQL emits the same statement.

```
git clone --depth 1 https://github.com/WeTune/WeTune-code
python tools/wetune_bench.py --wetune WeTune-code
```

| Measure | Result |
| --- | --- |
| Developers' rewrites verified by the prover | 5/50 |
| Refuted (counterexample) | 4/50, all "join elimination". Three rely on Rails associations the schema does not declare (no foreign key, so the counterexamples are databases the schema allows); one relies on a declared foreign key, which the prover does not read yet |
| Unknown | 41/50 (14 no mapping found, 6 `LIMIT` without `ORDER BY`, 4 `IN` subquery shapes, 3 timeouts, timestamps with a time part, outer joins inside subqueries, ...) |
| KumoSQL proven rewrites | 5/50, 0 unproven rewrites emitted |
| Reproduces the developers' improvement | 1/50 (DISTINCT elimination) |

WeTune itself rewrote 38 of the 50 (as its paper reports) with rules discovered offline and checked by its own verifier; KumoSQL's number is a baseline for proof-gated rewriting with no discovered rules.

WeTune's application workloads (8,518 queries from 20 applications) are stored in `wtune_data/wtune.db` through Git LFS, which this environment cannot download, so they are not scored yet.

## ClickBench

[ClickBench](https://github.com/ClickHouse/ClickBench) (CC BY-NC-SA 4.0; its files are read from a checkout, not copied here) times 43 queries over one wide `hits` table. `tools/clickbench_bench.py` rewrites each query from the table definition alone, then runs original and rewrite on PostgreSQL 16 (one warm-up each, then five alternating timed runs, medians compared) and checks that both return the same rows.

The real 100-million-row data set cannot be downloaded here, so `--generate 1000000` fills `hits` with one million synthetic rows (small integer domains, a few repeated strings including the empty string and Google URLs, dates in July 2013). The timings show whether a rewrite removes work; they are not ClickBench results.

```
git clone --depth 1 https://github.com/ClickHouse/ClickBench
createdb clickbench
python tools/clickbench_bench.py --clickbench ClickBench --db clickbench --generate 1000000
```

| Query | Proven rewrite | Same rows | Speedup |
| --- | --- | --- | ---: |
| Q30 | 90 `SUM(ResolutionWidth + k)` become `SUM(ResolutionWidth) + k * COUNT(ResolutionWidth)` | yes | 4.51x |
| Q35 | `GROUP BY 1, URL` (1 names the constant column) becomes `GROUP BY URL` | yes | 1.01x |
| Q36 | `GROUP BY ClientIP, ClientIP - 1, ClientIP - 2, ClientIP - 3` becomes `GROUP BY ClientIP` | yes | 1.04x |

3 of 43 queries are rewritten, all 3 are proven and return the same rows, 1 is at least 10% faster (geometric mean 1.68x over the 3); the other 40 are left unchanged (single-table scans with nothing provably redundant).
