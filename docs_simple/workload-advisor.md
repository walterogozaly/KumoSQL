# Which models should be stored?

[All simple guides](README.md) · [Full reference](../docs/workload-advisor.md)

In a Dataform project, each model is either a **view** (BigQuery recomputes it every time someone reads it) or a **table** (BigQuery computes it once per refresh and stores the result). Views cost nothing to keep but make every reader pay again. Tables make readers cheap but cost a refresh and storage. The best choice depends on how often each model is read, which is in your job history.

`python -m kumosql advise` looks at that history and lists the changes most worth making. It only recommends a change when the evidence says every reader would get the same rows as before. Anything that would need a condition to hold goes in a separate "needs proof" list, and is never added to the savings.

## A small example

Say a view `daily` adds up a large table, and dashboards read it 40 times a day. The table underneath is rebuilt every morning in the same run. Storing `daily` as a table, refreshed right after, means the dashboards stop re-adding the large table. KumoSQL would list that under "Recommended" with an estimate of the bytes saved per day and the reason it is safe.

Two other views in the same project would be listed differently:

- One reads a table that is loaded from outside the project. A stored copy could be out of date between loads, so it goes under "Needs proof", with the condition you would have to confirm. Its estimated saving is shown but not counted.
- One uses `CURRENT_DATE()`. A table would freeze the date at refresh time, so the answer could change. It is listed as "Would change results" and never recommended.

## Try it

First write two files from your own BigQuery warehouse's metadata. This only reads metadata; it never writes to the warehouse.

```sh
python -m kumosql export-warehouse --project my-billing-project --region us --jobs-out jobs.json --sizes-out sizes.json --dry-run
```

`--dry-run` prints the SQL it would run and sends nothing. Leave it off to run the export for real. Each read is estimated first and refused if it would scan more than a cap (1 GiB by default). The jobs file contains query text and user emails, so keep it private.

Then ask for advice:

```sh
python -m kumosql advise --project path/to/dataform --jobs jobs.json --sizes sizes.json
```

Add `--usd-per-tib 6.25` (use your own price) to see dollars instead of bytes. Add `--schedules schedules.json` if you know which schedule refreshes each model; it can turn "needs proof" into "proven".

## How far to trust it

- The savings are **estimates** scaled from jobs that really ran. They are not measurements of what you would save. After you make a change, compare real before-and-after cost to find out.
- How close the estimates come to real runtimes is being tested separately, and no result is claimed yet.
- The tests use a pretend BigQuery. The export command has not yet been tried on a real warehouse.
- "Proven" means the history and project show readers cannot tell the difference. It does not mean KumoSQL compared two queries' results.

For the exact options, the pricing rules and the safeguards of the export, see the [full reference](../docs/workload-advisor.md).
