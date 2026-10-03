# Join ordering and cardinality estimation

[Plain-language version](../docs_simple/joinorder.md)

`kumosql.joinorder` estimates how many rows each sub-join of a query returns and
picks a join order from those estimates. It is plain Python on top of sqlglot:
no numpy, no database and no model at planning time.

| Module | What it does |
| --- | --- |
| `query.py` | Reads a select-project-join query into a join graph: relations, per-relation filters, equi-join edges |
| `predicates.py` | Evaluates single-relation filters (`=`, ranges, `IN`, `LIKE`, `BETWEEN`, `IS NULL`, `AND`/`OR`/`NOT`) on sample rows with SQL's three-valued logic |
| `stats.py` | Statistics gathered once from the data (needs DuckDB): row counts, a row sample, and per join-key bins of row and distinct-key counts |
| `estimator.py` | `FactorEstimator`: sizes of connected sub-joins from those statistics |
| `planner.py` | DPccp dynamic programming over bushy join trees (greedy fallback), C_out or hash-join cost, and SQL with the chosen join tree spelled out |

## How the estimator works

* **Filters** run on the stored rows. Tables with at most `sample_rows` rows are
  stored whole, so their filters are exact. When no sampled row passes, the
  estimate is half a sampled row, or the product of the single predicates'
  rates when that is smaller.
* **Lookups fold into the table they describe.** A relation that meets the rest
  of the sub-join through one column only, where that column is unique in it
  (`company_name.id`, `info_type.id`, `title.id`), works as a filter on its
  partner. Each stored partner row is weighted by whether its key passes the
  lookup's filters. That is known exactly when the key's row is stored
  (always for small tables and frequent keys). Otherwise it is the share that
  passes in the key's bin, times the share of keys in that bin that exist in
  the lookup. Folding repeats, so `kind_type` folds into `title`, which then
  folds into `movie_companies`. This carries a filter across a join. "Movies
  made by one company" changes which movie bins the remaining joins see,
  instead of being assumed independent of them.
* **Joins.** Columns that the query sets equal form one equivalence class
  (`a.x = b.y AND b.y = c.z` is a three-way class). Each class is joined bin by
  bin. The key values of a join domain are split into bins: each of the 2,000
  most frequent values gets its own bin, and the rest fall into 1,000 value
  ranges of equal frequency. Inside a bin, rows are assumed to be spread evenly
  over its distinct keys, which is exact for the frequent values.
* **Filters and keys together.** Within each bin, the share of stored rows that
  pass (shrunk towards the overall share) scales that bin. A filter that keeps
  mostly some keys therefore moves the join estimate with it.
* **Frequent keys stored in full.** In a table whose key is unique (users,
  posts, titles), the rows for the most frequent join values are always
  stored. One very active user can decide a large part of a join, and a small
  sample often misses them.
* Different equivalence classes are treated as independent.

This follows FactorJoin (Wu et al., SIGMOD 2023), simplified so that sample-based
per-bin filter rates stand in for its learned single-table models.

## Benchmarks

All benchmark code lives in `kumosql.joinorder.bench` and `tools/joinorder_bench.py`.
It uses DuckDB, which is a dev dependency only, and optionally a Postgres server
for a baseline. Data and caches go to `$KUMOSQL_BENCH_DATA` (default
`~/.kumosql-bench`), never into git.

Exact sub-join sizes come from `bench/truth.py`. It computes a COUNT(*) over a
join by variable elimination (one variable per equivalence class, summed out
in min-degree order), so it never materialises joins with billions of rows. All
2,603 STATS-CEB sub-plans take 38 s, about 100 times faster than running the
joins. Each of the 146 query sizes matches the published value.

Metrics:

* **Q-error**: `max(est/true, true/est)`, with both sides at least 1.
* **Plan cost**: the chosen join tree's C_out (sum of intermediate result sizes)
  under the true sizes, divided by the cost of the tree chosen from true sizes.
  This is free of timing noise.
* **Runtime**: the query run in DuckDB with the chosen tree forced
  (`SET disabled_optimizers='join_order,build_side_probe_side'`, so DuckDB
  keeps both the join order and the plan's choice of which input to hash),
  next to DuckDB's own optimizer.

Postgres' estimates go through the same DPccp optimizer, so only the
cardinalities differ between rows.

### STATS-CEB

Source: [End-to-End-CardEst-Benchmark](https://github.com/Nathaniel-Han/End-to-End-CardEst-Benchmark)
at commit `670cb8d` (Han et al., VLDB 2022). It has 146 queries over the
simplified STATS dataset and 2,603 sub-plan queries. The repository also ships
the estimates of BayesCard, DeepDB, FLAT and NeuroCard for the same sub-plans,
which are scored here unchanged.

```
git clone https://github.com/Nathaniel-Han/End-to-End-CardEst-Benchmark
python tools/joinorder_bench.py stats-ceb --repo End-to-End-CardEst-Benchmark \
    --psql "psql -d stats" --execute
```

Sub-plan Q-error (2,603 sub-plans; 2026-10-02):

| Estimator | p50 | p90 | p95 | p99 | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| **KumoSQL, 10,000-row samples** | **1.18** | **2.48** | **4.3** | **16.9** | **126** |
| KumoSQL in PR #232 (no folding, 400 frequent keys) | 1.36 | 3.66 | 6.0 | 17 | 63 |
| Postgres 16 (`EXPLAIN`, this machine) | 2.05 | 33.8 | 102 | 3,482 | 8.2M |
| BayesCard (published estimates) | 1.18 | 50.4 | 95 | 156 | 8,559 |
| FLAT (published) | 1.68 | 10.4 | 26 | 771 | 119k |
| DeepDB (published) | 1.98 | 18.5 | 61 | 1,027 | 82k |
| NeuroCard (published) | 951 | 889k | 6.1M | 586M | 2.6T |

Before frequent keys were stored in full, the 10,000-row samples gave p99 2,622
and max 8.5M. The worst sub-plans were filters on users meeting very active
users the sample had missed.

End to end (146 queries, DuckDB 1.5.6, 4 threads, 60 s timeout counted as 60 s):

| Join trees chosen by | Total runtime | Timeouts | Plan cost / optimal (mean, p99, max) |
| --- | ---: | ---: | --- |
| DuckDB's own optimizer | 274.5 s | 3 | n/a |
| Postgres 16 estimates + DPccp | 170.9 s | 1 | 1.11, 6.2, 6.4 |
| **KumoSQL estimates + DPccp** | **133.1 s** | 0 | 1.009, 1.15, 1.59 |
| True sizes + DPccp | 134.3 s | 0 | 1, 1, 1 |

Every forced plan returned the published count. Most of STATS-CEB's runtime goes
into its final, very large joins, which every plan has to compute. That is why
runtimes sit close together while plan costs differ a lot.

Which input DuckDB hashes matters as much as the order here. PR #232 let DuckDB
pick the hashed side itself, and our plans then ran in 98.8 s. Without its
join-order pass, though, DuckDB misjudges filtered inputs, and on JOB the same
setting made forced plans twice as slow as DuckDB's own (21.5 s against
15.5 s). Both benchmarks now use one setting: the plan's smaller estimated
input is hashed. On the largest many-to-many STATS joins, hashing the larger
input is sometimes faster. A simple rule for that ("hash the larger side when
the join expands more than 10x") helped some queries and hurt others, so it
was dropped.

Honest limits: the frequent-key storage and the choice of 2,000 frequent keys
over value ranges were made while looking at STATS-CEB and at JOB families
1-16, so the STATS-CEB numbers are tuned on test.

### JOB

Source: [jo-bench](https://github.com/danolivo/jo-bench) at commit `ad516b3`
(BSD-2-Clause): the 113 Join Order Benchmark queries (Leis et al., VLDB 2015)
and the full IMDB data (3.7 GB of CSV, loaded into DuckDB in about 70 s).

```
git clone --depth 1 https://github.com/danolivo/jo-bench
python tools/joinorder_bench.py job --repo jo-bench --psql "psql -d imdb" --execute
```

JOB has no published sub-plan sizes. The harness computes exact sizes for all
71,384 connected sub-joins (one cache file per query; the 17-way queries 29a-c
have 13,246 each and take the longest). **Held out:** families 17-33 (54
queries) were never looked at while developing the estimator; families 1-16
were (`TUNING_FAMILIES` in `bench/job.py`).

Before any JOB work, the STATS-CEB version had p50 2.7 and p99 6,658 on
families 1-16, with plan cost 1.37x optimal. Folding lookups and fixing
`IS NOT NULL` and `NOT LIKE` (newer sqlglot marks them with a flag that was
being ignored) brought that to p50 1.9, p99 241 and 1.34x.

Sub-join Q-error (sub-joins of 2 or more relations; the 17-way queries make up
over half of them):

| Estimator | Queries | p50 | p90 | p99 |
| --- | --- | ---: | ---: | ---: |
| **KumoSQL (6 MB of statistics)** | all 113 | **4.7** | **698** | **87,573** |
| **KumoSQL** | held-out 54 | **5.0** | **815** | **88,020** |
| Postgres 16 | all 113 | 226 | 42,462 | 1.8M |
| Postgres 16 | held-out 54 | 287 | 47,492 | 1.9M |

Plan cost: the chosen tree's C_out under true sizes, divided by the best tree's.

| Estimates | Geomean, all | Geomean, held-out | p90 | max |
| --- | ---: | ---: | ---: | ---: |
| **KumoSQL** | **1.41** | **1.49** | **3.1** | **9.8** |
| Postgres 16 | 2.79 | 2.80 | 10.2 | 38,079 |
| True sizes | 1 | 1 | 1 | 1 |

Runtime, all 113 queries in DuckDB (best of two runs, 4 threads; no timeouts,
and every plan returned the same rows):

| Join trees chosen by | All 113 | Held-out 54 | Geomean per query |
| --- | ---: | ---: | ---: |
| DuckDB's own optimizer | 15.5 s | 8.4 s | 90 ms |
| Postgres 16 estimates + DPccp | 11.3 s | 6.4 s | 70 ms |
| **KumoSQL estimates + DPccp** | **10.8 s** | **6.3 s** | **65 ms** |
| True sizes + DPccp | 10.4 s | 6.2 s | 66 ms |

KumoSQL's plans run JOB 30% faster than DuckDB's optimizer and within 4% of
plans chosen from the true sizes. Estimating all sub-joins takes 1.5 ms each in
pure Python.

### Not available here

CEB-IMDb's 13,644 queries are hosted on Dropbox, which this environment cannot
reach. The CEB repository (`learnedsystems/CEB` at `9eaaadb`) ships the query
templates, so a regenerated workload is possible but would not be the
published one.

## Credits

* Workload and published estimates from Han et al.'s
  [End-to-End-CardEst-Benchmark](https://github.com/Nathaniel-Han/End-to-End-CardEst-Benchmark).
* JOB data and queries from [jo-bench](https://github.com/danolivo/jo-bench) (BSD-2-Clause),
  originally Leis et al., VLDB 2015.
* DPccp from Moerkotte and Neumann, VLDB 2006. Binned join estimation follows
  FactorJoin (Wu et al., SIGMOD 2023). No code was copied.
