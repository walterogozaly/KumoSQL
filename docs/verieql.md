# VeriEQL benchmarks (no LLM at evaluation time)

`tools/verieql_bench.py` scores KumoSQL on the three suites of [VeriEQL](https://github.com/VeriEQL/VeriEQL) ("VeriEQL: Bounded Equivalence Verification for Complex SQL Queries with Integrity Constraints", OOPSLA 2024): **LeetCode** (about 24,000 pairs of real solutions to LeetCode SQL problems), **Literature** (64 hard pairs from earlier equivalence research) and **Calcite-397** (optimizer rewrites with schemas and integrity constraints). Each case is two queries plus a schema with constraints (primary and foreign keys, NOT NULL, `CHECK`-style predicates, consecutive ids, cross-table implications). Unlike SQLSolver's suites, the pairs include inequivalent ones, and the files carry no labels.

Everything is deterministic Python: `kumosql.counterexample` (a constraint-respecting database generator that runs both queries on DuckDB) and the algebraic z3 prover (`kumosql.algebraic_equivalence`, see [sqlsolver.md](sqlsolver.md)). No model is called and no gold label is read.

## Verdicts and evidence levels

| Verdict | Evidence | Meaning |
| --- | --- | --- |
| `equivalent` | **unbounded proof** | The z3 prover proved the result bags equal on every database that satisfies the declared keys and NOT NULL columns. After a proof, 1,000 more random databases (a different seed) must still agree, or the case counts as `wrong`. |
| `different` | **executed counterexample** | A database satisfying every constraint was built and both queries were run on it with different result bags. The difference has to survive three row-order shuffles, so it never rests on `LIMIT` ties or on MySQL's arbitrary pick for a non-grouped column, and it has to show with DuckDB's optimizer switched off (`PRAGMA disable_optimizer`), because the optimizer has returned wrong rows for some correlated subqueries. Re-checking the full LeetCode run this way dropped 3 of 5,519 refutations; Calcite and Literature did not change. |
| `agrees` | **executed datasets only** | Not proven, and 600 random databases (200, then 400 more for pairs that are not proven) showed no difference. This is not a proof and not bounded verification. |
| `unknown` | none | The pair could not be run (a query DuckDB rejects, unreadable constraints). |
| `wrong` | | An `equivalent` verdict contradicted by a counterexample (our own second search, or VeriEQL's published counterexample replayed on DuckDB). Must stay 0. |

VeriEQL itself is *bounded* model checking: "verified" there means no counterexample exists up to a bound on table size, so a VeriEQL pass is weaker than an unbounded proof. The scoreboard keeps the levels apart: the **proof** rows count `equivalent`, the **executed** rows count `different` plus `agrees`. KumoSQL does not implement bounded verification yet, so there are no bounded rows.

## Counterexample generator

`kumosql.counterexample.find_counterexample(spec, left, right)` returns a `Counterexample` (the rows of every table plus both result bags, and `.script(spec)` for a runnable `CREATE`/`INSERT` script). Databases are small (up to 5 rows per table); values come from small domains seeded with the literals of the two queries (the constant, one below, one above, string and date literals), a "hot subset" per column makes ties and join matches common, and NULLs appear on nullable columns. Constraints are respected by construction (keys, foreign keys by drawing from parent rows, consecutive ids) or by rejection (`CHECK`-style predicates and implications; a NULL never satisfies one, which is the strict reading and therefore valid under both).

MySQL lets a grouped query read ungrouped columns; DuckDB refuses. Such columns are wrapped in `ANY_VALUE`, and any difference found must be stable under shuffling the input rows, so only functionally determined columns can produce a counterexample.

## Running

```
python tools/verieql_bench.py literature
python tools/verieql_bench.py calcite --jobs 4
python tools/verieql_bench.py leetcode --every 24 --jobs 4 --audit   # a 1,000-case stratified sample
python tools/verieql_bench.py leetcode --jobs 4 --audit              # all cases (about 2.5 hours on 4 cores)
```

`--audit` also compares with VeriEQL's published per-case outcomes (`NEQ` with a counterexample, `TMO` timeout and so on) and replays VeriEQL's counterexample against every pair we called equivalent. The suites are downloaded once into `~/.cache/kumosql/verieql` (set `KUMOSQL_VERIEQL_CACHE` to change it). The data is CC BY-NC-SA 4.0, so it is not copied into this repository, and none of VeriEQL's code is vendored (its licence is also CC BY-NC-SA 4.0); only its benchmark files are read. Please cite the VeriEQL paper when using these numbers.

`tests/test_verieql_benchmarks.py` pins floors on small samples (skipped, with a "data unavailable" message, when the data cannot be downloaded; downloads are retried, checked against pinned SHA-256 sums and written atomically).

## Results (2026-10-02)

| Suite | Pairs | Proven equivalent | Refuted (executed) | Agree on random databases | Not run | Wrong |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Literature | 64 | 10 | 22 | 21 | 11 | 0 |
| Calcite-397 | 397 | 200 | 15 | 86 | 96 | 0 |
| LeetCode (all pairs) | 23,994 | 4,380 | 5,516 | 12,175 | 1,923 | 0 |

On LeetCode, 3,233 of the 3,586 pairs VeriEQL itself refutes are refuted here (90%); we also refute about 2,283 pairs VeriEQL timed out on or could not read. The full LeetCode run takes about 4.5 hours on 4 cores. Search settings were tuned on every 24th pair; every other pair is untouched by that tuning. "Not run" means a query that DuckDB or the parser rejects (bare words used as strings, ambiguous columns, Calcite-only syntax).

The ORDER BY, LIMIT and OFFSET rules ([sqlsolver.md](sqlsolver.md#order-by-limit-and-offset)) added 3 Calcite-397 proofs (`ORDER BY 2 OFFSET 1` against `ORDER BY DEPTNO OFFSET 1` over a duplicated column, `ORDER BY CAST(DEPTNO AS DOUBLE)`, and a top-k pushed into `UNION ALL` branches).
