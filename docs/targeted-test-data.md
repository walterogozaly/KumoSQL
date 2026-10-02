# Targeted test data, multi-database checking and counterexample minimization

Three evals strengthen and measure the synthetic checker (the DuckDB execution engine in `kumosql.result_equivalence`). They share that one engine; nothing here is a second implementation.

| Piece | Module | Role |
| --- | --- | --- |
| Declared-rule generation, `DatasetRunner` | `result_equivalence.py` | `generate_synthetic_dataset(..., rules=)` respects NOT NULL columns and keys; `DatasetRunner` keeps one DuckDB connection, swaps rows and times out runaway queries |
| Targeted databases and the multi-database suite | `targeted_data.py` | Rows below, at and above each constant a query compares with, shared join and group pools, each table empty in turn, all NULL, doubled rows, one value everywhere |
| Faulty variants | `query_mutants.py` | One-site mutation operators (comparison and boundary, constant, AND/OR, dropped or negated predicate, join type, DISTINCT, aggregate, arithmetic, grouping, COALESCE, BETWEEN, LIMIT) |
| Minimization | `minimize.py` | Shrinks a failing pair, its schema and its database while the pair still differs; replayable JSON |
| Harness | `tools/targeted_data_bench.py` | Scores all of it |

Method after XData (Chandra et al., VLDB J. 2015, mutation-killing data generation) and SQLess (failure-preserving reduction). No code or data of either is used; the operators and generators are written for sqlglot trees. SQLess reduction preserves a *failure*; it is not an equivalence-preserving simplification, and the minimizer says so.

## What is scored

For each original query the harness builds mutants and records which strategy tells each one from the original on a database that respects declared constraints:

* `single_seed`: one random database (seed 1);
* `random_8`: the engine's default eight random databases (the baseline: the checker as it was);
* `targeted`: databases built around the query;
* `suite`: corner cases plus targeted plus four random databases.

A mutant that no strategy kills (and that 150 further random databases do not kill) is passed to the equivalence prover. Proven-equivalent mutants are not faults and leave the denominator; mutants that are neither killed nor proved stay in it as *survived, unclassified*, so the score never rewards an unproven guess. Mutants that are not valid queries (`error`) or run past the time limit (`timeout`) are discarded and counted. A kill must repeat on both sides (no nondeterminism) before it counts.

* **Originals** (not adapted): the SQLSolver query corpora in `tests/fixtures/sqlsolver` (Apache-2.0; Calcite, Spark SQL, TPC-H, TPC-C), plus a 16-query university-schema set written for this eval (`UNIVERSITY` in the tool). **Adapted cases** are the mutants. Calcite and Spark queries and the university set are the *development* split; TPC-H and TPC-C are *held out* and run once at the end.
* Overlap: the same query texts are used by the SQLSolver Calcite/Spark/TPC-H/TPC-C evals (equivalence pairs, a different task) and R-Bot (Calcite). Mutation scoring shares no labels with them.
* Declared NOT NULL and keys are respected; foreign keys are not modelled, so a kill that needs a dangling foreign key would count. Float results compare to 12 digits.

Run: `python tools/targeted_data_bench.py [--split dev|heldout|all]`; detail goes to `benchmarks/targeted_data/` (ignored by git except the summaries).

## Results

See the README scoreboard (rows *Targeted test data*, *Multi-database semantic suite*, *Counterexample minimization*) and `benchmarks/targeted_data/summary-*.json` for per-operator and per-suite tables.

__RESULTS__

## Regression cases

Mutants that escape the single seed and the default eight databases but are caught by the suite, and every mutant the suite does not catch, are kept in `tests/fixtures/targeted_data/cases.json`; `tests/test_targeted_data_bench.py` replays them and fails if the suite stops catching one it caught before, and pins floors for the full corpus.
