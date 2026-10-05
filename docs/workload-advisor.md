# Workload advisor: which models to store

[Plain-language version](../docs_simple/workload-advisor.md)

`python -m kumosql advise` ranks materialization changes for a Dataform project by what its job history shows. For each model it asks two questions: would storing this view as a table lower the daily cost of everything that reads it, and would turning this table back into a view? A change is recommended, and counted as a saving, only when the evidence says every read would return the same rows. A change that would need a condition to hold is listed apart as "needs proof" and is never added to any total.

It reads local files and prints a ranked answer. A second command, `python -m kumosql export-warehouse`, writes those files from your own BigQuery warehouse's metadata.

## What it needs

| Input | What it is | How to get it |
| --- | --- | --- |
| `--project DIR` | The Dataform project (or a folder of `.sql` files, or compiled graph JSON), as for `pipeline-report` | Your repository |
| `--jobs FILE` | Completed query jobs: bytes billed, slot time, referenced tables, destination, query text | `export-warehouse --jobs-out`, or any export `load_jobs` reads (JSON, JSON lines, CSV) |
| `--sizes FILE` | Rows, bytes and bytes per column of each table | `export-warehouse --sizes-out` |
| `--schedules FILE` (optional) | `{"project.dataset.model": ["schedule name", ...]}`: which schedules refresh a model | Your workflow configurations ([production schedules](dataform-repositories.md)) |
| prices (optional) | `--usd-per-tib`, `--usd-per-slot-hour`, `--usd-per-gib-month`, `--compute on_demand\|editions` | Your contract. No price is assumed: without one the answer is in bytes billed (or slot milliseconds) per day |

Without `--sizes` the advisor still runs, estimating bytes from column types, and says so; its estimates are then weaker.

## Example

A synthetic project of five models over one source, with 14 days of history (one build of `base` and `mart` a day, 40 dashboard reads of `daily` a day):

```sh
python -m kumosql advise --project my-dataform --jobs jobs.json --sizes sizes.json
```

```text
Counted saving (proven changes only): 155.9 MiB per day of 633.3 MiB measured/estimated baseline (exact search, 3 sets evaluated)

Recommended: proven to keep results (2)
  1. Store p.core.daily as a table
     saves 142.2 MiB per day (estimate), in the best set
     refresh 1/day at 13.7 MiB (refresh estimate, size measured)
     proof: its inputs were built in the same runs (14 runs observed); a table refreshed in those runs after them equals the view at every read outside a run
  ...
Needs proof: NOT counted as savings (2)
  - Store p.core.direct as a table
    would cost 15.3 MiB more per day if it held (estimate, NOT counted)
    why not proven: reads 1 declared source(s); a stored copy is stale between a source change and the next refresh
    condition: p.raw.events changes only before the scheduled refresh, and is never read in between

Would change results: never recommended (1)
  - Store p.core.recent as a table: CURRENT_DATE is evaluated at read time by a view and at refresh time by a table
```

`daily` aggregates a table that is rebuilt in the same run, so a stored copy refreshed right after it is the same at every read outside the run: proven, and the 40 daily reads stop re-aggregating the whole base table. `direct` reads a declared source that the project does not refresh, so a stored copy could be stale: it is listed with the condition a person would have to confirm, and is not counted. `recent` reads `CURRENT_DATE()`, which a table would freeze at refresh time: never recommended. (The numbers are from the test fixture in `tests/test_advise_cli.py`, not from a real warehouse.)

`--format json` prints the same answer as JSON (`recommendations`, `needs_proof`, `changes_results`, `not_worth_it`, `counted`, `calibration`, `workload`); `-o FILE` writes either form to a file instead of the screen. Exit status is 0, or 2 when an input cannot be read.

## How a change is priced

BigQuery on-demand bills the bytes of the columns a query reads from each stored table; a view is expanded into the query that defines it. For every read pattern and every refresh in the history, the advisor follows column lineage through the views to the stored columns the work reads, and prices them with the per-column sizes. A job that was measured keeps its measured cost; a change scales it by the ratio of the estimate after to the estimate before, so the estimate only has to get ratios right. Slot time is scaled the same way (a stand-in, since only completed jobs say what a plan costs in slot time). Every figure keeps its basis (`measured`, `estimate`, `upper_bound`) and measured and estimated figures are never added together. The `calibration` block compares the column estimate with what each measured job template processed (q-error: the larger of predicted over measured and measured over predicted).

The best set of changes is chosen by view selection (`materialization.py`): exact enumeration up to `--exact-limit` candidates, otherwise greedy with local search, with a Lagrangian upper bound when the problem has the right shape so the gap to the optimum is known. `--budget-bytes` caps the extra storage the chosen set may add. Storage is priced against compute only when both rates are given.

## The proof gate

Storing a view changes when its rows are computed, not what it says, so the check is whether any read could tell the difference. The advisor labels each candidate:

| Label | Meaning | Listed under | Counted |
| --- | --- | --- | --- |
| `proven` | Every stored input is refreshed by the same schedules (`--schedules`) or was built in the same runs in the history, and nothing reads a clock, `RAND()` or a UUID | Recommended (or "Proven, but no saving") | Yes, when it saves |
| `conditional` | An input is a declared source or is refreshed on another schedule, so a stored copy can be stale between a change and the next refresh | Needs proof, with the conditions | Never |
| `unknown` | The model does not parse, or is not a model of the project | Needs proof | Never |
| `changes_results` | The model reads `CURRENT_DATE()`, `RAND()`, `GENERATE_UUID()` or similar | Would change results | Never |

"Proven" here is an argument about freshness and determinism from the project and its history, not a prover run on two different queries: store-and-unstore candidates leave the SQL text alone. Changes that rewrite SQL, such as extracting a shared model, must carry a prover proof or the shared-model patch checks ([Shared models](shared-models.md)); the advisor does not generate those yet.

## Exporting from your warehouse

```sh
python -m kumosql export-warehouse --project my-billing-project --region us \
    --jobs-out jobs.json --sizes-out sizes.json --days 14 --dataset analytics
```

| Output | Source |
| --- | --- |
| jobs | `region-REGION.INFORMATION_SCHEMA.JOBS_BY_PROJECT`: completed query jobs of the last `--days` days (default 14) with no error, newest `--max-jobs` (default 100000) |
| sizes | `region-REGION.INFORMATION_SCHEMA.TABLE_STORAGE`: rows and logical bytes per table, limited by `--dataset` and `--table dataset.table` |
| per-column bytes | Free BigQuery dry runs: one `SELECT * FROM t` for the column names and types, then one `SELECT col FROM t` per column; the dry run's estimated bytes are that column's size. `--skip-columns` omits them |

Properties, each checked by `tests/test_warehouse_export.py`:

- **Read-only metadata.** Every statement is checked to be exactly one read-only `SELECT` before it is sent. The only statements that run are the two INFORMATION_SCHEMA reads; the per-column probes are dry runs, which read no data and cost nothing.
- **Capped.** Each INFORMATION_SCHEMA read is dry-run first and refused when its estimate is over `--max-bytes-billed` (default 1 GiB), and runs with BigQuery's `maximumBytesBilled` set to the same cap. A read of a long history can still bill (BigQuery has a 10 MB minimum per query); lower `--days` or narrow the tables to keep it small.
- **Explicit project.** `--project` (the project billed and whose metadata is read) and `--region` are required. Nothing is discovered from the environment, and no file is written unless you pass `--jobs-out` or `--sizes-out` (an existing file needs `--overwrite`).
- **See before you run.** `--dry-run` prints the SQL it would run and the plan, sends nothing and reads no credentials.
- **Refuses rather than truncates.** More tables than `--max-tables` (default 200) is an error that asks you to narrow; a full job page prints a warning that older jobs are missing; a column whose dry run fails (for example a table that requires a partition filter) is listed on stderr and left out, and the advisor then estimates it from its type.

Credentials are those of the dry-run command: `BQ_ACCESS_TOKEN`, a service account key, or Application Default Credentials (`pip install '.[bigquery]'`). The reads need permission to run query jobs, to list the project's jobs (`bigquery.jobs.listAll`) and to see table storage metadata; the per-column dry runs need access to plan queries on the tables.

The jobs file holds query text and user emails from your warehouse. Keep it private and do not commit it. `--omit-query-text` and `--omit-user-email` leave those fields out; without query text the advisor groups reads by the tables they touch, which prices them less precisely.

## Limits of the evidence

- Savings are **estimates** scaled from measured jobs, not measured savings. Whether an accepted change delivered what was predicted is what the measured before-and-after comparison in `savings.py` is for.
- The accuracy of the model against measured runtimes is being evaluated offline on the JOB and STATS-CEB workloads in DuckDB, in a separate piece of work still in progress; there are no scores for it yet and none are claimed here. The unit tests check the arithmetic and the proof gate on synthetic data; they say nothing about how close the estimates are on a real warehouse.
- The cost model is about bytes billed and, for editions, slot time scaled by bytes. It does not model query plans, so a change that mostly alters plan shape is not captured.
- A view with no stored size is bounded by its widest input (`upper_bound`); the output says which sizes were measured.
- Run-to-run variation in a job's bytes (partition pruning, clustering) is not modelled: one measured cost per read pattern is scaled.
- The tests use fakes in place of BigQuery; `export-warehouse` has not been run against a real warehouse by its author.

## Code and tests

| Module | Role |
| --- | --- |
| `advisor.py` | `advise(pipeline, jobs, sizes=, pricing=, schedules=)`: candidates, column-level bytes, freshness evidence |
| `workload.py` | Sorts job history into builds and reads per model; read templates (query text is never exported) |
| `materialization.py` | View selection and its upper bound |
| `cost_model.py` | `Pricing`, billed bytes, calibration and q-error |
| `backtest.py` | Rank correlation, top-k precision and measured before-and-after savings |
| `advice_report.py` | The text and JSON output of `advise` |
| `warehouse_export.py` | `export-warehouse` |

Tests: `tests/test_advise_cli.py`, `tests/test_warehouse_export.py`, `tests/test_advisor.py`, `tests/test_materialization.py`.

See also: [Cost, change reports and the BigQuery dry run](cost-and-change-reports.md) for cost attribution and the measured savings ledger.
