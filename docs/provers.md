# Equivalence provers

The equivalence checks behind every verified rewrite, from the structural prover to Z3, the algebraic prover and SQLSolver.

## Conservative SQL equivalence

`kumosql.prove_equivalent(left_sql, right_sql)` returns `proven_equivalent` only when both inputs are strict, single-query BigQuery statements whose normalized ASTs match after relational subquery lifting. By default it compares result bags, so unspecified row order is ignored.

Root CTEs are renamed by position after being put in a canonical dependency order, so CTE order alone does not block a proof; reordering is skipped for recursive WITH, forward references, or names that differ only in case.

Before comparing, the prover also removes grouping parentheses, flattens `AND`/`OR` chains, removes `TRUE` from `AND` and `FALSE` from `OR` in filter and join conditions, drops `WHERE TRUE`, merges root CTEs with identical bodies, and drops unreferenced CTEs. These normalizations are written separately from the cleanup rules, and a three-valued-logic test evaluates both against the original predicates.

It refuses to prove queries containing volatile values, windows, tie-sensitive aggregates, `TABLESAMPLE`, or any `LIMIT`/`OFFSET`. Structural differences are reported as `not_proven`, never as a proof of inequivalence. This is intentional: false negatives are acceptable; false positives are not.

For audit or optional execution, `result.verifier_sql` contains a BigQuery query that counts JSON-encoded result rows on each side and compares their multiplicities with a full outer join. The verifier is an execution artifact and does not override the static safety checks.

```python
from kumosql import prove_equivalent

result = prove_equivalent(
    "SELECT id FROM (SELECT id FROM `p.d.customers`) AS c",
    "WITH base AS (SELECT id FROM `p.d.customers`) SELECT id FROM base AS c",
)
assert result.proven
```

CLI usage:

```shell
python -m kumosql prove-sql-equivalent left.sql right.sql --verifier-sql verify.sql
```

## Result equivalence on synthetic data

The static prover only accepts rewrites whose normalized ASTs match. To test rewrites it cannot prove, `kumosql.check_result_equivalence(left_sql, right_sql, schema)` runs both sides against the same deterministic synthetic tables in a local DuckDB engine (BigQuery SQL is translated with `sqlglot`) and compares the results as multisets, including column names.

- Seed 0 is always empty tables; other seeds include NULLs and duplicate rows, drawn from small value domains so joins and groups collide.
- Every run gets a fresh in-memory connection. Tables written by a script (`CREATE TABLE ... AS`, `INSERT`) are renamed to run-unique local names, and the final written table is compared when the script does not end in a query.
- Dataform SQLX is supported: blocks are dropped and `${ref(...)}` becomes a table name. Any other interpolation, unknown table, or execution failure is reported as `error`, never as equivalent.
- A mismatch returns `different` with the failing seed and the rows only one side produced. Agreement is evidence, not a proof.
- Data generation is seeded and repeatable: the same schema and seed always produce the same rows (a test pins a digest of a small dataset, so a change to the value domains or draw order fails loudly). Column order in the schema mapping is part of the input.
- Each side is executed twice per seed on identical data. A side that differs from itself (for example `RAND()` or `GENERATE_UUID()`) makes the result `inconclusive`, naming the side and seed, instead of a false `different` or `equivalent`. Failures while fetching results are reported as `error`.

```python
from kumosql import assert_result_equivalent, lift_subqueries

schema = {"p.d.orders": {"customer_id": "INT64", "amount": "FLOAT64"}}
original = "SELECT * FROM (SELECT customer_id, SUM(amount) AS total FROM `p.d.orders` GROUP BY 1) AS t"
assert_result_equivalent(original, lift_subqueries(original).sql, schema)
```

### Recording synthetic evidence on a rewrite result

`attach_synthetic_check(result, schema, seeds=range(8))` (or `python -m kumosql rewrite-sql --synthetic-check --synthetic-schema schema.json [--synthetic-seeds N]`) runs the harness on a changed `apply_rules` result and records one `synthetic_results` check. It is opt in; ordinary rewriting never executes anything. `schema` maps each source table as written in the SQL to its columns and BigQuery types.

| Outcome | Meaning | Effect on the label |
|---|---|---|
| `passed` | Outputs matched on every seed | None. Agreement is evidence, not proof: an unproven result stays `unproven`, is never `trusted`, and `proven` is untouched. `summarize_evidence` counts it as `synthetic_agreed` and useful evidence. |
| `failed` | A counterexample was found | The result drops to `unproven` (a proven result too, since a disagreeing input contradicts it). The check names the failing seed, the difference kind, row counts and up to three synthetic rows per side; it never contains query text or column names. |
| `inconclusive` | A query differs from itself on identical data (`RAND()`, `GENERATE_UUID()`) | None. |
| `not_run` | Nothing was compared: duckdb missing (`kumosql[execution]`), output unchanged, a rule failed, an unsupported column type, or a query that cannot be translated or executed locally | None. |

Every outcome records `seeds`, `seeds_checked`, `failing_seed`, `rows_per_table`, `null_rate` and the engine version, so a failure reproduces with `generate_synthetic_dataset(schema, seed=<failing_seed>)`. Attaching again replaces the earlier synthetic check.

Install the engine with `pip install -e ".[execution]"` (it is included in `.[dev]`). `tests/test_result_equivalence.py` runs every lifted query in its corpus through the harness and checks that deliberately broken rewrites are caught.

## SMT equivalence prover

`kumosql.prove_equivalent_smt(left_sql, right_sql)` proves semantic equivalence with Z3 instead of comparing syntax, so it accepts rewrites such as filter pushdown into a CTE, join reordering, `DISTINCT` to `GROUP BY`, `CASE` to `IF`, redundant predicates, and self-join elimination under `DISTINCT`. Install it with the optional extra: `pip install -e ".[smt]"`.

It models inner and cross joins, `WHERE`, derived tables and CTEs, `GROUP BY`/`HAVING` with `COUNT`, `SUM`, `MIN`, `MAX`, `AVG`, `COUNTIF`, `LOGICAL_AND`/`LOGICAL_OR`, `UNION ALL`/`UNION DISTINCT`, `SELECT DISTINCT`, and NULLs with three-valued logic. Other deterministic functions are uninterpreted: equal inputs give equal outputs. `UNION ... BY NAME` and `CORRESPONDING` are rewritten to the positional form first (`kumosql.set_operations`); when a branch's columns are not known (`SELECT *`, duplicate names) or differ under a plain `BY NAME`, the result is `not_proven`. Anything else (outer joins, windows, `LIMIT`, predicate subqueries, nondeterministic functions) returns `not_proven`.

The result is one of:

- `proven_equivalent`: the two queries return the same bag of rows on every database, under the listed `assumptions` (no NaN, runtime errors not modeled, column types not compared).
- `not_equivalent`: `counterexample.tables` is a small database on which the queries return different rows (`left_rows`, `right_rows`). It is only reported when no uninterpreted function is involved.
- `not_proven`: outside the subset, or no proof was found.

**Executed counterexamples.** The solver's model gives no counterexample for outer joins, duplicate-producing joins, `NOT IN` with NULLs or set operations of different shapes, so such pairs used to stay `not_proven`. `prove_equivalent_algebraic(..., search_counterexample=True)` (on for **Compare queries** and `POST /api/prove-queries`) then runs both queries on the corner-case, targeted and random databases of `kumosql.targeted_data`, built around both queries and respecting declared NOT NULL columns, keys and foreign keys, and returns the first database on which the result bags differ, shrunk row by row, as a `not_equivalent` counterexample (`src/kumosql/executed_refutation.py`). It is a refutation that needs no trust in the prover: anyone can replay it. To keep it a refutation of the BigQuery queries rather than of DuckDB's reading of them, the search runs only when every column type is declared, both queries stay within an allow-list whose DuckDB translation evaluates as in BigQuery (joins, set operations, subqueries, comparisons, arithmetic, `CASE`/`IF`/`COALESCE`, plain aggregates and aggregate windows; no `LIMIT`, string or date functions, `LIKE`, arrays, `ROW_NUMBER` or nondeterministic functions), a zero divisor fails the run instead of returning infinity, and the difference survives rounding floats to 6 digits and reversing every table's rows. Finding nothing proves nothing.

```python
from kumosql import prove_equivalent_smt

result = prove_equivalent_smt(
    "SELECT id FROM (SELECT id, a FROM t WHERE a = 1) AS s WHERE id > 0",
    "WITH s AS (SELECT id, a FROM t WHERE id > 0) SELECT id FROM s WHERE a = 1",
)
assert result.proven
```

Pass `schema={"t": ["id", "a"]}` to enable `SELECT *` and unqualified columns in joins, and `exact_arithmetic=True` to reason about `+`, `-` and `*` exactly (right for INT64 and NUMERIC, not FLOAT64). `group_by_constants=True` reads a literal in `GROUP BY` as a constant (Calcite's rule) instead of a column ordinal. The CLI prints JSON and exits 0 only on a proof:

```shell
python -m kumosql prove-sql-smt left.sql right.sql --schema schema.json
```

`tests/test_smt_fuzz.py` checks the prover against SQLite on random queries and databases: every proof must hold and every counterexample must separate the queries.

**Solver in rewrite verification.** `verify_rewrite` also hands any changed statement to the algebraic prover (the section below), so a rewrite beyond predicates (a removed self-join, a pushed-down aggregate) can be `proven`. The solver is on by default; **Settings → Solver** (or `GET`/`PUT /api/prover`) turns it off and sets the time limit per check (500 to 60000 ms, default 5000). It assumes only what is declared: `REQUIRED` columns and a primary key from the saved BigQuery catalog, and the `assertions` (`nonNull`, `uniqueKey`, `uniqueKeys`) of the loaded Dataform project, plus the columns of each model's SELECT. Neither BigQuery nor Dataform enforces these when data is written, so a proof that used them lists that as an assumption.

**Saved equivalences across layers.** Tell KumoSQL that two tables hold the same data under different names ("column `amt` of `raw.orders` is column `amount` of `raw.orders_v2`") and the proofs use it, including many layers down a pipeline. In **Settings → Solver → Equivalent columns**, or with `python -m kumosql equivalence add raw.orders raw.orders_v2 amt=amount id=order_id --whole`, save the pair; list columns only (the listed columns hold the same rows, so it applies to queries that read nothing else from the second table) or tick *same rows* for the whole table. They are stored in the data folder (`equivalences.json`), never written to BigQuery, and used as an assumption that proofs list. Rewrite verification applies them automatically. **Compare tables** in the same page, `POST /api/prove-tables` and `python -m kumosql prove-tables LEFT RIGHT --project DIR` prove two models of a pipeline equivalent: models are matched layer by layer from the sources up, each match becoming a lemma for the layers above (so one declaration on a source ripples up through every layer built on it), and when the pipelines are cut at different places the models are inlined as derived tables and the flat queries compared. **Compare queries** (same page, `POST /api/prove-queries`) proves two pasted queries equivalent with the same declared keys, types and saved equivalences, and shows a counterexample database when they differ. Workspace and Changes list the assumptions a proof relied on under its checks.

## Algebraic prover and SQLSolver (no admin rights)

What makes [SQLSolver](https://github.com/SJTU-IPADS/SQLSolver) (SIGMOD 2024) strong is that it treats queries as arithmetic over tuple multiplicities: under bag semantics `UNION ALL` is addition, a join is multiplication, and aggregates over a sum split into partial aggregates combined arithmetically. `kumosql.algebraic_equivalence` brings that into KumoSQL in pure Python on top of the existing Z3 prover, so it installs per user with `pip install --user -e ".[smt]"`:

- joins, filters and projections distribute over a derived `UNION ALL`;
- `COUNT`, `SUM`, `MIN` and `MAX` over a `UNION ALL` equal the same function over per-branch partial aggregates (`COUNT` combines with `SUM`), with or without `GROUP BY`;
- redundant regrouping of an already-grouped subquery is dropped, also when an output combines several aggregates (`SUM(total) / SUM(n)` over a finer rollup becomes `SUM(x) / COUNT(x)`, `regroup_arithmetic.py`), and union branches are put in a canonical order and naming so equal shapes compare equal.

Schema facts strengthen proofs: pass `constraints={"orders": TableConstraints(not_null=frozenset({"id"}), keys=(("id",),))}` to `prove_equivalent_smt` or `prove_equivalent_algebraic` and the prover uses NOT NULL columns and keys (a join of a table to itself on its key is dropped, `IS NOT NULL` on a NOT NULL column is redundant), and counterexamples respect them. Blocks that can never return a row are dropped (so `WHERE a = 1 AND a = 2` equals `WHERE 1 = 0`), a global aggregate over no rows is the constant row `COUNT = 0`, others NULL, `compare_names=False` ignores output column names and `dialect="mysql"` reads other dialects. `EXISTS`, `IN`, `NOT IN`, `NOT EXISTS`, `INTERSECT`, `EXCEPT` and `LEFT`/`RIGHT`/`FULL` joins are handled as existence tests and anti-joins: two tests are the same when each implies the other, a required test on a unique key or under `DISTINCT` becomes a join, and an outer join is an inner join plus a NULL-padded no-match branch. A join to a `DISTINCT` or `GROUP BY` derived table on all of its columns is the same existence test, and `HAVING` on group keys is a `WHERE`. A DISTINCT aggregate finished in a second grouping (`SUM(p)` over `GROUP BY k, x` next to `SUM(x)`) is read back as `SUM(DISTINCT x)`, and `MIN(k)`/`SUM(DISTINCT k)` of a group key is the key. `ORDER BY .. LIMIT n [OFFSET m]` (or `ORDER BY .. OFFSET m` alone) at the end of a query is compared by limit, offset and ordering (by output column) with the queries underneath proved equivalent; when the ordering leaves ties, the result's assumptions say rows tied on it must be cut the same way. Cuts inside the query are lifted, merged or dropped only where that holds for every way of breaking ties (see [ORDER BY, LIMIT and OFFSET](sqlsolver.md#order-by-limit-and-offset)). `LIMIT 0` is an empty result, and a `LIMIT` inside a derived table is read as the same relation only when its text matches on both sides (also listed as an assumption). Constant dates are folded (`DATE('1994-09-01 +08')`, `date + INTERVAL 3 MONTH`, `DATE_ADD`) so equal dates compare equal, and string and date ordering uses a one-to-one rank instead of Z3's slow string order. Pre-aggregated tables are unnested (`src/kumosql/eager_aggregation.py`): an aggregate over a join with a grouped subquery (`SUM(d.s * w)`, `SUM(d.count)`, `MIN`/`MAX` of its own `MIN`/`MAX`) becomes the flat aggregate, and a join of grouped subqueries read off by arithmetic (`p.sum * q.count`) becomes one aggregate over the flat join; shapes that would change how many times a group is counted are left alone. An aggregate of values that are all NULL is constant (`COUNT(NULL)` is 0, `SUM(NULL)` is NULL, a global one is a single constant row), `UPPER(LOWER(x))` is `UPPER(x)`, `CONCAT` nests flatten, `(a, b) IN (SELECT x, y ...)` is an existence test on every column. An aggregate over an outer join reads the join as one derived relation with positionally named columns (and a derived table that only computes expressions over one table is folded into the query that uses it), so two spellings of the same join compare equal. Column names are case-insensitive, a `UNION ALL` source is read only for the columns the query uses (in any order), the group-key conjuncts of a mixed `HAVING` move to `WHERE`, and a `GROUP BY TRUE` derived table over constants is an existence test. A derived table that only filters or joins (no grouping) under a grouped select is folded into it, `x IS NOT NULL` is dropped when another condition compares `x` or the column is declared NOT NULL (and no outer join can pad it), and `1.00` reads as `1`, so a derived relation written two ways has one identity. A correlated scalar aggregate compared in WHERE (`x < (SELECT 0.2 * AVG(q) FROM t WHERE t.k = outer.k)`, not `COUNT`, equality correlations only) is read as the join with a `GROUP BY k` table, a derived relation is identified by position rather than output name, two derived relations written differently are one relation when the prover shows them equal, and `HAVING MIN(x) IS NOT NULL` is dropped when `x` is never NULL. `a LEFT SEMI JOIN b ON c` is `WHERE EXISTS (SELECT 1 FROM b WHERE c)` (`LEFT ANTI JOIN` is `NOT EXISTS`), `x IN (SELECT k FROM t GROUP BY k HAVING f(agg))` reads the groups as a derived table with the HAVING as a condition on its aggregates, and two existence tests of one query that its plain conditions make equal (`o.k = l.k AND EXISTS(.. o.k) AND EXISTS(.. l.k)`) are one test. Window functions are computed in a derived table over the select's FROM and WHERE (`QUALIFY` becomes a condition on it), which the prover keeps whole: two queries agree when their window computations read alike, and the proof assumes ties in ORDER BY resolve the same way. `AVG(x)` is `SUM(x) / COUNT(x)`, so it equals the spelled-out quotient (and `COUNT(*)` when `x` is never NULL). BigQuery conveniences read as their plain forms: `WITH` tables are inlined, `JOIN .. USING (k)` is `ON a.k = b.k` (the merged column is read from the left side, `SELECT *` lists it first), `* EXCEPT (..)` and `* REPLACE (..)` expand to explicit columns, `COUNTIF(c)` is `COUNT(CASE WHEN c THEN 1 END)` and `SAFE_DIVIDE(a, b)` is `IF(b = 0, NULL, a / b)`; `tests/test_bigquery_refactors.py` lists the everyday refactors proved and the near misses refused. An uncorrelated scalar subquery (`x = (SELECT MAX(r) FROM t)`) that appears on both sides, written the same or provably equivalent, is one shared unknown value (every column in it must bind inside it, otherwise it is left alone; the proof assumes it returns at most one row), `YEAR(d)`/`MONTH`/`DAY` equal `EXTRACT`, `(SELECT 1)` is read as `1`, `IN`/`EXISTS` over a subquery with `WHERE FALSE` or `LIMIT 0` as FALSE, and `agg(x) FILTER (WHERE c)` as `agg(CASE WHEN c THEN x END)`. With a declared key, a keyed table joined to a pre-aggregated one (`customers JOIN (SELECT customer_id, SUM(x) .. GROUP BY customer_id)`) is the flat aggregate grouped by the key, and `SELECT DISTINCT` over a `UNION ALL` is `UNION DISTINCT`. A filter on the right side of a `LEFT JOIN` written as a derived table is the same as in the ON clause, a grouped select over a derived table that only lists a join's columns reads the join itself, and bare columns in a grouped select over an outer join are matched to their source from the schema. With declared column types (`types={'orders': {'amount': 'DECIMAL(15, 2)'}}`, filled from the saved BigQuery catalog's `INTEGER`/`NUMERIC` columns) a `CAST(x AS DECIMAL(p, s))` that keeps every value of `x` is dropped, and a grouped derived table lists its keys and aggregates in a fixed order so two spellings of it read alike. A correlated scalar aggregate in the select list (`(SELECT SUM(y) FROM b WHERE b.id = a.id)`, not `COUNT`) is a left join to the grouped table, a global `SUM`/`MIN`/`MAX` of a grouped select's own `SUM`/`MIN`/`MAX` is the aggregate over all rows, `x IN (SELECT .. UNION ALL SELECT ..)` is an `OR` of the branches, `WHERE x IS NOT NULL` over a global aggregate of `x` is dropped (`COUNT(*)` becomes `COUNT(x)`), `LOWER(TRIM(x))` is `TRIM(LOWER(x))` `a || b` is `CONCAT(a, b)`, and an `ORDER BY .. LIMIT` derived table that the outer select only projects is the limit at the top. `, UNNEST(arr) [AS t] [WITH OFFSET AS o]` and `CROSS JOIN UNNEST(..)` (not in an outer join) read the array as a table of (array, element, offset) rows with one row per array and offset, so two spellings over the same array compare equal; arrays are known by their text, and no counterexample is generated for a query that unnests. A grouped derived table joined on its key need not repeat an `EXISTS` test that the join already makes on the same key, `EXISTS` filters on a lone-table derived table are tested in the select that joins it, a select that only lists columns of a grouped derived table reads that table, and `IS NOT NULL` on arithmetic over `SUM`/`MIN`/`MAX`/`AVG` of NOT NULL columns is dropped. Counterexamples are not generated for queries with such tests. BigQuery string literals are given one spelling before the provers and the DuckDB re-check read them (`src/kumosql/string_literals.py`): sqlglot keeps escapes such as `\"` undecoded, so `'a\"b'` and `'a"b'` looked like different strings, and it reads two adjacent quoted names (`` `col``col` ``) as one name with a backtick in it. Raw and bytes literals and strings with `\x`, `\u` or octal escapes are left as written, so those stay unproven rather than wrong. `python tools/sqlsolver_bench.py` measures coverage on SQLSolver's published Calcite, Spark, TPC-H and TPC-C pairs (results in `docs/sqlsolver.md`); every proof is re-run on random DuckDB databases. `python tools/rbot_bench.py` does the same for the Calcite rewrite pairs shipped with [R-Bot](https://github.com/curtis-sun/LLM4Rewrite) (Apache-2.0). `python tools/calcite_mined_bench.py` scores pairs mined from Calcite's current rule tests (`tools/calcite_plan_to_sql.py`). `python tools/cosette_bench.py` scores the examples of [Cosette](https://github.com/uwdb/Cosette) (BSD-2-Clause) and the [SPES](https://github.com/georgia-tech-db/spes) Calcite pairs (Apache-2.0) worded differently from SQLSolver's. `python tools/qed_bench.py` scores the Calcite cases of the [QED prover](https://github.com/qed-solver/prover) (MIT), converted from QED's relational-algebra JSON to SQL by `tools/qed_to_sql.py`. A `GROUP BY` or `DISTINCT` over one table that includes a NOT NULL key (or one fixed by `WHERE k = 10`) reads each row's own values, and `EXISTS` over a global aggregate is TRUE.

A query split into partitions by a filter is put back together (`src/kumosql/partition_rules.py`): `UNION ALL` branches that are the same query except for WHERE filters no row can make TRUE twice are one branch filtered by their `OR`, dropped when the `OR` is always TRUE (`p`, `NOT p`, `p IS NULL` under three-valued logic, as in SQLancer's TLP), and a global `SUM` of `COUNT`s, `SUM` of `SUM`s, `MIN` of `MIN`s or `MAX` of `MAX`es over a `UNION ALL` of global aggregates is one aggregate over the `UNION ALL` of their arguments. See [docs/fuzzing.md](fuzzing.md#partition-recombination).

`AVG`, `DISTINCT`, outer joins, `LIMIT` and `UNION DISTINCT` are left alone. `tests/test_algebraic_equivalence.py` runs every rewrite on random SQLite databases (empty tables and NULLs included) to check normalization never changes results.

**Declined, never guessed.** A construct the provers cannot read faithfully makes the result `not_proven`, never a proof:

- Rewritten queries pass between the provers as text, and some sqlglot generators change meaning when they print (MySQL writes `a DIV b` as `CAST(a / b AS SIGNED)`, which rounds where `DIV` truncates, `CAST(x AS BOOLEAN)` as an integer cast, and `FULL JOIN` as a `LEFT`/`RIGHT` union that is wrong under an aggregate). `kumosql.ast_utils.faithful_sql` prints a query, parses it back and compares the two trees; when no spelling reads back the same, the pair is declined.
- A column list on a table alias (`FROM dept AS d(name, x)`, on a table, CTE or derived table) renames by position and is spelled out as explicit renames before proving; a list that cannot be resolved (unknown table, star select, too many names) is declined.
- A derived table folded into the query that reads it keeps its output names, and a rewrite that would rename a derived table's outputs is not applied.
- On the NULL-padded side of an outer join only expressions that are NULL whenever their columns are (columns, arithmetic, comparisons, a `CASE`/`IF`/`COALESCE` whose every result is such an expression) are folded into the outer query; constants, `IS NULL` and the like stay in the derived table.
- `GROUP BY 2` and `ORDER BY 2` are spelled out as the second output's expression first, so a constant folded into those clauses later is not read back as a column position.
- A select-list `x IN (SELECT y FROM t ...)` rewritten as `EXISTS (... AND y = x)` qualifies `x` by its outer table, and a subquery table of the same name gets a fresh alias, so the inner scope cannot capture `x` (`deptno IN (SELECT deptno FROM emp ...)` over `emp` is not `deptno = deptno`); when the owner of `x` is unclear the rewrite is skipped.
- Before any rewrite, a bare column in a select with several sources is written with its source's name, so reading a derived table as its base table (which brings that table's other columns into scope) cannot capture it or make it ambiguous.
- The same goes for a bare column a subquery reads from an enclosing query (`EXISTS (SELECT 1 FROM (SELECT 2 * deptno AS f FROM dept) AS t WHERE deptno = t.f)`): when a derived table on the way reads a table with a column of that name, the column is written with its enclosing source's name first; if a source on the way reuses that name, the query is declined.
- A table function handed a CTE by name (DuckDB's `histogram_values(cte, l)`, BigQuery's `TABLE cte`) is declined by every prover, since such a read is not a table reference and the CTE would look unused.

`tests/test_soundness_regressions.py` keeps each wrong proof found so far, with the database on which DuckDB shows the two queries differ, next to equivalent near misses that must stay proven.

[docs/singh-bedathur.md](singh-bedathur.md) scores the 2,800 public LeetCode pairs from Singh and Bedathur's SQL-equivalence study with no model at run time (`python tools/singh_bedathur_bench.py`): each pair is proved equivalent, shown different by a DuckDB counterexample database, or left unknown. `kumosql.canonical_rules.canonicalize` holds the constraint-free rewrites (merging a select into the one derived table it reads, `DISTINCT` over selected group keys, `IN` over a grouped table as a join) that the harness tries when the prover finds no proof.

`kumosql.sqlsolver_backend.prove_equivalent(left, right, schema=...)` runs the algebraic prover first and, only when it finds no proof, the real SQLSolver jar if one is installed in a user folder. See [docs/sqlsolver.md](sqlsolver.md) for setup, the translation rules and the rollout plan.

`python tools/verieql_bench.py` scores the three [VeriEQL](https://github.com/VeriEQL/VeriEQL) suites (LeetCode, Literature, Calcite-397; the paper is "VeriEQL: Bounded Equivalence Verification for Complex SQL Queries with Integrity Constraints", OOPSLA 2024) with no LLM: `kumosql.counterexample` builds databases that satisfy the schema's constraints and runs both queries on DuckDB, and the z3 prover supplies unbounded proofs. Unbounded proofs, executed counterexamples and executed-dataset agreement are reported as separate evidence levels in the scoreboard. The VeriEQL data is CC BY-NC-SA 4.0: it is downloaded on first use into a cache folder, never stored in this repository, and none of its code is vendored. See [docs/verieql.md](verieql.md).

```shell
python -m kumosql prove-sql-sqlsolver left.sql right.sql --schema schema.json   # --backend auto|algebraic|sqlsolver|z3
python -m kumosql prove-sql-sqlsolver --check                                   # is Java + SQLSolver usable here?
```

The SQLSolver stage needs a schema listing every table with columns, optionally typed: `{"proj.ds.orders": [["id", "INT64"], ["status", "STRING"]]}`. It is tested against a stand-in for Java; end-to-end runs against a real SQLSolver build are the next step in the plan.

SQL-IQ's SQL Equivalence Judge, SQL Judge and Error Classification tasks are scored with these provers and hand-written rules and no language model: `python tools/sqliq_bench.py --data <SQL-IQ checkout>` (see [docs/sql-iq.md](sql-iq.md); the SQL Judge and Error Classification rules were tuned on SQL-IQ's own data, so those two scores are tuned-on-test).

`kumosql.bounded_equivalence` is a third level between a proof and executed datasets: a z3 check that two queries agree on every database with at most N rows per table, written from the VeriEQL paper (OOPSLA 2024) and sharing none of its code. Its answer reads "bounded, N rows" and is never called a proof; every counterexample is replayed on DuckDB. See [bounded-verification.md](bounded-verification.md).
