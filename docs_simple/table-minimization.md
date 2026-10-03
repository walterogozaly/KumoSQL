# Simplifying a set of table definitions

[All simple guides](README.md) · [Full reference](../docs/table-minimization.md)

Give KumoSQL several table-defining queries and choose protected outputs. It searches for a simpler set while keeping those protected names, column order, and results.

Unprotected tables may be dropped, folded into readers, merged with equal tables, pruned of unused columns, or simplified. A change is kept only when the pipeline prover accepts the protected outputs against the original definitions.

## A small input file

Save this as `case.json`:

```json
{
  "tables": {
    "stage": "SELECT id FROM orders",
    "report": "SELECT id FROM stage"
  },
  "protected": ["report"],
  "sources": {"orders": {"id": "INT64"}}
}
```

Then run:

```sh
python -m kumosql minimize-tables case.json
```

The JSON result includes proposed definitions, proofs, moves, rejected moves, and the stopping reason. Read assumptions alongside the proofs. The Python API also provides `minimize_tables` and `verify_tables`.

## Names, scripts and nested `WITH` tables

The minimizer proves things about *your* tables, so it is careful about what a name means:

- A table made of several statements (a script) is kept exactly as written, with every table it mentions. Example: a table `out` that runs `SELECT 0; SELECT x FROM tail` keeps `tail`, because the second statement reads it.
- `p.D.stage` and `p.d.stage` are different tables (BigQuery's default). If two names differ only by case, those tables, and the tables that read them, are left alone, because a dataset set to case-insensitive names would make them one table.
- Names it makes up internally can never be confused with a table you named yourself.
- A `WITH stage AS (...)` inside one subquery does not hide a real table `stage` read somewhere else in the statement.

Before it returns an answer it checks that nothing in it reads a table the answer removed. The evidence is the regression tests in `tests/test_table_minimizer.py` (each case also runs in DuckDB where that is possible); it does not cover datasets configured for case-insensitive names or incremental Dataform tables.

## What “simpler” means

The score counts SQL structures using KumoSQL's measure over SQLFluff's parse tree, plus a term for the number of tables. It is not a runtime or cost measurement.

The search is greedy and time-limited, so it need not find the global optimum. Queries with unstable values or unsupported statements receive special protection. Unknown moves are rejected.

The full guide lists score details, supported moves, source-schema formats, nondeterminism restrictions, and prover limitations, including known soundness issues. The [evaluation guide](evals/table-minimization.md) describes independent execution checks on the returned candidates.
