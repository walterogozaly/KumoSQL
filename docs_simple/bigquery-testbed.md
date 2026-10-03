# A BigQuery project for trying the reports

[All simple guides](README.md) · [Full reference](../docs/bigquery-testbed.md)

The example in `examples/bq_testbed/` builds a deliberately messy set of BigQuery models and runs a repeatable workload. This gives KumoSQL real tables and real job history for graph and cost experiments.

## What you get

The example samples public ecommerce data into small raw tables. It builds staging views, reports, chains of views, and several models doing overlapping work. The workload then reads the sampled tables so you can investigate repeated scans and model roles.

The default dataset name is `kumosql_messy`. Tables expire after 60 days. Repeating the setup replaces derived tables and views; raw copies are reused unless you ask to refresh them.

## Before running it

This example contacts BigQuery and creates or replaces objects in the chosen project. You need the Google Cloud SDK, a project you can write to, and the setup commands in [the full run instructions](../docs/bigquery-testbed.md#run-it).

Unlike a local rewrite example, this runs queries and creates storage. Use a dedicated demo project.

The script caps bytes per job and checks a running dry-run estimate before billable jobs. These controls help bound the workload; they do not replace project controls. The full guide explains a sandbox project without billing, daily query quotas, and why a budget alert alone is not a hard spending cap.

## Connect it to KumoSQL

Use an account with the catalog and job-history permissions listed in [Access for KumoSQL](../docs/bigquery-testbed.md#access-for-kumosql). Building the demo needs write access too; reading it for reports needs a narrower set of permissions.

Load the project and job history into the app to see measured usage alongside repeated-work opportunities. The purpose is to explore reports on known messy data, not to benchmark all production workloads. See [cost and change reports](cost-and-change-reports.md).
