# Cost and change reports

[All simple guides](README.md) · [Full reference](../docs/cost-and-change-reports.md)

These reports help you review a change: whether BigQuery can plan it, what depends on it, and what evidence supports it. Cost numbers need their own context.

## A dry run checks planning

A BigQuery dry run asks BigQuery to plan SQL without executing the query. It checks names and types and estimates bytes processed.

From a checkout, set up the optional integration:

```sh
python -m pip install ".[bigquery]"
gcloud auth application-default login
```

The login command needs the Google Cloud SDK. These Application Default Credentials are different from the CLI login created by `gcloud auth login`.

```sh
python -m kumosql dry-run original.sql --rewritten rewritten.sql --project my-project
```

Replace the project name. The check compares the output schemas as well as planning both statements. Equal schemas do not establish equal values. Fewer estimated bytes are a planning signal, not measured savings.

## Read cost with its source

The Cost page can show repeated work from a loaded project. Add job history to attribute measured billed bytes to models.

Some jobs cannot be attributed. They stay in an `unattributed` bucket with a reason, so the totals still account for them. Measured usage and estimates remain separate.

A repeated query is an opportunity to investigate. Sharing it can have storage or refresh costs, and it might not reduce the jobs that actually run. Inspect the proposal's validation and cost rationale.

## Compare two project versions

```sh
python -m kumosql change-report path/to/base path/to/head -o report.json
python -m kumosql ci-check report.json --comment-out comment.md
```

The first command compares the two project folders. It reports changed models, evidence, downstream consumers, and existing work that may be reused. The second turns the report into a CI conclusion and a Markdown comment file; writing that file does not post it to GitHub.

In the app, Change reports can compare the connected git project with another branch.

Read a proposal's proof, consumer coverage, assumptions, and readiness together. An incomplete consumer list or unknown cost rationale is still incomplete, even when part of the SQL has a proof. The [UI roadmap](ui-roadmap.md) explains the API payloads behind the views.
