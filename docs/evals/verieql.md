# VeriEQL benchmarks (no LLM at evaluation time)

`tools/verieql_bench.py` scores KumoSQL on the three suites of [VeriEQL](https://github.com/VeriEQL/VeriEQL) ("VeriEQL: Bounded Equivalence Verification for Complex SQL Queries with Integrity Constraints", OOPSLA 2024): **LeetCode** (about 24,000 pairs of real solutions to LeetCode SQL problems), **Literature** (64 hard pairs from earlier equivalence research) and **Calcite-397** (optimizer rewrites with schemas and integrity constraints). Each case is two queries plus a schema with constraints (primary and foreign keys, NOT NULL, `CHECK`-style predicates, consecutive ids, cross-table implications). Unlike SQLSolver's suites, the pairs include inequivalent ones, and the files carry no labels.

Everything is deterministic Python: `kumosql.counterexample` (a constraint-respecting database generator that runs both queries on DuckDB) and the algebraic z3 prover (`kumosql.algebraic_equivalence`, see [sqlsolver.md](sqlsolver.md)). No model is called and no gold label is read.

## Verdicts and evidence levels

| Verdict | Evidence | Meaning |
| --- | --- | --- |
| `equivalent` | **unbounded proof** | The z3 prover proved the result bags equal on every database that satisfies the declared keys and NOT NULL columns. After a proof, 1,000 more random databases (a different seed) must still agree, or the case counts as `wrong`. |
| `different` | **executed counterexample** | A database satisfying every constraint was built and both queries were run on it with different result bags. The difference has to survive three row-order shuffles, so it never rests on `LIMIT` ties or on MySQL's arbitrary pick for a non-grouped column, and both queries have to return the same rows with DuckDB's optimizer switched off (`PRAGMA disable_optimizer`), because the optimizer has returned wrong rows for some correlated subqueries. Re-checking the full LeetCode run this way dropped 3 of 5,519 refutations; Calcite and Literature did not change. |
| `agrees` | **executed datasets only** | Not proven, and 600 random databases (200, then 400 more for pairs that are not proven; 400 wider ones when a query has `HAVING`) showed no difference. This is not a proof and not bounded verification. |
| `unknown` | none | The pair could not be run (a query DuckDB rejects, unreadable constraints). |
| `wrong` | | An `equivalent` verdict contradicted by a counterexample (our own second search, or VeriEQL's published counterexample replayed on DuckDB). Must stay 0. |

VeriEQL itself is *bounded* model checking: "verified" there means no counterexample exists up to a bound on table size, so a VeriEQL pass is weaker than an unbounded proof. The scoreboard keeps the levels apart: the **proof** rows count `equivalent`, the **executed** rows count `different` plus `agrees`. KumoSQL's own bounded checker ([bounded-verification.md](bounded-verification.md)) is run on all three suites and scored in separate **bounded** rows.

## Counterexample generator

`kumosql.counterexample.find_counterexample(spec, left, right)` returns a `Counterexample` (the rows of every table plus both result bags, and `.script(spec)` for a runnable `CREATE`/`INSERT` script). Databases are small (up to 5 rows per table); values come from small domains seeded with the literals of the two queries (the constant, one below, one above, string and date literals), a "hot subset" per column makes ties and join matches common, and NULLs appear on nullable columns. When a query filters groups (`HAVING`) and nothing else settled the pair, a wider search follows: tables of 3 to 8 rows, plus n to n + 2 rows for each `COUNT(..) > n`-style test up to 12; some columns drawn from their whole domain, so one group can hold many distinct values; and half of each integer literal, so two values can sum to a `SUM(..) >= n` threshold exactly. Proofs of such pairs are re-checked on 200 of these wider databases too. Constraints are respected by construction (keys, foreign keys by drawing from parent rows, consecutive ids) or by rejection (`CHECK`-style predicates and implications; a NULL never satisfies one, which is the strict reading and therefore valid under both).

MySQL lets a grouped query read ungrouped columns; DuckDB refuses. Such columns are wrapped in `ANY_VALUE`, and any difference found must be stable under shuffling the input rows, so only functionally determined columns can produce a counterexample. Keys inside `GROUPING SETS`, `ROLLUP` and `CUBE` count as grouped, and columns inside an aggregate's `FILTER (WHERE ...)` are left alone.

A table that a foreign key points at gets rows even when neither query reads it, so a query over a child table alone (EMP, whose DEPTNO references DEPT) is searched with a non-empty child. A database on which a query raises an error (a failed cast, `SINGLE_VALUE` over two rows) is skipped, and the search goes on with the next one.

Every difference is run a second time with DuckDB's optimizer turned off (`kumosql.duckdb_load.run_unoptimized`) and counts only when both runs return the same rows. DuckDB 1.5's optimizer returns wrong rows for some correlated subqueries (for example `EXISTS (SELECT 1 FROM u WHERE u.d <> t.a AND t.b > u.c)` when the tables hold NULLs), which would otherwise refute an equivalent pair or fail a correct proof. The same recheck guards `sqlsolver_bench.differ` (SQLSolver, QED, R-Bot, Calcite-mined, Cosette, SPES), the Singh & Bedathur search and `kumosql.random_check`.

## Harness translation

The Calcite-397 queries are printed by Calcite, and some of its spellings mean nothing to DuckDB or to the prover. Each translation below has one reading; anything else is left as written, so the pair stays `unknown` instead of being scored under a guess.

| Calcite spelling | Run as | Where |
| --- | --- | --- |
| `$f0`, `EXPR$1`, `$cor0` | `_S_f0`, `EXPR_S_1`, `_S_cor0` (DuckDB rejects `$` in bare names) | `counterexample.to_duckdb` |
| `COUNT(a, b)` | `COUNT(CASE WHEN a IS NOT NULL AND b IS NOT NULL THEN 1 END)` | `to_duckdb` |
| `FIRST_VALUE(x)` / `LAST_VALUE(x)` as an aggregate | DuckDB `first(x)` / `last(x)` (differences that depend on row order are dropped) | `to_duckdb` |
| `SINGLE_VALUE(x)` | `x` of the only row, NULL with none, an error (database skipped) with two | `to_duckdb` |
| `SELECT FROM t` (no columns) | one constant column, excluded again from an outer `*` | `to_duckdb` |
| `ORDER BY NULL` | dropped | `to_duckdb` |
| `$cor0.$f0`, where `$f0` is a column of the LATERAL subquery, not of `$cor0` | that subquery's alias, when exactly one source has the column | `tools/bench_sql_repairs.py` |
| `SELECT *` over a join with a repeated column, inside a derived table | the columns spelled out, later copies named `SAL_1` as DuckDB names them (`t.SAL` is the first copy, as in Calcite) | `bench_sql_repairs.py` |
| `a \|\| b` (concatenation; MySQL reads `\|\|` as OR) | `CONCAT(a, b)`, NULL when either is NULL | `bench_sql_repairs.py` |

Literature pairs call uninterpreted predicates on whole rows (`B1(X)` where `X` names a FROM item). The harness spells each out over the row's columns (`B1(X.a, X.b)`): the prover reads it as an uninterpreted function, and DuckDB runs it as a macro with one fixed, arbitrary interpretation (a hash of the arguments). A difference under that interpretation refutes the pair, since an equivalent pair must agree under every interpretation.

The prover gets the schema's foreign keys on a second attempt, after a first attempt with keys and NOT NULL columns alone (the foreign-key rules can rewrite one side out of the shape the other side's proof needs).

Left unknown on purpose: 5 pairs whose Calcite text lost a correlated column (`WHERE * = t5.DEPTNO`), 2 with a subquery in an outer join's `ON` (DuckDB cannot run it), 1 comparing VARCHAR with INT (MySQL coerces, DuckDB refuses), 1 that names a renamed lateral column (`$cor0.SAL0`), and 2 malformed Literature pairs.

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
| Literature | 64 | 24 | 25 | 13 | 2 | 0 |
| Calcite-397 | 397 | 315 | 28 | 45 | 9 | 0 |
| LeetCode (all pairs) | 23,994 | 4,793 | 5,676 | 12,454 | 1,071 | 0 |

The [harness translation](#harness-translation), with the bare-word and `$` fixes from the full LeetCode rerun, took Calcite-397 from 197 proved, 15 refuted and 96 not run to these numbers, and Literature from 10 proved and 11 not run. VeriEQL marks five of our Calcite refutations equivalent (pairs 80, 120, 126, 257 and 367); each counterexample was executed and read by hand, and the queries do differ (for example pair 120's rewrite counts `DISTINCT ENAME` once per `JOB` in the ROLLUP subtotal rows, and pair 80 returns `'TABLE'` against `'TABLE '` because Calcite pads CHAR literals and DuckDB's VARCHAR does not).

The ORDER BY, LIMIT and OFFSET rules ([sqlsolver.md](sqlsolver.md#order-by-limit-and-offset)) added 5 Calcite-397 proofs (measured against master when they were merged), among them `ORDER BY 2 OFFSET 1` against `ORDER BY DEPTNO OFFSET 1` over a duplicated column, `ORDER BY CAST(DEPTNO AS DOUBLE)`, and a top-k pushed into `UNION ALL` branches.

On LeetCode, 3,310 of the 3,586 pairs VeriEQL itself refutes are refuted here (92%); we also refute 2,366 pairs VeriEQL timed out on, could not read or did not refute. VeriEQL marks 30 of our LeetCode refutations equivalent; the ones read by hand differ on an empty table or a NULL (`NOT IN` over a NULL, `ROUND(SUM(..) / COUNT(*))` against `COALESCE(.., 0)`), cases VeriEQL's bounded search did not reach. VeriEQL refutes one pair we prove (21215) without a counterexample that replays; the two queries differ only by `GROUP BY 1` against `GROUP BY E1.EMPLOYEE_ID` and a `WHERE EMPLOYEE_ID IS NOT NULL` on the primary key, so the proof stands. The LeetCode row was measured on master 7b7ba13 (with the harness translation, the HAVING-only wider search and the optimizer recheck); prover rules merged after it, such as `cast_rules.py`, are not in it yet. The full LeetCode run takes about 5.8 hours on 4 cores. Search settings were tuned on every 24th pair; every other pair is untouched by that tuning. "Not run" means a query that DuckDB or the parser rejects (a table the schema names differently, `GROUP BY` positions that name aggregates, Calcite-only syntax). Translation handles a set operation of bare table names (`(R UNION ALL S)`, relational-algebra shorthand in two Literature pairs, spelled `(SELECT * FROM R UNION ALL SELECT * FROM S)` and marked `adapted`; both are then proved), bare words used as strings, `$` in names, unqualified `GROUP BY` names that DuckDB calls ambiguous, `SUBDATE`/`ADDDATE`, `CROSS JOIN .. ON` and MySQL's ungrouped columns.
