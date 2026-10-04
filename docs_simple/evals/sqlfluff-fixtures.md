# Checking SQLFluff's expected fixes

[Simple eval index](README.md) · [Full reference](../../docs/evals/sqlfluff-fixtures.md)

SQLFluff has test fixtures showing SQL before and after a lint fix. This evaluation checks those pairs and tests KumoSQL's formatter against them.

## Three different questions

- **Semantic fixes:** do the two queries mean the same thing under the inferred schema?
- **Layout fixes:** did spacing or casing change without changing meaningful tokens?
- **KumoSQL formatting:** how does KumoSQL behave on the fixture inputs?

Some lint fixes intentionally change meaning. An expected SQLFluff fix is not automatically an equivalence label.

## Run a part

From a development checkout:

```sh
python tools/sqlfluff_fixtures_bench.py semantic
python tools/sqlfluff_fixtures_bench.py layout
python tools/sqlfluff_fixtures_bench.py kumosql
```

The full guide lists source/version setup and recorded scores.

Execution checks run in a separate process on Windows as well as Unix. If that process crashes or
runs out of time, the pair stays unchecked. A large returned example is read before the parent
waits for the child to finish, so its size alone does not look like a timeout.

Original fixtures are scored separately from adapted statements. A script or template may need adaptation before its query can be checked, which does not establish the original file was fully supported.

Inferred schemas can include placeholder columns to make `SELECT *` well defined. A proof about that inferred schema is not a proof about every possible table width.

The reference describes deliberate semantic changes, unsupported features, licensing, and what each score counts. See [rewrite rules](../rewrite-rules.md) for normal formatting behavior.
