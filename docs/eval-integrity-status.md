# Eval integrity audit status

An external auditor reviewed commit `b7d7dc6` (2026-09-29) and sent the [eval integrity audit](eval-integrity-audit-2026-10-02.md) on 2026-10-02. Master had moved about 400 pull requests since, so every finding was re-run on master (`a481075`, 2026-10-03) before anything was changed. This page records what each finding meant on master and what was done about it.

| Finding | On master before the fixes | Done |
| --- | --- | --- |
| Lifted CTE shadows a table, and the verifier approves it | Still a false proof. `SELECT a.x, b.x FROM (SELECT 1 AS x) a JOIN __lifted_subquery_001 b ON TRUE` was lifted into a CTE named `__lifted_subquery_001`, and `prove_equivalent` proved the two equal (with a table row `x = 7` they return `(1, 7)` and `(1, 1)`). The same capture existed in the prover's CTE canonicalization: a CTE renamed `__canonical_cte_001` matched a real table of that name, so a CTE self-join was proved equal to a join with the table | The lifter skips every table and CTE name in the statement, case-insensitively, and the structural prover declines a query that reads a real table named like a CTE it generates. Both witnesses are regression tests (`tests/test_lift_subqueries.py`, `tests/test_equivalence.py`) |
| SMT proof ignores `FOR SYSTEM_TIME AS OF` | Already fixed: `prove_equivalent_smt` answers `not_proven` ("Table.version is not modeled") | The audit's exact pair is a regression test in `tests/test_smt_equivalence.py` |
| Execution comparison merges different values | Still real: `TRUE` equalled `1`, NaN equalled the array `['NaN']`, a struct equalled an array of pairs, and floats always compared at 12 significant digits | STATUS_RESULT |
| Static proofs checked by execution with no coverage floor | Still real: `test_static_proofs_agree_with_execution` passed with zero checked pairs | STATUS_COVERAGE |
| Synthetic data never hits a query's constants (`WHERE a = 1000` vs `WHERE FALSE`) | Already fixed: the generator draws half its values from the queries' own constants, and the pair is reported different | None needed |
| Workbook fixture gate accepts invalid inputs, unchanged outputs and parse recovery; overrides can empty the denominator; CI never runs it | Still real | STATUS_FIXTURE |
| Benchmark population changed, sanitizer reports no discarded rows | History is as described; the current fixture is the 32 public hand-written samples | STATUS_SANITIZER |
| SMT fuzz floor rewards identity pairs; one RNG for pairs and data | Still real: 102 of 132 proofs were of identical strings | The 250 pairs are generated before any proof and pinned by a sha256; validation databases come from a separate RNG per pair. Identity pairs (108) must all be proven, and the floors count changed-text pairs only: at least 28 proofs (32 measured), at least 7 outside the trivial `WHERE TRUE AND` rewrite (9), at least 70 refutations (79). See [Equivalence provers](provers.md) |
| Ambient Google credentials break the dry-run test | Already fixed: the test blocks Application Default Credentials | None needed |
| Results don't record versions | Results files had no versions | `write_results` in `tools/bench_common.py` adds an `environment` key (library versions and the git commit) to every results file it writes; see the [results format](../benchmarks/README.md) |
| No protected held-out corpus | Out of date: most proof evals now keep a held-out split, reported in each results file's `held_out` key | None needed |

No eval score moved. STATUS_EVALDIFF

The audit's companion evidence JSON and reproduction script describe the older commit and were not committed.
