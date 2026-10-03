# Simplifying a set of table definitions

[All simple guides](README.md) · [Full reference](../docs/table-minimization.md)

Give KumoSQL several table-defining queries and choose protected outputs. It searches for a simpler set while keeping those protected names, column order, and results.

Unprotected tables may be dropped, folded into readers, merged with equal tables, pruned of unused columns, or simplified. With factoring on, a query repeated in several tables can move into one new table. A change is kept only when the pipeline prover accepts the protected outputs against the original definitions.

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

## What “simpler” means

The score counts SQL structures using KumoSQL's measure over SQLFluff's parse tree, plus a term for the number of tables. It is not a runtime or cost measurement.

The search is greedy and time-limited, so it need not find the global optimum. Queries with unstable values or unsupported statements receive special protection. Unknown moves are rejected.

The full guide lists score details, supported moves, source-schema formats, nondeterminism restrictions, and prover limitations, including known soundness issues. The [evaluation guide](evals/table-minimization.md) describes independent execution checks on the returned candidates.
