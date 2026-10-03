# Understanding a whole pipeline

[All simple guides](README.md) · [Full reference](../docs/pipeline-analysis.md)

A pipeline is a set of queries connected by the tables they read. For example, `orders` feeds `daily_sales`, which feeds `monthly_sales`. Changing `orders` can affect both summaries.

KumoSQL reads a Dataform project or a folder of SQL files and builds this dependency graph.

```sh
python -m kumosql pipeline-report path/to/project -o report.json
python -m kumosql ui --project path/to/project
```

Replace the path with your project folder. The first command writes a JSON report; the second shows the project in the app. When available, a compiled Dataform graph provides exact compiled SQL and is preferable to unresolved templates.

## Follow a change

To ask about removing a column:

```sh
python -m kumosql pipeline-report path/to/project --assess drop_column --target demo.analytics.daily_sales.total
```

Replace the target with your table and column. A direct reader can be marked `breaks`, a reader further along the chain `indirect`, and a model the analyzer cannot understand `unknown`.

**Lineage** follows where data came from. Table lineage says that `monthly_sales` reads `daily_sales`; column lineage says which input columns contribute to `monthly_sales.total`. A column used only in a filter or join has no value descendants but still changes which rows come out, so use the change assessment for the blast radius. If models depend on each other in a loop, no order exists: the report marks the order as incomplete and lists those models.

Statements that change tables also retain their inputs and outputs. A script that inserts into `first` and then `second`
lists both written tables, and a rename connects the old name to the new one. These table connections do not guarantee
that every assigned column can be traced. Nested fields of a stored STRUCT still trace to the containing column.
A partition name such as `events$__UNPARTITIONED__` uses the schema of `events` when it is available.

## Find work already done elsewhere

A table profile describes its sources, attributes, and grain. Grain means what one row represents: one sale, one customer, or one customer per day.

- An **overlap** is a table already providing the same attribute at the same grain.
- A **rollup** is a finer summary that may supply a coarser one. Daily sales might supply monthly sales by summing days.
- Two queries that read `Orders` and `orders` are not duplicates: BigQuery table names are case-sensitive. Column names and aliases are not.
- A **near-duplicate** is similar SQL worth investigating. Similar text alone does not prove interchangeable results.

Profiles and match reports include reasons and unknowns. Check whether the match supports a real replacement before changing consumers.

## Why missing schemas matter

`SELECT *` over an external table needs that table's column list. Without it, KumoSQL cannot reliably trace the columns. Saved catalog data or an explicitly requested schema lookup can fill the gap.

An unresolved Dataform template can also hide a dependency. The report marks analysis gaps; it does not silently treat them as no dependency.

For a refactor already built into two datasets, output-comparison plans progress from row counts to fingerprints to exact row comparisons. Those comparisons describe the data in those builds, while static proofs reason under their stated assumptions. See [cost and change reports](cost-and-change-reports.md) and [whole-pipeline equivalence](evals/pipeline-equivalence.md).
