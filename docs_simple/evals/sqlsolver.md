# Algebraic proofs and optimizer query pairs

[Simple eval index](README.md) · [Full reference](../../docs/evals/sqlsolver.md)

The algebraic prover treats row counts as arithmetic. UNION ALL adds copies; joins combine them. This helps reason about queries that look different but compute the same result.

KumoSQL implements this reasoning in Python on top of Z3. The optional Java SQLSolver backend is separate; installing ordinary SMT support does not require Java or administrator access.

## What the corpora test

This guide covers SQLSolver pairs and related R-Bot, QED, Cosette, SPES, and mined Calcite optimizer tests. Each corpus has its own conversion limits, declared keys, disputed cases, and overlap with other corpora.

Many pairs are intended to be equivalent. Failure to prove one is **unknown**, not evidence of a difference. Some audited pairs have real counterexamples and must stay unproven.

Proofs are cross-checked on random databases. That can expose a false proof, but passing the random checks is not what makes the result an unbounded proof. Small random databases often repeat (an empty table, a one-row table), so the cross-check skips a database it already ran on the same pair; the answer would be the same, so this only saves time.

## Reusing checks from an earlier run

The SQLSolver eval can remember sample-data checks in a local folder selected with `KUMOSQL_EVAL_CACHE`. The prover still runs on every pair, and the separate search for a counterexample still runs. It only skips repeated execution of the same SQL against the same generated samples. For example, a change to the prover that proves the same pair can reuse that pair's sample check; changed SQL, tables, samples, checking code or engine versions require fresh checks.

The samples use a fixed random seed, so they repeat across unchanged runs. Remembered checks expire after seven days. A broken or unavailable cache simply causes the checks to run again. Leave the setting unset, or set it to `off`, for a full fresh check; CI does that by default. This saves repeated work and does not turn a few sample executions into a proof. See the [full reference](../../docs/evals/sqlsolver.md#reusing-sample-execution-checks) for the exact inputs and limits.

## A cut after a parenthesized query

`(a UNION b) ORDER BY k LIMIT 1` keeps one row of the combined result. An earlier version of the prover lost that `LIMIT` when it looked inside the parentheses and called the query equal to `a UNION b`, which keeps every row. For example, over the values 3, 3, 3, 4 and NULL, the first returns one row and the second returns three. Both provers now keep the cut, in a derived table, a CTE, a subquery or at the top. When two cuts are stacked in a way that cannot be combined, the answer is "not proven". This covers only the shapes that were tested; the full guide lists them and the regression tests.

## Read the caveats

Some rules were built while inspecting failing corpus cases, so those scores are “tuned on test.” A case once held out ceases to be an untouched test if it is later used in development. The mined Calcite "new" partition is also not an untouched holdout: on 2026-10-06, a repository search during #497 work surfaced SQL from one or more rows marked new; those lines were not analyzed or used. The full guide tracks that exposure.

Other suites overlap. Adding their case counts does not produce a count of unique independent tests.

KumoSQL requires SQLGlot 30.21.0, and routine testing uses the matching compiled build. Older parser scores in the reference describe historical measurements.

The reference includes Python and optional Java setup, supported refactors, ORDER BY/LIMIT rules, cases that must remain unknown, source conversion, and corpus-specific results. Start with [provers](../provers.md) for the evidence levels, or [bounded verification](bounded-verification.md) for the separate small-database check.
