# Join ordering and cardinality estimation

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
  stored whole, so their filters are exact.
* **Joins.** Columns that the query sets equal form one equivalence class
  (`a.x = b.y AND b.y = c.z` is a three-way class). Each class is joined bin by
  bin. The key values of a join domain are split into bins: each of the 400
  most frequent values gets its own bin, and the rest fall into 600 value
  ranges of equal frequency. Inside a bin, rows are assumed to be spread evenly
  over its distinct keys, which is exact for the frequent values.
* **Filters and keys together.** Within each bin, the share of stored rows that
  pass the relation's filters (shrunk towards the overall share) scales that
  bin. A filter that keeps mostly some keys therefore moves the join estimate
  with it.
* **Frequent keys stored in full.** In a table whose key is unique (users,
  posts, titles), the rows for the 400 most frequent join values are always
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
  (`SET disabled_optimizers='join_order'`), next to DuckDB's own optimizer.

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
| **KumoSQL, 10,000-row samples (1.2 MB)** | **1.36** | **3.66** | **6.0** | **17** | **63** |
| KumoSQL, 30,000-row samples (3.1 MB) | 1.32 | 3.30 | 4.8 | 12.5 | 36 |
| Postgres 16 (`EXPLAIN`, this machine) | 2.05 | 33.8 | 102 | 3,482 | 8.2M |
| BayesCard (published estimates) | 1.18 | 50.4 | 95 | 156 | 8,559 |
| FLAT (published) | 1.68 | 10.4 | 26 | 771 | 119k |
| DeepDB (published) | 1.98 | 18.5 | 61 | 1,027 | 82k |
| NeuroCard (published) | 951 | 889k | 6.1M | 586M | 2.6T |

Before frequent keys were stored in full, the 10,000-row samples gave p99 2,622
and max 8.5M. The worst sub-plans were filters on users meeting very active
users the sample had missed.

End to end (146 queries, DuckDB 1.5.6, 4 threads, 60 s timeout counted as 60 s):

| Join trees chosen by | Total runtime | Timeouts | Plan cost / optimal (median, max) |
| --- | ---: | ---: | --- |
| DuckDB's own optimizer | 275 s | 3 | n/a |
| Postgres 16 estimates + DPccp | 101.5 s | 0 | 1.00, 6.41 |
| **KumoSQL estimates + DPccp** | **98.8 s** | 0 | 1.00, 1.59 |
| True sizes + DPccp | 97.9 s | 0 | 1.00, 1.00 |

Every forced plan returned the published count. Most of STATS-CEB's runtime goes
into its final, very large joins, which every plan has to compute. That is why
the estimators sit close together on runtime while differing a lot on plan cost.

Honest limits: the frequent-key storage was added after looking at STATS-CEB's
worst cases, so these numbers are tuned on test. JOB is kept as the held-out
workload: its query families were not looked at while building the estimator.

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
