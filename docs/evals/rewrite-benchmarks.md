# Query rewriting benchmarks

[Plain-language version](../../docs_simple/evals/rewrite-benchmarks.md)

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

## QueryBooster experiment rewrites

[QueryBooster](https://github.com/ISG-ICS/QueryBooster) (Bai et al., VLDB 2023, GPL-3.0) keeps the inputs of its experiments in `experiments/`: pairs of an original query and a rewrite that a rule produced or a person wrote. Such a rewrite is a claim of equivalence, not a proof. `tools/querybooster_bench.py` checks every pair two ways: the algebraic prover tries to prove it, and a counterexample search looks for a database on which the two sides return different rows. A refuted pair is a **label failure**. It stays in the eval as a negative, together with its counterexample. GPL-3.0 means the files are downloaded at commit `19008ac` when the eval runs, checked against pinned SHA-256 digests and never committed. Only the digests and this harness are stored here.

```
python tools/querybooster_bench.py --write-results          # all 68 pairs, about 3 minutes on 3 cores
python tools/querybooster_bench.py --family tpch-pg --show refuted,unknown
```

| Family | Source | Pairs | Proven | Refuted (label failures) | Unknown |
| --- | --- | ---: | ---: | ---: | ---: |
| `wetune-app` | `Test_wetune.csv`: WeTune's rewrites of Broadleaf, Diaspora and Discourse queries | 30 | 7 | 15 | 8 |
| `rule-training` | the three `Train_*.csv` examples for "LEFT OUTER JOIN to INNER JOIN" and "remove a useless INNER JOIN" | 14 | 5 | 9 | 0 |
| `tweets-cast` | `tweets_cast_{2..5}q.csv`: 18 rows, of which 4 are rule templates (`<x1>` placeholders, not SQL) and 14 are concrete pairs (5 distinct) | 5 | 0 | 5 | 0 |
| `tpch-pg` | `tpch_pg.md`: Tableau's TPC-H query against each rewrite below it (14 by hand, 4 by WeTune, 1 by ChatGPT; the deprecated Q18 included) | 19 | 6 | 3 | 10 |
| **All** | | **68** | **18** | **32** | **18** |

**0 wrong**: no pair is both proven and refuted. The held-out fifth (17 pairs, chosen by a SHA-1 hash of the case id) is 10/17 decided: 2 proven, 8 refuted.

**Schemas.** The WeTune pairs and the training rows use the applications' schema dumps from [WeTune](https://github.com/WeTune/WeTune-code) (Apache-2.0, commit `f99ee9e`, also downloaded and checked by digest). They are read with `tools/wetune_bench.py`, and this harness adds column types, unique indexes over nullable columns and declared foreign keys. The prover gets columns, types, NOT NULL, keys and declared foreign keys. Generated databases respect all of these. TPC-H uses `tests/fixtures/sqlsolver/tpch.schema.sql`. Some training rows rename tables or columns (`posts0`, `id0`, `t`/`tf`). For those, the missing columns are added to the application's schema (nullable, never in a key, typed from the column they are equated with), or the whole schema is inferred when a table is missing. The Twitter pairs have no DDL, so `tweets` and `users` are inferred from the SQL, with `*_at` columns as `TIMESTAMP`. Each case records which schema it used.

**Refutations.** The search tries the prover's own counterexample first, when it is a database the schema allows, then KumoSQL's targeted and random databases (`kumosql.refute`). A difference counts only if DuckDB shows it with its optimizer on and off. When a side has a `LIMIT`, the difference must also appear with the rows loaded in reverse order, so ties cannot decide a pair. DuckDB runs MySQL pairs with MySQL's NULL ordering, and with a case-insensitive collation for Broadleaf (whose tables use MySQL's default `utf8` collation). Diaspora's tables are `utf8mb4_bin`. Each counterexample is shrunk row by row and stored in the run's `--json` output.

What the 32 label failures are:

| Reason | Pairs |
| --- | --- |
| A join is removed although no foreign key is declared (Rails associations the schema does not state), or on an inferred schema without a key | 17: wetune-app 04, 07, 13, 19, 24–28; train-join 0–3; train-join-agg 0–3 |
| `LEFT JOIN` turned into `JOIN` although the `WHERE` keeps unmatched rows (an `OR`, or no condition on the inner side) | 5: wetune-app 10, 15, 20, 22; train-loj-5 |
| `CAST(ts AS DATE) = TIMESTAMP '2016-10-01 00:00:00'` rewritten to `ts = TIMESTAMP '2016-10-01 00:00:00'` (a row at `2016-10-01 01:00:00` separates them) | 5: tweets-cast 0–4 |
| The two sides return different numbers of columns (`SELECT *` for a column list; WeTune's TPC-H Q2 rewrite returns 2 of 3 columns) | 2: wetune-app-02, tpch-q2-wetune-1 |
| A join on a nullable column is removed (`o_auth_application_id` is NULL) | 1: wetune-app-03 |
| Q11 by hand: Tableau's `CASE WHEN total = 0 THEN NULL` guard becomes a `HAVING` that keeps a group when every cost is 0 | 1: tpch-q11-human-1 |
| Q17 by hand: `LOWER(..)` prefix `'med'` becomes the case-sensitive `LIKE 'MED%'`, and the output columns change | 1: tpch-q17-human-1 |

The 18 proofs are WeTune join eliminations backed by declared foreign keys and NOT NULL columns (wetune-app 00, 01, 08, 11), a `WHERE 1 = 0` pair (wetune-app-18), seven `LEFT JOIN` to `JOIN` rewrites whose `WHERE` rejects NULLs (wetune-app 21, 23; train-loj 0–4), the TPC-H rewrites that only add `pg_hint_plan` hints or move a filter (Q7 twice, Q8 twice, Q9), and WeTune's Q18 rewrite. The 18 unknowns are timestamp literals the prover does not read (`'1995-01-01 00:00:00.000'`, `'2021-04-28T06:05:27.000z'`, 6 pairs), `LIMIT` without `ORDER BY` (2), a 17-digit numeric literal (1), derived tables not joined on all their columns (2) and pairs with no row-preserving mapping (7). Two of the unknowns look like label failures on reading, but the search does not reach them: the hand-written and ChatGPT Q2 rewrites replace Tableau's case-insensitive suffix test on `p_type` (`SUBSTR(RTRIM(LOWER(..)))`) with a different `LIKE`.

**Overlap.** `calcite_tests.csv` (228 rows, 227 distinct test names) is SQLSolver's Calcite set. All 228 names are in `tests/fixtures/sqlsolver/calcite_pairs.txt`, and 137 rows have the same text up to aliases. Checked once against the `winoros/wetune` clone, 176 match WeTune's own `calcite_tests` file. Those rows are scored under `sqlsolver-calcite` and are not scored again here. The other files share no pair with `wetune-issues`: none of the 30 WeTune application queries or 14 training queries is one of WeTune's 50 GitHub issues (closest text similarity 0.6).

**Baseline and changes.** The first run (12 proven, 28 refuted, 28 unknown, 0 wrong) printed every pair, held-out ones included. The harness was then changed with all pairs visible, so the score is marked tuned on test (harness only):

* the prover gets the declared foreign keys and compares result columns by position, not by name (+6 proofs);
* the prover's own counterexample is replayed;
* a column added for a renamed training row takes the type of the column it is equated with, because DuckDB rejected the pair before (+1 refutation);
* Tableau's `"public".` schema prefix is dropped, and so is the text after the final semicolon of a markdown block (ChatGPT's explanation). Both changes keep the query's meaning, and they let DuckDB run the TPC-H pairs (+3 refutations).

No SQL is otherwise adapted, and no prover module was changed.

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

## Cost-recommendation validity

Does a rewrite KumoSQL recommends keep the query's meaning, and does it save what the cost estimate says? `tools/cost_validity_bench.py` collects the recommendations of two sources for every statement of a workload and judges them in a fixed order:

1. **Correctness.** Two separate facts, never merged. The proof label KumoSQL attaches (proven, or unproven), and the dataset agreement: original and rewrite run on PostgreSQL 16 and are compared by the rules in [How the rows are compared](#how-the-rows-are-compared).
2. **Estimated benefit.** PostgreSQL's `EXPLAIN` total cost before and after, the local stand-in for a BigQuery dry run. "Cheaper" means at least 2% lower.
3. **Observed benefit.** Median of five alternating runs after a warm-up. "Faster" means at least 10%.

A benefit is counted only for rewrites that passed step 1, and estimates and observations are reported separately.

The sources are the rule pipeline (`canonical_rule_order()` without `format_sql`; it works on BigQuery SQL, so each query is transpiled to BigQuery and back and the round-tripped original is the baseline) and `query_optimizer.optimize` with the cost guard and the database's catalog. The workload is TPC-DS (99 templates, 103 statements) and DSB (53 statements) generated with DSB's `dsqgen` (seed 7, PostgreSQL templates) at scale factor 1; the generated queries are not stored here.

```
python tools/cost_validity_bench.py --workload tpcds=TPCDS_QUERY_DIR --workload dsb=DSB_QUERY_DIR --jobs 4 --out results.json
python tools/cost_validity_bench.py --workload ... --rejudge results.json --out results2.json   # execute the same recommendations again
```

| Source | Recommended | Proven | Same rows | Different rows | Not judged | Estimated cheaper | Faster | Slower | Geomean speedup |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Rule pipeline | 44 | 38 | 37 | 0 | 7 | 0 | 0 | 0 | 1.001x |
| Optimizer | 28 | 28 | 28 | 0 | 0 | 0 | 2 | 0 | 1.017x |

* **What "0 wrong" means.** 65 comparator agreements and 7 recommendations not judged; no wrong result observed under the current comparison policy (rows as a bag with multiplicity, output column names and types equal, sort keys compared including hidden ones, floats rounded to 9 places). It is not a proof and not a rate: the 65 are one workload (PostgreSQL scale factor 1, seed 7), there is no held-out split, and the comparison was made on one dataset. 66 of the 72 carry a proof label and 6 are unproven (single-use CTE inlining around window functions in TPC-DS q23a, q23b, q47, q51 and q57, plus q21); they are labelled unproven, so they are never presented as safe. Of those six, five ran and returned the same rows, and q21 is one of the 7 not judged.
* **Not judged.** For 7 TPC-DS queries (q2, q5, q21, q40, q77, q78, q80) the round-tripped original does not run on PostgreSQL (`ROUND(double precision, int)`, interval arithmetic written as `- 30 AS days`); these are transpiler limits, not rule results.
* **Estimate versus observation.** The rule recommendations (27 parenthesis removals, 24 single-use CTE inlinings) never change PostgreSQL's estimate and never moved runtime by 10%; they make SQL easier to read, not cheaper. No optimizer recommendation was estimated at least 2% cheaper. Two ran at least 10% faster although their estimate barely moved (TPC-DS q38 1.40x and q87 1.42x, both estimated about 0.4% lower), so on this draw the estimate predicted no saving and missed two; none ran slower by 10%. The first run's one estimated saving (DSB `multi_block_queries-query069`, estimate -11%, 1.30x) did not recur: its generated instance differs.
* In the first run, the optimizer's TPC-DS q44 rewrite (dropping `rnk < 11` on one side of a join on `rnk`, implied by the other side) compared as different rows because tied rows came back in another order; re-run, both returned the same 10 rows in the same order. The tie-aware comparison above came from this case.

BigQuery dry-run bytes for the same recommendations need real BigQuery access and have not been measured yet.

### What the score covers

The 72 recommendations are 44 from the rule pipeline and 28 from the optimizer, on 103 TPC-DS and 53 DSB statements. They do not include build recommendations, shared-model proposals, cost attribution or change reports, so the score says nothing about those (cost attribution and change reports are described in [Cost, change reports and the BigQuery dry run](../cost-and-change-reports.md)). There is no held-out split.

A proof label holds under its model, and the tool now copies these assumptions into every recommendation it saves (`assumptions`):

* declared primary keys and NOT NULL columns hold in the data (PostgreSQL enforces them here; BigQuery does not);
* floating-point values are never NaN;
* runtime errors (division by zero, overflow, failed casts) are not modeled;
* column types are not compared by the proof;
* rows that tie under `ORDER BY` and `LIMIT` may be chosen differently.

The optimizer also accepts a proven rewrite whose `EXPLAIN` estimate is at most 2% above the original, so an accepted recommendation is not necessarily cheaper; the "estimated cheaper" column above is the stricter reading (at least 2% lower).

### How the rows are compared

`judge` runs the original and the rewrite and records `outcome`, which is the dataset agreement and nothing else (the proof label is the separate `label` field, and the summary counts `proven` and `unproven` apart from `same_rows`). Its `agreement_basis` field says what the comparison covered:

* **Rows.** The same bag of rows, with multiplicity. Floats are compared rounded to 9 decimal places (`FLOAT_PLACES` in `tools/rewrite_bench.py`). The tolerance is absolute: two floats less than about 5e-10 apart can compare equal, and above about 4e6, where neighbouring doubles are more than 1e-9 apart, the comparison is in effect exact. Decimals and dates are compared as text, so exactly.
* **Order.** Under a top-level `ORDER BY` the sequence of sort keys must match, and rows tied on every key may come back in any order. When a sort key is not an output column, both statements are re-run with the missing keys appended to the select list (as `_kumo_sort_N`); the rows together with their keys must form the same bag and the key sequence must be the same. A rewrite may add tie-breaking keys after the original's. When a key cannot be appended (`SELECT DISTINCT`, a set operation) or the rewrite sorts by fewer keys, the order is not checked and `agreement_basis.order` says `bag only`.
* **Schema.** The output column names and PostgreSQL type OIDs must match; otherwise the outcome is `different_schema`. A rewrite that cannot run is `rewrite_error`; a rewrite counts as wrong when its outcome is `different_rows`, `different_schema` or `rewrite_error`.

Limits that remain. Agreement on one dataset can never show equivalence: `WHERE k = 10` and `WHERE k < 15` agree on any table with no row where k is 11 to 14. The float tolerance hides differences below about 5e-10. The bag-only fallback for `DISTINCT` and set operations does not see a reordering. These are why proof labels and agreement are reported side by side.

The first comparator, which produced the saved 70, compared an ordered result as a bag when its sort key was not projected, rounded floats the same way and did not compare column names. Calling the real `judge` on deliberately wrong pairs, it accepted `ORDER BY k ASC` against `DESC` with a unique hidden `k`, `CAST(1.0000000001 AS DOUBLE)` against `...0002`, `SELECT 1 AS old_name` against `AS new_name`, and `k = 10` against `k < 15`. None of these was seen in the 70. The first and third are now rejected, with regression tests in `tests/test_cost_validity_bench.py`; the float pair and the `k` pair are documented limits and have tests that pin them.

### Rerun status

The stricter comparator was rerun on 2026-10-03 on PostgreSQL 16 with TPC-DS and DSB at scale factor 1 (data from the `dsdgen` of each public kit, queries from `dsqgen` with seed 7: TPC-DS templates with the Netezza dialect file, DSB's PostgreSQL templates), 103 TPC-DS and 53 DSB statements as before. The generated queries are not stored and the first run's query text is not available, so this is a fresh draw from the same templates, not a `--rejudge` of the first run's recommendations. Result: 72 recommendations (44 rules, 28 optimizer), 65 judged and agreeing, 7 not judged (the same seven TPC-DS queries), `different_rows` 0, `different_schema` 0, `rewrite_error` 0. The first run's figures (70 recommendations, 63 agreeing, 1 estimated cheaper at 1.30x) were from the first comparator and a different draw, and are superseded by this run. Estimates and timings differ between draws and runs; the correctness counts are what the rerun was for.
