# QED's CockroachDB cases in plain language

[Simple eval index](README.md) · [Full reference and recorded scores](../../docs/evals/qed-cockroach.md)

## What this tests

Another research prover, QED, ships 1,287 pairs of query plans taken from CockroachDB's optimizer tests. In each pair the database rewrote a plan into a plan it believes gives the same rows. This eval asks KumoSQL to prove each pair equal, and checks the answers against QED's own results on the same pairs.

## An example

A rewrite turns `WHERE b <> TRUE` into `WHERE NOT b`. KumoSQL's prover currently cannot prove that one, because it treats a true/false column like a number, so it answers "unknown". QED proves it. The eval records that as a case QED wins.

## How a case is scored

1. A converter turns the pair into two ordinary SQL queries. If it cannot do that exactly (a function whose name QED's data does not keep, a `LIMIT` with no `ORDER BY`, an exotic column type) it skips the pair and says why. It never guesses.
2. The prover answers **proved**, **unknown** or, when a random database shows the two queries return different rows, **different**.
3. Every "proved" is re-run on 60 random databases in DuckDB. A proof that fails there would be counted as **wrong**. The number of wrong proofs must be 0.

## What the numbers mean

See the [full reference](../../docs/evals/qed-cockroach.md) for the recorded scores. In short: KumoSQL proves a little over half of all 1,287 cases and QED proves about three quarters, so QED is ahead on this suite. A few hundred pairs are missed for reasons that fall into groups: whole-number reasoning, true/false columns, a database technique called an apply join, and outer joins that can be simplified. Those are written up in the reference, with what each would need.

Part of QED's lead cannot be checked at all. Where the data does not name a function, QED treats any two calls with the same arguments as equal, and where a `LIMIT` has no `ORDER BY` it treats the cut as a fixed function of the input. DuckDB cannot confirm those proofs, and KumoSQL does not claim them.

## Limits of the evidence

- A pair that a counterexample separates is taken out of the denominator. Five are, and they are real differences in QED's own data (for example a unique index the data left out), so QED does not prove them either.
- No prover rule was written for these pairs, so nothing is tuned on them yet. Once someone targets the unknown pairs, the results file must say so.
- A proof here is a proof by KumoSQL's prover plus agreement on random databases. It is not a proof about CockroachDB itself, which has its own types and error behaviour.
- The pairs come from CockroachDB's tests, whose own licence terms continue to apply. The repository stores only converted SQL; the fixture README says exactly what was and was not checked.
