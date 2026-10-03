# Algebraic proofs and optimizer query pairs

[Simple eval index](README.md) · [Full reference](../../docs/evals/sqlsolver.md)

The algebraic prover treats row counts as arithmetic. UNION ALL adds copies; joins combine them. This helps reason about queries that look different but compute the same result.

KumoSQL implements this reasoning in Python on top of Z3. The optional Java SQLSolver backend is separate; installing ordinary SMT support does not require Java or administrator access.

## What the corpora test

This guide covers SQLSolver pairs and related R-Bot, QED, Cosette, SPES, and mined Calcite optimizer tests. Each corpus has its own conversion limits, declared keys, disputed cases, and overlap with other corpora.

Many pairs are intended to be equivalent. Failure to prove one is **unknown**, not evidence of a difference. Some audited pairs have real counterexamples and must stay unproven.

Proofs are cross-checked on random databases. That can expose a false proof, but passing the random checks is not what makes the result an unbounded proof. Small random databases often repeat (an empty table, a one-row table), so the cross-check skips a database it already ran on the same pair; the answer would be the same, so this only saves time.

## Read the caveats

Some rules were built while inspecting failing corpus cases, so those scores are “tuned on test.” A case once held out ceases to be an untouched test if it is later used in development. The full guide tracks that exposure.

Other suites overlap. Adding their case counts does not produce a count of unique independent tests.

The reference includes Python and optional Java setup, supported refactors, ORDER BY/LIMIT rules, cases that must remain unknown, source conversion, and corpus-specific results. Start with [provers](../provers.md) for the evidence levels, or [bounded verification](bounded-verification.md) for the separate small-database check.
