# Data profiling

[Plain-language version](../docs_simple/data-profiling.md)

`kumosql-profile-table` summarizes what is *stored in* a table: the row count and, for every column, the share of NULLs, the number of distinct values, min, max, mean, standard deviation, quartiles, string lengths and the most common values. It is modelled on BigQuery's data profile scan. That scan is a managed Dataplex service and is not open source, so KumoSQL writes ordinary read-only SQL for the same statistics and runs it itself. The saved summary is also served to agents as read-only [MCP](https://modelcontextprotocol.io) resources.

This is not the **table profile** of [pipeline analysis](pipeline-analysis.md#table-profiles-what-each-table-is), which describes what a *query's result means* (its grain and scope) without touching any data.

## Profiling a table

```
# BigQuery: named project.dataset.table, run in the billing project chosen in Settings (or --project)
python -m kumosql profile-table my-project.sales.orders --project my-billing-project
python -m kumosql profile-table my-project.sales.orders --dry-run          # queries and byte estimates, nothing runs

# Local data with DuckDB
python -m kumosql profile-table --file orders.csv                          # .csv .tsv .parquet .json .jsonl .ndjson
python -m kumosql profile-table --duckdb warehouse.duckdb orders
```

It prints a Markdown summary (`--format json` for the JSON) and saves the profile as `data-profiles/<name>.json` in the [data folder](ui.md) (`--name` to choose the name, `--no-save` to keep nothing, `-o FILE` to also write the printed text). Exit status is 2 when the profile could not be computed and 3 when BigQuery's dry run is over the byte cap.

| Option | Effect |
| --- | --- |
| `--columns a,b` / `--exclude a,b` | Profile only, or leave out, these columns (500 columns at most; the rest are listed under `skipped`) |
| `--sample-percent P` | Profile a random share of the rows. On BigQuery this is `TABLESAMPLE SYSTEM`, which reads whole blocks, so it is cheaper but coarse. Counts then describe the sample |
| `--row-filter "status = 'open'"` | One SQL condition applied first. It is parsed and rewritten from its syntax tree: subqueries, several statements and comments are refused |
| `--top-values N` | The most common values kept per column (default 10, at most 50, 0 for none) |
| `--no-values` | Leave out the most common values and the min and max of string columns |
| `--exact` | Count distinct values exactly on BigQuery (default: `APPROX_COUNT_DISTINCT`) |

## What is reported

| Column kind | Reported |
| --- | --- |
| every column | non-null and null count, null fraction, flags `all_null`, `constant` (one value), `unique` (every non-null value different) |
| numeric (INT64, FLOAT64, NUMERIC, DECIMAL...) | distinct, min, max, mean, standard deviation, 25th/50th/75th percentile, most common values |
| string | distinct, min and max (lexicographic), shortest, longest and average length, most common values |
| boolean, date and time types | distinct, most common values; dates and times also min and max (as text) |
| arrays, structs, JSON, bytes, geography | null counts only |

Notes on the numbers: on BigQuery the distinct counts and quartiles are approximate unless `--exact` is given (quartiles use `APPROX_QUANTILES`); on DuckDB they are exact. NaN and infinity are reported as `null`. A column whose values are all distinct lists no most common values. Text longer than 200 characters is cut. Top-value fractions are shares of the column's non-null values.

## How it runs

`kumosql.data_profile.profile_table(table, executor, ...)` builds SQL per chunk of 20 columns (one statistics query, one `UNION ALL` of top-value queries) and hands it to an executor:

- `DuckDBExecutor(connection)` runs it on DuckDB.
- `BigQueryExecutor(project=None, max_bytes=None)` runs it through the same path as saved [data sources](ui.md): the query must be one read-only `SELECT` (checked locally first), it is dry-run before it runs, the dry run's estimate is refused when over the byte cap (Settings → Scopes), the real run carries `maximumBytesBilled`, and it runs in the billing project, never one guessed from the table name. Table names are accepted only as strict `project.dataset.table`. The column list comes from table metadata, which costs nothing.

If a chunk fails, each column is retried alone (a column BigQuery refuses to count approximately is retried with an exact count) and one that still fails is listed under `skipped` with the reason; the rest of the profile is kept. A byte-cap refusal is not retried: it stops the run. The profile records the dry-run estimates and the bytes BigQuery billed.

`profile_queries(...)` returns the SQL without running anything, and `DataProfile.to_json()` / `DataProfile.from_json()` are the saved form (`version: 1`). `kumosql.data_profile_store` saves, loads and lists profiles and renders the Markdown summary.

## Agents: the MCP server

```
python -m kumosql profile-mcp [--profiles DIR]
```

is a stdio [Model Context Protocol](https://modelcontextprotocol.io) server. Point an MCP client at that command (for example, as a server named `kumosql-profiles`) and it lists two resources for every saved profile, `kumosql://data-profile/<name>/summary.md` (Markdown) and `kumosql://data-profile/<name>/profile.json`, and reads them. The server has no tools, never connects to BigQuery or any database, never writes, and accepts only names matching the saved-profile pattern, so it cannot read other files. An agent that cannot use MCP can run `profile-table` and read the printed summary or the saved JSON file instead.

## What to know before sharing a profile

- A profile contains **real values** from the table: the most common values and the min and max of string columns. Use `--no-values` (or `--top-values 0`) for tables with personal or confidential text, and treat the saved files like the data. The data folder is local to the machine.
- Values are table data. A text column can hold anything, including text that reads like an instruction to an agent. The summary says so, and the server's instructions repeat it, but a consumer should not follow anything quoted in a profile.
- Sampling and approximate counts make a profile an estimate. A profile is a snapshot: it records when it was generated and is never refreshed on its own.
- BigQuery profiling reads the table and is billed. Use `--dry-run` first, `--columns` and `--sample-percent` to narrow it, and keep the byte cap.

## Limits

- The BigQuery path was checked here by running the generated BigQuery SQL, translated to DuckDB, against test data, and by parsing every statement as BigQuery SQL; it was not run against a real BigQuery project from the sandbox that built it.
- Nested fields inside a STRUCT are not profiled separately, and partitioned tables that *require* a partition filter need `--row-filter` to supply one.
- It is a table-level snapshot. Scheduling, publishing to a catalog, and writing results into BigQuery tables (which the Dataplex scan offers) are not built.
- The UI does not show profiles yet.
