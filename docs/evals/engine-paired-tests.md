# Paired engine tests

157 query pairs taken from other engines' own tests and bug fixes, plus authored guards, each with a hand-checked label. The provers must prove the equivalent pairs and may refute the others; a proof of a pair that is not equivalent, or any claim that an equivalent pair differs, is wrong. Results file: `engine-paired-tests`. The pairs are in `tests/fixtures/engine_pairs` ([sources, pins, adaptations and overlap](../../tests/fixtures/engine_pairs/README.md)).

```
python tools/engine_pairs_bench.py                    # about two minutes
python tools/engine_pairs_bench.py --show unknown
python tools/engine_pairs_bench.py --write-results
python tools/engine_pairs_bench.py --check-sources    # download the pinned files, check SHA-256, re-extract
```

## Sources

| Source (inventory row) | Pairs | What a pair is |
| --- | --- | --- |
| Trino `AbstractTestJoinQueries` (A-R01, Apache-2.0) | 137 extracted, 134 scored | Every `assertQuery(sql, expectedSql)` whose two SQL arguments are string literals (four more pass a session first; it only fixes the join order). Trino runs the first query and H2 the second over TPC-H tiny, and the test asserts the same rows. 27 pairs read tables on both sides (alternative SQL); 107 compare a query with its expected result written as SQL (`VALUES`, `SELECT 0`). Calls built with `format()` or a query template are not extracted |
| Spark `SubquerySuite` (A-R02, Apache-2.0) | 9 extracted, 7 scored | The EXISTS / IN / NOT EXISTS / NOT IN tests over the `l` and `r` tables (NULLs and duplicates). Queries that expect the same rows are paired; NOT EXISTS against NOT IN is the NULL-sensitive negative. Two pairs use multi-column `NOT IN`, which has no GoogleSQL form, and are kept unscored |
| PostgreSQL commit f4c00d1, bug 17976 (A-R04, PostgreSQL licence) | 2 | The regression query the fix added to `join.sql`, against the join-removed form the expected plan in `join.out` shows (equivalent: `t.a` is UNIQUE) and against the dropped-qual reading of the fault (not equivalent: one row instead of none) |
| DuckDB `test_window_order_collate.test`, issue 20608 (A-R08, MIT) | 4 extracted, 3 scored | The file's two window queries over a `COLLATE NOCASE` column (kept unscored: `row_number()` without `ORDER BY` is not determined), the same pair with `ORDER BY name` (equivalent), an authored `NOACCENT.NOCASE` sibling (not equivalent) and the 20608 `EXCEPT ALL` fixture against the join it subtracts from (same rows on the fixture, `cOB4` kept as stored). These stay in DuckDB SQL |
| JoinEquiv projection boundary (A-R06, authored) | 3 | Our counterexample: an always-false join under `SELECT DISTINCT 1` is empty, but the INTERSECT of the projected LEFT and RIGHT joins is `{1}`. The full-row form is equivalent with NOT NULL columns and not equivalent without them |
| jOOQ documented transforms (A-R07, authored) | 8 | `COUNT(*) > 0` to `EXISTS`, `COUNT(x) > 0` to `EXISTS ... x IS NOT NULL`, unnecessary `DISTINCT` in `EXISTS`, and negative siblings: the dropped `IS NOT NULL`, `HAVING`, an aggregate inside `EXISTS`, `LIMIT 0`. A `GROUP BY` on the correlated key stays equivalent. Written by us from the [jOOQ manual's transformation pages](https://www.jooq.org/doc/latest/manual/sql-building/queryparts/sql-transformation/transform-patterns/); no text is copied |

Labels are **equivalent** (133: same rows on every database the declared keys and NOT NULL columns allow), **fixture** (15: same rows on the source's own data only, for example "no part is named 'a'") and **not_equivalent** (9). Each is written in `why`. TPC-H pairs get the specification's primary keys and NOT NULL columns.

## How a pair is decided

1. **proven**: `kumosql.prove_equivalent` (structural) or `prove_equivalent_algebraic` (with the fixture's types, keys and NOT NULL columns) proves it.
2. **refuted**: the algebraic prover's counterexample (SMT model or executed search) or the targeted refuter (`kumosql.refute.find_targeted_difference`) gives a database, and the database keeps the declared keys and NOT NULL columns and separates the two queries in DuckDB, confirmed with the optimizer off (`run_unoptimized`). An SMT model can use placeholder values (a string in an `INT64` column); such a claim does not count as a refutation, and it was the case for one TPC-H pair, which the targeted refuter then refuted with a typed database.
3. **unknown**: anything else.

Every pair also runs on its source's data: TPC-H at scale 0.01 (Trino's `tiny` schema, made by `tools/benchmark_corpora.py`; it reproduces Trino's expected rows for order 31718), the inline Spark, PostgreSQL, DuckDB and authored tables otherwise. All 157 agree with their label there. Trino's inline `VALUES` become `UNNEST([STRUCT(...)])` in GoogleSQL, which KumoSQL's DuckDB reading refuses; for execution only, the harness rewrites those to the same rows as `SELECT ... UNION ALL SELECT ...`. The provers get the pairs as written.

**Held out.** One case in five, by a hash of its id (36 of 157). Every case, held-out ones included, was seen while building the harness and its Trino translations, so the held-out score is not a clean estimate (tuned on test); no prover change was made.

## Scores

2026-10-04, baseline on master 0f13b9af (sqlglot 30.21, z3-solver 5.1.0.0, an otherwise idle 4-core machine; no prover change was made for this eval): **41/157 decided correctly, 0 wrong**: 20 proved, 21 refuted, 116 unknown. Held out: 11/36, 0 wrong. A first run on 2026-10-03 on an older master measured 33/157; the 8 more are all Trino pairs that agree only on TPC-H tiny and are now refuted. Whether that comes from rules merged since, or from the earlier run having been on a busy machine (the targeted refuter works to a time budget), was not separated.

| Source | Scored | Proved | Refuted | Unknown |
| --- | --- | --- | --- | --- |
| Trino | 134 | 16 | 12 | 106 |
| Spark | 7 | 2 | 2 | 3 |
| PostgreSQL | 2 | 1 | 1 | 0 |
| DuckDB | 3 | 0 | 0 | 3 |
| JoinEquiv (authored) | 3 | 0 | 2 | 1 |
| jOOQ (authored) | 8 | 1 | 4 | 3 |

By origin: original pairs (upstream pairs, translated) 20/122, adapted pairs (paired, completed or changed by us) 14/23, authored guards 7/12. By label: 20/133 equivalent pairs proved, 13/15 fixture-only pairs refuted, 8/9 not-equivalent pairs refuted. 0 prover claims were left unknown for failing to replay.

What is proved: JOIN USING against ON (one and two columns, `SELECT *`, unaliased derived tables), the join order of a date-bounded join, a CTE against its inlined copy, three FULL JOIN counts filtered to unmatched rows against their LEFT JOIN UNION ALL RIGHT JOIN rewrites, the full join with nulls on the probe side (`testOuterJoinWithNullsOnProbe`), five closed pairs over empty inputs, EXISTS against IN and against `EXISTS OR EXISTS` (Spark), PostgreSQL's join removal under a UNIQUE key and `EXISTS (SELECT DISTINCT ...)`. What is refuted: twelve Trino pairs that agree only on the tiny data (the four empty-build-side tests, true only while no part is named 'a'; six single-nation-row and single-order tests), Spark's NOT IN against NOT EXISTS and its fixture-only pair, the PostgreSQL dropped-qual reading, the JoinEquiv projection counterexample and the unguarded full-row form, and four jOOQ negatives. Every refutation database respects the fixture's declared keys and NOT NULL columns (the harness checks this before it counts one).

What stays unknown:

* **Inline rows (about 80 Trino pairs).** sqlglot writes Trino's `VALUES` as `UNNEST([STRUCT(1 AS a), ...])`, the usual GoogleSQL form, and the prover does not read an UNNEST of a STRUCT array outside a cross join, its fields, or `SELECT *` over it. These pairs test outer joins with constant and non-equality conditions, so this is the largest gap the eval shows.
* **Aggregates over outer joins (7)** and **FULL JOIN normalised to LEFT or RIGHT under a NULL-rejecting filter (4)**: the Trino FULL JOIN tests against their LEFT JOIN UNION ALL RIGHT JOIN rewrites.
* `orderkey + 1 = orderkey + 1` against `orderkey = orderkey`, `COUNT(*)` over `orders UNION ALL orders` joined on its key against `2 * COUNT(*)`, Spark's implied ORs, the jOOQ `COUNT(*) > 0` forms, and every DuckDB collation pair (the provers do not model collations; none was proved, including the `NOACCENT.NOCASE` trap).

## Limits

* The GoogleSQL translations were run on DuckDB through KumoSQL's BigQuery reading, not on BigQuery. Two readings are worth knowing: a Trino `a.*` after `USING` leaves out the join column, so the translation says `a.* EXCEPT (orderkey)`; and `date + INTERVAL n DAY` is kept as written on the Trino side while H2's `DATEADD` becomes `DATE_ADD`.
* The Spark pairing, the PostgreSQL and DuckDB counterparts and the JoinEquiv and jOOQ guards are ours. The PostgreSQL dropped-qual query is our reading of the commit message ("we may drop essential join quals"); the bug report itself is on a blocked host, and so are PG17982, PG17985 and DuckDB issues 20483 and 20486.
* Trino's labels assume no arithmetic overflow and the TPC-H keys; Trino's own test compares results on the tiny data only.
* Overlap: none of these pairs is in the [optimizer-bug pairs](optimizer-bugs.md) (R019); the closest are its NOT IN decorrelation cases (bug-003, bug-022), which are different queries. The DuckDB test file's queries are also run one at a time, through the rewrites, by the [DuckDB SQLLogicTest eval](engine-suites.md); here they are pairs.
