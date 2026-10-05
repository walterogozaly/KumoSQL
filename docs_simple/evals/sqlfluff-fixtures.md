# Checking SQLFluff's expected fixes

[Simple eval index](README.md) · [Full reference](../../docs/evals/sqlfluff-fixtures.md)

SQLFluff has test fixtures showing SQL before and after a lint fix. This evaluation checks those pairs and tests KumoSQL's formatter against them.

## Four different questions

- **Semantic fixes:** do the two queries mean the same thing under the inferred schema?
- **Layout fixes:** did spacing or casing change without changing meaningful tokens?
- **KumoSQL formatting:** how does KumoSQL behave on the fixture inputs?
- **Refusal cases:** SQLFluff has queries it deliberately leaves alone. Do KumoSQL's own structural rules leave them alone too, or change them only when the change is proven and keeps the meaning? For example, a subquery that reads a column of the query around it must not be lifted out into a CTE.

The refusal cases found rule bugs that were fixed with the eval. A score here is limited by the 212 cases SQLFluff publishes, and two lifted forms that the structural prover used to accept wrongly are now refused and kept as regression tests; the full guide lists them.

Some lint fixes intentionally change meaning. An expected SQLFluff fix is not automatically an equivalence label.

## Run a part

From a development checkout:

```sh
python tools/sqlfluff_fixtures_bench.py semantic
python tools/sqlfluff_fixtures_bench.py layout
python tools/sqlfluff_fixtures_bench.py kumosql
python tools/sqlfluff_fixtures_bench.py refusals
```

The full guide lists source/version setup and recorded scores.

Original fixtures are scored separately from adapted statements. A script or template may need adaptation before its query can be checked, which does not establish the original file was fully supported.

Inferred schemas can include placeholder columns to make `SELECT *` well defined. A proof about that inferred schema is not a proof about every possible table width.

The reference describes deliberate semantic changes, unsupported features, licensing, and what each score counts. See [rewrite rules](../rewrite-rules.md) for normal formatting behavior.
