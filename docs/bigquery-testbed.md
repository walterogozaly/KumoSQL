# BigQuery test bed

`examples/bq_testbed/` builds a deliberately messy model layer in a BigQuery project and runs a repeatable query workload, so the project has real job history (`INFORMATION_SCHEMA.JOBS`) for exercising KumoSQL's graph, cost, table-role and repeated-work features.

## What it builds

A dataset (default `kumosql_messy`, location `US`) over `bigquery-public-data.thelook_ecommerce`:

- **Raw**: sampled copies of users, orders, order items and products (10% of users by default). Everything downstream reads these small tables, never the public data.
- **Views**: staging, dimensions, facts, and an eight-layer view chain.
- **Overlapping work**: a state-level revenue rollup that exists as a view, as a second view built from a different path, as a finer-grained monthly view, and as a materialised table that other marts still recompute; likewise daily and weekly sales.
- **Inconsistent dimensions**: `dim_customer` and `dim_customers` are near-twins, and some marts join the staging views or a lookup table directly instead of a dimension.
- **Near-duplicates**: pairs of views that differ only by a filter or a `LIMIT`.
- **Workload**: dashboard tiles (the same text every round), scheduled-style rollups that redo work a model already does, and templated ad-hoc queries. Jobs are labeled `kumosql_testbed:1` and `role:<dashboard|etl|adhoc|build>`.

## Run it

Requires the [Google Cloud SDK](https://cloud.google.com/sdk) (`bq`) and `gcloud auth login` as a user who can create datasets in the project.

```bash
python examples/bq_testbed/build_testbed.py --project kumosql --print-only      # show all SQL, no access needed
python examples/bq_testbed/build_testbed.py --project kumosql --estimate-only   # dry-run the raw copy
python examples/bq_testbed/build_testbed.py --project kumosql                   # build, then run 3 rounds
python examples/bq_testbed/build_testbed.py --project kumosql --workload-only --rounds 5   # add more history
```

It is idempotent: views and tables are replaced, raw copies are reused unless `--refresh-raw` is set, and reruns only add job history. It uses only DDL and `CREATE TABLE AS SELECT` (no DML) and every table expires after 60 days, so it works in the free [BigQuery sandbox](https://cloud.google.com/bigquery/docs/sandbox).

## Cost

- Every job carries `maximum_bytes_billed` (`--max-job-mb`, default 500 MB); BigQuery fails a job that would exceed it, at no charge.
- Before each billable job the script dry-runs it and stops if the running estimate passes `--max-total-gb` (default 5).
- Expected scan: the raw copy reads the public tables once (users, orders, order items, products, on the order of a few hundred MB in total). The workload then reads only the small sampled tables; each query is billed at BigQuery's 10 MB minimum, so 3 rounds (about 75 queries) bills under 1 GB. Treat a full run as low single-digit GB.
- On-demand pricing is about $6.25 per TiB, so a full run costs cents at most, and the first 1 TiB per month is free. Storage is a few MB.

## Guards on the project

The script's caps are a courtesy; set project-level limits so nothing can overspend:

1. **Hard cap: the sandbox.** A project with no billing account is the sandbox and cannot be charged (10 GB storage, 1 TiB queries per month, 60-day expiry). This is the safest choice if the test bed fits, and it does.
2. **Hard cap: daily query quota.** If billing is enabled, go to IAM & Admin, Quotas & System Limits, filter for "Query usage per day" (BigQuery API), and set a custom limit on the project (and per user), for example 10 GiB. Jobs over the limit fail instead of billing. This is the only setting here that actually stops spend.
3. **Alert: budget.** In Billing, Budgets & alerts, create a small budget (for example $5) with alerts at 50%, 90% and 100%. A budget only notifies; it does not stop usage.

## Access for KumoSQL

Give the account or service account KumoSQL runs as these roles on the project:

| Role | Why |
| --- | --- |
| BigQuery Data Viewer (`roles/bigquery.dataViewer`) | read views, tables and schemas |
| BigQuery Job User (`roles/bigquery.jobUser`) | run dry-runs and queries |
| BigQuery Resource Viewer (`roles/bigquery.resourceViewer`) | read `INFORMATION_SCHEMA.JOBS` for all users |

Building the dataset needs write access as well (BigQuery Data Editor or Owner on the project), so run the script as yourself rather than as the read-only service account. Job history is kept for 180 days.

To check what the workload produced:

```sql
SELECT role, COUNT(*) AS jobs, SUM(total_bytes_billed) / POW(1024, 2) AS mb_billed
FROM (
  SELECT (SELECT value FROM UNNEST(labels) WHERE key = 'role') AS role, total_bytes_billed
  FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
  WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)
    AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'kumosql_testbed')
)
GROUP BY role
```
