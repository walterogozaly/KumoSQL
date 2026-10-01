# Algebraic prover (SQLSolver's arithmetic) for BigQuery and Dataform SQL

KumoSQL ports the core of [SQLSolver](https://github.com/SJTU-IPADS/SQLSolver) (SIGMOD 2024, Apache-2.0) to Python on top of `z3-solver`. What is taken is its *system of arithmetic*: a query is a function from tuples to multiplicities (natural numbers), so under bag semantics `UNION ALL` is addition, a join is multiplication, a filter or projection is linear, and equivalence becomes an arithmetic identity that an SMT solver can decide. Everything runs in pure Python from a user-level `pip install`, so it works on locked-down work computers: no Java, no native Z3 build, no admin rights.

## Plan

1. **Algebraic normal form (done, `algebraic_equivalence.py`).** Joins, filters and projections distribute over `UNION ALL`; `COUNT`, `SUM`, `MIN` and `MAX` over a union become partial aggregates combined arithmetically (with or without `GROUP BY`); redundant regrouping is dropped; union shapes are canonical. Every rewrite is checked against SQLite on random databases (NULLs and empty tables included).
2. **Multiplicity algebra (next).** Replace text-matching of derived tables with a real intermediate representation: queries as sums of products of table multiplicities, `DISTINCT` as squash (`‖x‖`), selection as an indicator, and unification of bound tuple variables. Equivalence of two normal forms is discharged by Z3 over integers. This subsumes the bijection search in `smt_equivalence.py` for bag queries and handles nested derived tables and CTEs uniformly.
3. **LIA\* reduction.** SQLSolver's key step for aggregates over joins and nested subqueries: group cardinalities and per-group sums are encoded as sets of integer vectors with a star operator, then reduced to plain linear integer arithmetic (a Parikh-style closed form) that Z3 decides. Done in Python with `z3-solver`; the reduction is validated against SQLSolver's published benchmarks (Calcite, TPC-H) and the existing SQLite fuzz harness.
4. **BigQuery and Dataform front end.** Schemas come from Dataform declarations and BigQuery table metadata (types, `NOT NULL`, partition and unique keys when known, which strengthen proofs). Dialect handling for `SAFE_*`, `IFNULL`, date functions and `UNNEST` stays conservative: unsupported shapes return `not_proven`, never a wrong proof.
5. **Pipeline and UI.** Rewrite verification and the equivalent-work reports use the algebraic prover; a Settings toggle and a status line show which stage produced each proof.
6. **Optional Java SQLSolver (kept as a cross-check, done as an adapter).** `sqlsolver_backend.py` can run the real jar from a user folder when present, as a second opinion after the Python prover. It is never required. A setup helper and Windows validation of this path are last and lowest priority.

## Windows without admin rights

Steps 1–5 need only `pip install --user` (or a venv) for `sqlglot` and `z3-solver`; the z3 wheel bundles its own native library. Only step 6 needs Java, a Z3 Java binding and Windows Z3 DLLs, all fetched into one user folder (`KUMOSQL_SQLSOLVER_HOME`, default `%LOCALAPPDATA%\kumosql\sqlsolver`). SQLSolver's license (Apache-2.0) permits redistribution with its LICENSE and NOTICE; nothing is vendored today.

## Optional Java backend

`prove_equivalent(left, right, schema=...)` in `sqlsolver_backend.py` runs the algebraic prover first and asks the real SQLSolver only when no proof is found. Layout of the user folder:

```
<home>/sqlsolver.jar     built with `gradle fatjar` from SQLSolver (Java 17)
<home>/lib/              Z3 4.13 natives: libz3 + libz3java (.dll / .so / .dylib)
<home>/jre/bin/java      optional portable JRE 17+, used when no Java is on PATH
```

Java is taken from `KUMOSQL_JAVA`, `<home>/jre`, `JAVA_HOME`, then `PATH`; `python -m kumosql prove-sql-sqlsolver --check` says what is missing. BigQuery SQL is parsed with sqlglot and re-emitted as one line of Calcite SQL (table names flattened to one identifier, schema as `CREATE TABLE`). Queries using `UNNEST`, `QUALIFY`, windows, arrays, structs, `PIVOT`, `TABLESAMPLE`, nondeterministic functions or tables missing from the schema are refused. SQLSolver answers `EQ` when both queries fail its semantic checks, so each query is also compared with an always-empty wrapper of itself; a control that comes back `EQ` voids the proof. SQLSolver's `NEQ` carries no counterexample, so it is reported as `not_proven`; counterexamples come from the Z3 stage.

## Benchmark coverage

`tools/sqlsolver_bench.py` runs the benchmark pairs published with SQLSolver (copied to `tests/fixtures/sqlsolver/`, Apache-2.0, LICENSE alongside) through the prover. SQLSolver's authors state every pair is equivalent, so the benchmark measures how many pairs KumoSQL **proves** versus leaves **unknown**; a pair is never reported "not equivalent" just because it was not proved. Each proof is re-checked on 60 random DuckDB databases (NULLs, duplicates, empty tables; NOT NULL and primary keys respected) and any disagreement counts as a **wrong** proof. `tests/test_sqlsolver_benchmarks.py` fails on any wrong proof or when a suite falls below its floor.

Run it with `python tools/sqlsolver_bench.py [calcite|spark|tpch|tpcc]`. Pairs are read as MySQL and translated to BigQuery for the prover.

| Suite | Pairs | Proved | Unknown | Wrong | Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| Calcite | 232 | 165 | 67 | 0 | was 163 before filter-into-HAVING folds, DISTINCT over UNION ALL and hidden ORDER BY keys; 162 before AVG became SUM / COUNT; 160 before window functions were read as a kept-whole derived table; 159 before derived aggregates were compared by proof; 158 before filtering derived tables under a grouping were folded in; 147 before aggregates over outer joins; 138 before case-insensitive columns, UNION ALL column pruning, mixed HAVING and constant set sources; 93 before EXISTS/IN and outer joins |
| Spark SQL | 127 | 106 | 21 | 0 | was 105 before columns of joins were qualified from the schema; 99 before constant aggregates and function identities; 86 before |
| TPC-H | 22 | 21 | 1 | 0 | was 20 before casts that keep every value were dropped by declared column type and grouped derived tables were listed in a fixed column order; 19 before outer-join filters and joins under a grouping were read directly; was 16 before semi/anti joins, `IN` over a grouped subquery and repeated existence tests; 15 before correlated scalar aggregates became joins and derived aggregates were compared by proof; 14 before NULL guards, `1.00 = 1` and folded derived tables made the two spellings of a scalar subquery read alike; 8 before YEAR()/EXTRACT unification and uncorrelated scalar subqueries; the one left (18) needs a semi-join filter pushed into a scalar aggregate |
| TPC-C | 19 | 19 | 0 | 0 | was 17 before LIMIT |

The benchmark runs with `exact_arithmetic=True` (mathematical integers, as SQLSolver assumes), output names ignored, NOT NULL and primary keys from the schema, and the input read as MySQL. "Unchecked" proofs (a few pairs that DuckDB itself rejects) are listed in the tool output.

SQLSolver's own proved counts are in its paper; they are not repeated here because they could not be checked against the repository, which publishes inputs only.

## Rewrite verification and declared facts

`kumosql.prover_context.prove` is the one entry point the app uses; `verify_rewrite` calls it for any changed statement when the solver is enabled (`prover` section of the saved settings, default on, 5000 ms). The facts it may assume come from `kumosql.prover_schema`: BigQuery `REQUIRED` columns and `tableConstraints.primaryKey` from the saved catalog (`bigquery_catalog.saved_tables`), and Dataform `assertions` read by `pipeline_loading` into `Model.non_null` / `Model.unique_keys`. A table is registered under each spelling (`project.dataset.table`, `dataset.table`, `table`); a bare name shared by two tables is dropped. Proofs that used any declared fact list the assumption "declared keys and NOT NULL columns hold in the data".

## Saved equivalences and pipeline comparison

`kumosql.equivalences` stores declarations (`left`, `right`, `columns` pairs, `whole`) in `equivalences.json` in the data folder, one per `right` table and never cyclic. A declaration is an assumption on base relations: each reference to `right` is rewritten to `(SELECT left.x AS y, ... FROM left)`, so the solver sees one relation and keys, joins and aggregates reason over it unchanged. A columns-only declaration provides only the listed columns, so a query that reads another column of `right` fails to compile and stays unknown. Proofs that used a declaration list it in their assumptions.

`kumosql.pipeline_equivalence.prove_models` compares two models of a pipeline. Models are visited from the sources up; each model's SQL is read with equivalent tables substituted (declarations and earlier lemmas), compared only with models that now read the same tables, and a proved pair becomes a lemma (a positional column mapping) for the layers above. If the final models still differ, both are inlined as derived tables (400,000 characters at most) and the flat queries are compared. At most 300 solver calls are spent on lemmas; the result is `equivalent` only when every step was proven, with the lemmas and declarations used reported.

Everyday BigQuery refactors (CTE inlining, `USING` joins, `* EXCEPT`, `COUNTIF`, `SAFE_DIVIDE`, `QUALIFY` dedupes, `IN` versus `EXISTS`, windows through a CTE) are pinned in `tests/test_bigquery_refactors.py`, with near misses (a moved boundary, `SAFE_DIVIDE` versus plain division, a window ordered the other way) that must stay unproved.


## Keyed dimension joined to a grouped fact

With a declared key on `customers`, `customers JOIN (SELECT customer_id, SUM(x) ... GROUP BY customer_id)` is rewritten to the flat join grouped by the key, so a CTE-reuse refactor and its flat form are proved equal. Without the declared key nothing changes.

## Everyday refactors

`tests/test_bigquery_refactors.py` lists everyday BigQuery refactors the prover proves and near misses it refuses. Added in this round: a correlated scalar aggregate in the select list versus a left join to the grouped table (the inner join is refused), the sum of grouped sums versus the plain sum (a sum of grouped counts is refused: it reads NULL for no rows), `IN` over a `UNION ALL` versus an `OR` of the branches, a NULL guard under a global aggregate (refused under a `GROUP BY`), `LOWER(TRIM(x))` versus `TRIM(LOWER(x))` and `||` versus `CONCAT`, and a `LIMIT` inside a derived table that the outer select only projects versus the same `ORDER BY .. LIMIT` at the top.
