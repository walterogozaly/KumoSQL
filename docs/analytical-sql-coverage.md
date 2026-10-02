# Analytical SQL coverage: TPC-DS, DSB and SQLStorm

`tools/analytical_coverage.py` runs public analytical benchmark queries through every KumoSQL stage. It extends the [syntax coverage suite](bigquery-syntax-coverage.md), which has one small case per construct, to large real-world corpora of nested queries, aggregates, correlated subqueries and windows. For the rewrites measured on the benchmarks' real data (TPC-H, TPC-DS and JOB on IMDB), see [transformation-bench.md](transformation-bench.md).

## The score

None of these benchmarks has an official score for a static tool, so this one is KumoSQL's own:

- **Clean**: the share of queries that every stage handles with no crash, no timeout and no silent loss. Silent loss means a lost read, a rewrite proven equivalent that returns different rows, or a query called different from itself.
- **Full**: the share where every stage also gives a real answer instead of an honest "unsupported".

Every query has exactly one outcome: **pass** (full), **unsupported** (clean, but some stage declined), **fail**, **timeout** or **error**. Queries that sqlglot cannot convert from Postgres to BigQuery count as **convert** and are left out of the score. The supported-subset score is pass / (pass + fail + timeout + error).

The stages are those of the syntax suite (parse, load, graph and column lineage, fingerprint, cleanup, format, prover), plus **execution**. In the execution stage, the original query and each rewrite (the cleanup rules and the formatter) run in DuckDB on generated tables built from the corpus's schema. A rewrite that was marked proven but returns different rows is a failure. A rewrite that returns different rows but was *not* marked proven was caught by the verifier, so it is reported (`caught`) but not counted as damage. Two kinds of difference are not counted as wrong, because both sides are valid answers:

- With a shared `ORDER BY ... LIMIT`, DuckDB may keep different tied rows. The check then compares the rows before the limit.
- A tie-sensitive window such as `ROW_NUMBER` can differ between any two runs. This is reported as unsupported.

The prover stage compares each query with itself, given the corpus's table columns (as a saved BigQuery catalog would supply them).

## Corpora

None of the data is checked in. `tools/benchmark_corpora.py` fetches each corpus at a pinned commit into `$KUMOSQL_BENCH_DIR` (default `~/.cache/kumosql-bench`):

| Corpus | Source (pinned commit) | Licence | Queries |
|---|---|---|---:|
| `sqlstorm/stackoverflow`, `tpch`, `tpcds`, `job` | [SQLStorm](https://github.com/SQL-Storm/SQLStorm) v1.0, LLM-generated analytical queries (`b3bb0b9`) | MIT | 18,251 / 17,036 / 15,242 / 11,714 |
| `sqlstorm-v0/tpch`, `tpcds`, `job` | SQLStorm v0.0, the official TPC-H, TPC-DS and JOB templates instantiated by each benchmark's own generator | MIT (queries: TPC and JOB terms) | 44,000 / 51,500 / 56,500 |
| `dsb` | [DSB](https://github.com/microsoft/dsb) (`ec9a156`), 52 TPC-DS-derived templates, three seeds each, generated locally with its `dsqgen` | MIT | 156 |

Schemas come from DSB's `tpcds.sql`, SQLStorm's StackOverflow `schema.sql`, the [Join Order Benchmark](https://github.com/gregrahn/join-order-benchmark) `schema.sql` (`a396036`) and the standard TPC-H DDL.

**Held out.** One query in five, chosen by a hash of its id (`benchmark_corpora.held_out`), is held out. `--split dev` (the default) never runs those queries; `--split held-out` runs only them, for a final score. The first measurements below used `--split all` on an even sample, before the split existed, so their held-out share was seen during development.

```
python tools/benchmark_corpora.py fetch sqlstorm dsb
python tools/analytical_coverage.py                         # 200 dev queries from each corpus
python tools/analytical_coverage.py dsb --limit 0 --reasons # every DSB dev query, reasons grouped
python tools/analytical_coverage.py --split held-out --limit 60
python tools/analytical_coverage.py --log run.jsonl         # resumable: appends each result as it finishes
```

## Results

<!-- results:start -->
Measured 2026-10-02 on 60 queries spread evenly through each corpus (`--split all --limit 60`), sqlglot 30.21, master `5701ec3`:

**354/475 (74.5%) of queries pass every stage, 0 wrong. 475/475 are clean.** The baseline on the same queries before the fixes below was 243/475 (51.2%). The eval first merged at 334/475. Later prover merges in other threads made 20 more TPC-DS cleanup rewrites provable (sqlstorm-v0/tpcds 20/60 → 40/60), and some cleanup and execution counts moved by one or two. The 70 queries in the held-out fifth score 53/70 (75.7%), but that split was introduced after this sample was used, so it is not a true held-out score.

| Corpus | Queries | Converted | Clean | Full | pass / unsupported / fail / timeout / error | parse | load | graph | fingerprint | cleanup | format | prover | execution |
|---|---:|---:|---:|---:|---|---|---|---|---|---|---|---|---|
| dsb | 60 | 59 | 59/59 (100.0%) | 59/59 (100.0%) | 59 / 0 / 0 / 0 / 0 | 59 ✅ | 59 ✅ | 59 ✅ | 59 ✅ | 59 ✅ | 59 ✅ | 59 ✅ | 55 ✅ |
| sqlstorm-v0/job | 60 | 60 | 60/60 (100.0%) | 60/60 (100.0%) | 60 / 0 / 0 / 0 / 0 | 60 ✅ | 60 ✅ | 60 ✅ | 60 ✅ | 60 ✅ | 60 ✅ | 60 ✅ | 60 ✅ |
| sqlstorm-v0/tpcds | 60 | 60 | 60/60 (100.0%) | 40/60 (66.7%) | 40 / 20 / 0 / 0 / 0 | 60 ✅ | 60 ✅ | 60 ✅ | 60 ✅ | 60 ✅ | 60 ✅ | 40 ✅ 20 ⚪ | 60 ✅ |
| sqlstorm-v0/tpch | 60 | 60 | 60/60 (100.0%) | 60/60 (100.0%) | 60 / 0 / 0 / 0 / 0 | 60 ✅ | 60 ✅ | 60 ✅ | 60 ✅ | 60 ✅ | 60 ✅ | 60 ✅ | 60 ✅ |
| sqlstorm/job | 60 | 57 | 57/57 (100.0%) | 21/57 (36.8%) | 21 / 36 / 0 / 0 / 0 | 57 ✅ | 57 ✅ | 57 ✅ | 57 ✅ | 31 ✅ 26 ⚪ | 57 ✅ | 21 ✅ 36 ⚪ | 48 ✅ |
| sqlstorm/stackoverflow | 60 | 59 | 59/59 (100.0%) | 30/59 (50.8%) | 30 / 29 / 0 / 0 / 0 | 59 ✅ | 59 ✅ | 59 ✅ | 59 ✅ | 42 ✅ 17 ⚪ | 58 ✅ 1 ⚪ | 30 ✅ 29 ⚪ | 34 ✅ |
| sqlstorm/tpcds | 60 | 60 | 60/60 (100.0%) | 41/60 (68.3%) | 41 / 19 / 0 / 0 / 0 | 60 ✅ | 60 ✅ | 60 ✅ | 60 ✅ | 54 ✅ 6 ⚪ | 60 ✅ | 41 ✅ 19 ⚪ | 58 ✅ |
| sqlstorm/tpch | 60 | 60 | 60/60 (100.0%) | 43/60 (71.7%) | 43 / 17 / 0 / 0 / 0 | 60 ✅ | 60 ✅ | 60 ✅ | 60 ✅ | 55 ✅ 5 ⚪ | 60 ✅ | 43 ✅ 17 ⚪ | 56 ✅ 1 ⚪ |
| **all** | 480 | 475 | 475/475 (100.0%) | 354/475 (74.5%) | 354 / 121 / 0 / 0 / 0 |  |  |  |  |  |  |  |  |

Execution counts are lower than the query counts because some originals do not run in DuckDB after translation, and queries that no rewrite changed have nothing to execute. Both are `n/a`. One execution ⚪ is a tie-sensitive window. The supported-subset score is 354/354, since nothing failed, timed out or crashed.
<!-- results:end -->

## What this found and fixed

- **The formatter re-cased unquoted names.** The `capitalisation` rule group includes sqlfluff's identifier rule (CP02), so `FROM Posts` became `FROM POSTS` and `AS PostId` became `AS POSTID`. BigQuery table names are case sensitive, and an alias's case is the output column's name. The verifier refused to approve those changes, so the formatter was failing on almost every mixed-case query. CP02 now runs only when named on its own in the format settings.
- **Windows were never provable.** The conservative prover refused every query with an `OVER` clause. Window functions that give tied rows the same value are now accepted, because their result does not depend on how ties are broken: `RANK`, `DENSE_RANK`, `PERCENT_RANK`, `CUME_DIST`, and aggregates over a whole partition or a `RANGE` frame. `ROW_NUMBER`, `LAG`/`LEAD`, `FIRST_VALUE`, `NTILE` and `ROWS` frames are still refused. A named window (`OVER w` with `WINDOW w AS (...)`) is spelled out first (`named_windows.py`), so it is judged like the window it names; a reference that repeats a clause its base already has, or names an unknown window, is left alone.
- **A shared `ORDER BY ... LIMIT` blocked proofs.** Nearly every TPC-DS query ends in `ORDER BY ... LIMIT 100`. Two queries that end in the same ordering, by output columns only, and the same `LIMIT`/`OFFSET` are now proven when the rows underneath are. A `LIMIT` at least as large as the most rows a query can return (a global aggregate returns one row, and a `UNION ALL` of them returns their sum) does nothing, so it is dropped.
- **Column lineage through `SELECT *` CTEs.** `WITH f AS (SELECT * FROM t WHERE ...) SELECT f.x` traced `x` to `t.*` instead of `t.x`. Also, sqlglot treats earlier CTEs as sources of a later `SELECT *` CTE, and those are no longer followed.
- **Generated data ignored the queries' constants.** Filters like `info = 'top 250 rank'` never matched the generator's small fixed value set, so execution checks compared empty results and agreed trivially. `check_result_equivalence` now adds each query's constants, plus a string matching each `LIKE` pattern, to the values it draws (half the time). Pass `use_query_constants=False` for the old behaviour.

Each fix has a regression test, and three analytical cases were added to the syntax suite (`query/analytical_*`).

## Gaps that are not fixed here

- **Prover**: tie-sensitive windows (`ROW_NUMBER`, `LAG`, `ROWS` frames), `STRING_AGG`/`ARRAY_AGG` without `ORDER BY`, and `LIMIT` without `ORDER BY` stay unknown. They are nondeterministic, so unknown is the right answer.
- **Formatter**: sqlfluff cannot parse a few queries (reported as `parse_error`), and those are left unchanged.
- **Convert**: about 1% of queries use Postgres syntax that sqlglot cannot convert to BigQuery. They are outside the score.
