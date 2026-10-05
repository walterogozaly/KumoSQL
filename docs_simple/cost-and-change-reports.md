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

Replace the project name. The check compares the output schemas as well as planning both statements. Equal schemas do not establish equal values. If BigQuery's answer carries no output schema, the report says the schemas were not compared (`not_run`, `schema_matches=unknown`) instead of calling them a match. Fewer estimated bytes are a planning signal, not measured savings.

## Read cost with its source

The Cost page can show repeated work from a loaded project. Add job history to attribute measured billed bytes to models.

Some jobs cannot be attributed. They stay in an `unattributed` bucket with a reason, so the totals still account for them. Measured usage and estimates remain separate.

Job history can come as a flat export (one row per job) or as BigQuery API job resources, where the numbers sit inside `statistics`. Both are read. If a job has no recorded billed bytes, it is not counted as free: it is left out of the totals and the number of such jobs is reported as `unmeasured`, so a small total is not mistaken for a cheap one. A job that did really bill zero bytes still counts as zero.

Example: a job resource that reports one TiB billed adds one TiB. A resource with no `statistics` section adds nothing and shows up in the `unmeasured` count.

When you give a price per TiB, the result repeats the price, currency, billing model and region you supplied, and says what an invoice includes that this number does not (BI Engine and reservation charges, storage, discounts, credits, taxes, jobs outside the history). It is billed bytes times your rate, nothing more. The [full reference](../docs/cost-and-change-reports.md) has the field names; it does not record how closely any figure matches a real invoice.

A repeated query is an opportunity to investigate. Sharing it can have storage or refresh costs, and it might not reduce the jobs that actually run. Inspect the proposal's validation and cost rationale.

## Decide which models to store

Job history can also show which views are read so often that storing them as tables would cost less, and which tables are barely read. `python -m kumosql advise` ranks those changes and counts a saving only when the readers would get the same rows. See [Which models should be stored?](workload-advisor.md).

## Compare two project versions

```sh
python -m kumosql change-report path/to/base path/to/head -o report.json
python -m kumosql ci-check report.json --comment-out comment.md
```

The first command compares the two project folders. It reports changed models, evidence, downstream consumers, and existing work that may be reused. The second turns the report into a CI conclusion and a Markdown comment file; writing that file does not post it to GitHub.

A model can change without its file changing. If a model says `SELECT *` and the table it reads gains a column, the model's text is the same but its output has a new column that flows on to every reader. When you give the report the table columns for each side (`--base-source-schema` and `--head-source-schema`, the same JSON as for `pipeline-report`), it compares what each model resolves to, not only its text. A model whose output columns, column lineage or tables read differ is listed as unproven with a `contract` entry saying what moved, so the CI conclusion is `neutral` instead of `success`. A text edit that would otherwise be proven is also downgraded when the output moved. Without those files the report only sees what the project files can tell it, so a `success` means "no change the files reveal", not "outputs unchanged". Column types are not compared yet.

A model is also reported as changed when only its surroundings changed, even with identical query text: a new statement that runs before or after the query (for example a `DELETE`), a table that became a view, an added dependency, or a changed not-null or unique-key assertion. Those changes are shown as unproven, so the CI conclusion is never success for them. For example, a model whose query stays `SELECT x FROM src` but gains `DELETE ... WHERE x = 1` afterwards now appears in the report. Details are in the [full reference](../docs/cost-and-change-reports.md).

In the app, Change reports can compare the connected git project with another branch.

A repeated piece of SQL that reads a column of the query around it, such as a correlated subquery `(SELECT COUNT(*) FROM u WHERE k = x)` where `x` comes from the outer query, cannot become a shared table of its own, so it is never marked ready or proven. Details are in the [full reference](../docs/cost-and-change-reports.md).

Read a proposal's proof, consumer coverage, assumptions, and readiness together. An incomplete consumer list or unknown cost rationale is still incomplete, even when part of the SQL has a proof. The [UI roadmap](ui-roadmap.md) explains the API payloads behind the views.
