# Keeping Dataform files intact

[Simple eval index](README.md) · [Full reference](../../docs/evals/dataform-bench.md)

SQLX mixes SQL with configuration, JavaScript, and `${...}` expressions. A SQL rewrite must preserve the surrounding Dataform code and still find its dependencies.

The suite generates files with tricky blocks, comments, quotes, nested templates, operation hooks, and references. It knows which pieces must be protected and which tables each reference should reach.

## What is measured?

- Protected spans must survive byte for byte and with the same number of copies.
- Dependencies must reach the correct declared table, including tables outside the default schema.
- Unresolvable templates must be flagged as unknown.
- SQL that can be cleaned up should still be rewritten around the protected text.

For example, a `${ref(...)}` in pre-operations must count as a dependency. An unresolved `${tbl}` must not become an invented external table with an internal placeholder name.

## Run it

From a development checkout:

```sh
python tools/dataform_bench.py
```

The full guide lists scale options and recorded results.

Generated files cover the shapes the authors built; real projects can have others. The dependency expectations follow documented Dataform behavior rather than executing the Dataform compiler. See [Dataform repositories](../dataform-repositories.md) for everyday project loading.
