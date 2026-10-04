# Numeric assumption report, explained simply

[Index](README.md) · [Full reference](../docs/numeric-assumption-report.md)

When KumoSQL proves two queries equal, it lists what it had to assume. For numbers the list used to say things like "no `NaN` ever appears", "a division by zero or an overflow never happens" and "adding and multiplying is exact". Issue #484 taught the prover to read numbers the way BigQuery does, so that many proofs can drop some of those assumptions. This report counts how many did.

## What it does

It takes the query pairs of the prover evals (the same pairs the scoreboard uses), proves them with the prover as it was before that work, then with the prover as it is now, and compares the assumptions each proof lists. It prints, for each assumption, how many proofs carried it before and after, and how many proofs that touch numbers now carry fewer.

An example from the numeric traps eval: `SELECT IF(y = 0, 0, x / y) ...` against `SELECT x / y ...` used to be proven "assuming errors never happen". Now the prover sees that the second query can divide by zero on a row the first one guards, and it refuses the proof. A rewrite that only reorders an addition now carries "both queries can fail in the same way" instead of "errors are not modeled".

"Touches numbers" means the proof listed a number-related assumption, or its queries contain arithmetic or a number. Because every proof used to list the `NaN`, error and sum-order assumptions, that covers every proof, so the report also shows two stricter counts.

```sh
python tools/numeric_assumption_report.py                   # the prover evals (long: about 80 minutes)
python tools/numeric_assumption_report.py --bigquery --evals qed rbot   # the same pairs read as BigQuery
python tools/numeric_assumption_report.py --evals numeric-traps         # the traps eval, quick
```

## What it found

- **On the evals as they run: none.** Of 2,220 proofs, 0 carry fewer assumptions (0%), against the goal of 80%. The reason is simple: those evals are written in MySQL or PostgreSQL SQL, and the prover only applies BigQuery's number rules to BigQuery SQL. The new work never touched these proofs. No proof was lost or gained.
- **If the same pairs are read as BigQuery SQL: most.** Of 737 proofs, 653 (88.6%) carry fewer assumptions. This is an experiment: MySQL SQL read as BigQuery, and three slow evals left out. Six proofs were lost, all because a rewrite now looks able to overflow on a row the original filtered out. They are not wrong by rows, but they are not "false proofs" either, so the report lists them.
- **On the numbers eval written with the change: 79.2%** (19 of 24), and the four lost proofs there were false.

## What to keep in mind

- The pairs and numbers are a snapshot of one day. Other prover changes will move them; rerun the command to see the sum.
- The result depends on how "touches numbers" is defined; the report states it and gives the stricter variants.
- The test of "was a lost proof false" is the eval's own search with random databases, so "no counterexample" does not mean the proof was right.
- The numeric traps pairs were written by the person who changed the prover, so that eval is a development check, not an independent one.

See the full reference for every count, the per-eval tables and the six lost proofs.
