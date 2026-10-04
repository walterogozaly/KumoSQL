# Checking the "equivalent under conditions" answer

[Simple eval index](README.md) · [Full reference](../../docs/evals/conditional-equivalence.md)

The [conditional answer](../conditional-equivalence.md) says two queries match whenever a short list of facts is true. These evals ask how often KumoSQL finds such a list on public benchmarks, and whether any answer is wrong.

## How it is scored

The benchmarks are the Singh and Bedathur LeetCode pairs and VeriEQL's LeetCode set. The Singh files list no keys and no NOT NULL facts, so many pairs that look equal are only equal when a fact holds. VeriEQL keeps each problem's declared facts, so there a conditional answer means a fact beyond those.

Four counts are kept apart: pairs proved with no conditions, pairs proved under named conditions, pairs where a database that meets every candidate fact still separates the queries, and pairs left unknown. Nothing reads the published label while deciding.

## How each answer is checked

Every conditional answer is re-run on many random databases built to meet its conditions. The queries must match on all of them, and the proof must disappear when any one condition is removed. A database that breaks the conditions and separates the queries is shown as supporting evidence only; agreement on sampled databases is never treated as the truth.

## Pairs that only touch a constraint

A list of 655 benchmark pairs was mined for queries that read a declared key, NOT NULL column or foreign key. The check strips every declared constraint from 508 of them and asks which facts a proof then needs. Most (386) need none, so reading a key is not the same as depending on it. 49 need facts: 35 only what the schema already declared, 14 a fact the schema never stated (for example that a department number is unique). For 11 of the 49 the queries may not need the facts at all; the prover does. No answer was wrong. Five more pairs are proved when the schema's constraints are given but not by the conditional search. The full guide has the counts and the command.

## Hand-checked cases

Small suites with answers worked out by hand cover duplicates, NULLs, composite keys and contradictory conditions. A condition set that no database can satisfy is refused, and pairs that need facts outside the supported list must stay unproven.

## Limits

The VeriEQL run is a sample, not the whole set, and no held-out split was reserved for it. Conditions are minimal for this prover only. The full guide has the counts, the held-out result and the commands to rerun them.
