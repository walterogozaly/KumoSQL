# Testing BigQuery and Dataform syntax

[Simple eval index](README.md) · [Full reference](../../docs/evals/bigquery-syntax-coverage.md)

This suite has small examples of BigQuery and Dataform constructs: queries, scripts, table changes, functions, SQLX configuration, and project layouts. It checks each applicable stage of KumoSQL.

## Three outcomes

| Outcome | Meaning |
| --- | --- |
| Pass | The stage handled the construct correctly |
| Unsupported | The stage explicitly declined it without damaging it |
| Fail | A crash, lost dependency, or changed meaning occurred |

Unsupported cases are recorded in `tests/fixtures/bq_syntax/known_gaps.json`. A newly unsupported case needs an explanation rather than silently disappearing.

## Why this matters

A tool can parse some SQL yet misunderstand a reference inside Dataform JavaScript, change a quoted function name, or lose a dependency in an operation block. Testing several stages catches problems that a parsing-only score misses.

Some procedural and newer BigQuery forms are kept as opaque text. Preserving them with a clear diagnostic is different from fully analyzing them.

The fixture folder holds a manifest, examples, dry-run records, and known gaps. A dry-run failure can also mean an example names an object absent from the test project. The full guide explains which gaps belong to KumoSQL, its parser, or the external environment.

## Newly read constructs

Five BigQuery constructs that real queries use were either rejected or misread, and are now handled inside KumoSQL (never by waiting for the parser library):

- `name LIKE ALL UNNEST(['a%', '%b'])`, where every pattern has to match.
- An aggregate with its own filter, such as `COUNT(* WHERE age > 30)`.
- `WITH(a AS 1, a + 1)`, which names a value and uses it. The `a` inside it is not counted as a column of any table.
- `FROM t, t.tags tag WITH OFFSET pos`, which numbers the items of an array column.
- `STRUCT<>()`, which the parser used to read as a "not equal" comparison. BigQuery rejects it, so KumoSQL now refuses it too.

Where BigQuery itself refuses a form (a filtered aggregate inside a window, or next to `ORDER BY`), KumoSQL declines it as well. The provers say "unknown" for the new forms rather than guess. Each example was first checked with a free BigQuery dry run. The evidence is a handful of small queries, not a broad measurement. The full guide lists the details and the two operator-precedence misreads that another workstream owns.

## A second sweep

About 800 more probe queries were written from the BigQuery reference and checked against a free BigQuery dry run. The ones BigQuery accepts but KumoSQL refused or misread are now fixed inside KumoSQL:

- A table function that is given a table and an array (`EXTERNAL_OBJECT_TRANSFORM(TABLE t, ['SO_UNKNOWN'])`) is read, and the table counts as a read.
- Pipe queries (`FROM t |> ...`). The parser library folds the steps into one `SELECT`, which is wrong when the order matters: `|> LIMIT 1 |> ORDER BY x` was read as "sort everything, take one". `SELECT DISTINCT`, `PIVOT`, `AS STRUCT` and `ROLLUP` were silently dropped. KumoSQL now rewrites the ones that have a plain spelling (`|> WINDOW`, `|> SELECT DISTINCT`, `GROUP AND ORDER BY`) and refuses the rest with a parse error, rather than reading a different query.
- A string such as `'\x41'` (the letter A) was treated as the same text as `'\\x41'` (four characters), and the structural prover called two such queries equal. Escapes are now decoded, and a string with an escape whose value is not certain is not proven.
- `AI.GENERATE_BOOL('a prompt')` was read as a table called `a prompt`. Scalar AI calls are plain functions, and arguments the parser has no slot for (such as a new option of `AI.FORECAST`) are kept instead of dropped.
- A table sampled after a time-travel clause kept its sample on the table.

The evidence is a few hundred small queries and dry runs, not a measurement of real projects. Forms BigQuery accepts that are still refused, and forms KumoSQL reads that BigQuery rejects, are listed in the [full reference](../../docs/evals/bigquery-syntax-coverage.md).

Syntax coverage does not establish equivalence on every dataset. See [BigQuery behavior](bigquery-behavior-eval.md) for execution checks.
