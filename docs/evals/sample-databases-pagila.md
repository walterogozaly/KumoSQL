# Sample databases: Pagila

Pagila, PostgreSQL's DVD-rental sample database, loaded whole into DuckDB from its pinned upstream scripts, with the same two scores as the other [sample databases](sample-databases.md): KumoSQL's rewrites on its workload, checked on the real data, and query pairs on its schema run through the provers. It is the third adapter of `tools/sample_db_bench.py`; Chinook and Northwind keep their own results files and numbers, and Pagila has its own.

| Database | Upstream (pinned) | Licence | Loaded | Declared keys |
| --- | --- | --- | --- | --- |
| Pagila 4.1.1 | [devrimgunduz/pagila](https://github.com/devrimgunduz/pagila) `9baf49c4149e`, `pagila-schema.sql` (SHA-256 `071ee940a73c`) and `pagila-data.sql` (SHA-256 `a88efa94c7ae`) | PostgreSQL licence, Copyright (c) Devrim Gündüz | 16 tables, 122,209 rows | 16 primary, 19 foreign keys, 78 NOT NULL columns |

| Score | Results file |
| --- | --- |
| 65 workload queries through every rewrite: **0 wrong in 436 executed cases, 192 rewrites verified on the real data** | `sample-databases-pagila-rewrites` |
| 60 authored pairs: **22/27 equivalent proved, 31/33 different refuted (every refutation replayed), 0 wrong** | `sample-databases-pagila-pairs` |

```
python tools/sample_db_bench.py --check --database pagila      # load and check against upstream (10 seconds)
python tools/sample_db_bench.py --database pagila --part pairs  # (b) only, about 30 seconds
python tools/sample_db_bench.py --database pagila --write-results   # both parts and both results files, about 5 minutes
```

The results are kept apart from the first two databases on purpose: a database that changes the numbers of rows already on the scoreboard would hide whether a rule or prover change moved them. `Adapter.results_group` (here `sample-databases-pagila`) names the pair of results files, `results_order` the scoreboard position and `docs_page` this page; Chinook and Northwind keep the default group.

## Data and adaptation

The two upstream files are committed unchanged with the licence in [`tests/fixtures/sample_databases/pagila`](../../tests/fixtures/sample_databases/README.md). The data file is 13 MB: the licence allows it, and the harness reads the `COPY` blocks of the dump (`read_copy`), so no row is ever copied by hand. `pagila-insert-data.sql` (19 MB, the same data as `INSERT`s) and the `.backup` files are not used.

`adapted/schema.sql` is BigQuery DDL and says so in its first line. What changes:

- Types: `integer` and `smallint` become `INT64`, `text` `STRING`, `boolean` `BOOL`, `date` `DATE`, `numeric(p, s)` `NUMERIC(p, s)`, `bytea` `BYTES`. `timestamp with time zone` becomes `DATETIME` holding the UTC time (every upstream value ends in `+00`; the loader refuses any other offset).
- Types with no BigQuery counterpart keep the dump's text form in a `STRING`: the `mpaa_rating` enum, the `year` domain (an `INT64`, its 1901 to 2155 check dropped), `uuid`, the `text[]` column `special_features` (`{Commentaries,"Deleted Scenes"}`), the `tsvector` column `fulltext` and the pgvector column `film_embedding.embedding` (`[1,0,0.5,...]`).
- `payment` is a partitioned table: 55 monthly partitions, January 2022 to July 2026, each with its own `COPY` block. They are read into the one adapted table `payment`.
- `film.length_hours` is a `VIRTUAL` generated column (`round(length / 60.0, 2)`), so the dump does not store it. The adapter computes it from that expression (half away from zero, like PostgreSQL's `numeric`).
- Primary and foreign keys are declared `NOT ENFORCED`. Dropped: defaults, sequences, triggers, `ON UPDATE` and `ON DELETE` actions, the indexes (BigQuery has no `UNIQUE` constraint, so the five unique indexes on tables, such as `store(manager_staff_id)` and `rental(rental_date, inventory_id, customer_id)`, are not declared keys), the vector extension and the functions.

Every run checks the load against upstream, and the eval stops on any difference:

- the SHA-256 of each upstream file;
- the same tables, columns (in order), NOT NULL columns, primary keys and foreign keys as upstream's DDL, with the 55 partitions folded into `payment` (they must all declare `payment`'s columns, and the foreign keys the table is credited with are the ones every partition declares);
- each table's row count equal to the rows the dump inserts, and `customer` equal to the 999 the README gives (4.0.0 history), `rental` and `payment` within the README's "about 51.8k" and "about 51k";
- every declared key unique, every NOT NULL column without NULLs and every declared foreign key satisfied by the real rows;
- 21 more assertions taken from the pinned release: the README's date range (January 2022 to July 2026), no padding left in `language.name` and no run of customers named "ELIZABETH HALL" (both 4.0.0 fixes), `length_hours` present exactly when `length` is, every id within the sequence value the dump ends with (`setval`), and the three `payment` foreign keys satisfied by every payment.

### What the pinned release does

- **`payment` declares no foreign key.** Upstream declares `payment`'s three foreign keys (customer, rental, staff) on the first six partitions (January to June 2022) and on none of the other 49. The table as a whole guarantees nothing, so the adapted `payment` has only its primary key, the composite `(payment_date, payment_id)`. The data satisfies all three keys anyway (checked). The pairs use the difference: a join from `payment` to `customer` is not removable under the declarations, although it is on the data. This looks like an upstream oversight, not a choice.
- **`payment_id` alone is not a key**, only the composite `(payment_date, payment_id)` is (PostgreSQL needs the partition column in a key). No two payments share an id in the data, but the declarations do not say so.
- **`film.original_language_id` is NULL in every film**, so a join on it returns nothing and `NOT IN` over it returns nothing.
- The data is synthetic and uneven: 500 stores but customers, inventory and rentals in only two of them, 1,500 staff members spread over 475 stores, so `sales_by_store` has two rows.

## Workload (a): every rewrite, checked on the real data

| Origin | Queries | What it is |
| --- | ---: | --- |
| `upstream-view` | 8 | the 8 `CREATE VIEW` bodies of `pagila-schema.sql` (`actor_info`, `customer_list`, `film_list`, `nicer_but_slower_film_list`, `sales_by_film_category`, `sales_by_store`, `staff_list` and the materialized view `rental_by_category`, created `WITH NO DATA` upstream), adapted to BigQuery SQL. The harness checks each names a view of the upstream script |
| `upstream-procedure` | 9 | the `SELECT`s of the upstream functions with their parameters bound: `film_in_stock`, `film_not_in_stock`, two of the three `SELECT`s of `get_customer_balance`, `inventory_held_by_customer`, both of `inventory_in_stock`, and the dynamic SQL of `rewards_report` (alone and joined to `customer` as the function does) |
| `upstream-readme` | 4 | the README's portable example queries: late rentals, the film embedding lookup, `length_hours`, the three newest customers by `uuid`. The JSON, pgvector-operator, full-text and temporal examples have no BigQuery form |
| `authored` | 44 | written for this eval: inner, left and self joins, nullable foreign keys, aggregation and `HAVING`, `DISTINCT` on single and composite keys, `IN`, `EXISTS`, `NOT EXISTS`, `NOT IN` with NULLs, scalar and correlated subqueries, set operations, window functions, CTEs with an unused one, trivial predicates, `DATE` and `DATETIME` functions, `LIKE`, and joins on `payment` |

Each adaptation is recorded with the query in `workload.json` (`adaptation`). The ones that change meaning: `jsonb_object_agg` has no BigQuery form, so `actor_info` returns a JSON array of `{category, titles}` built with `TO_JSON_STRING(ARRAY_AGG(STRUCT(...)))`; the aggregate `group_concat` becomes `STRING_AGG(..., ', ' ORDER BY first_name, last_name, actor_id)` (upstream's order is unspecified, and an unordered string would differ between a query and its rewrite for no real reason); the call `inventory_in_stock(inventory_id)` in `film_in_stock` is inlined as "no rental of the item has a NULL `return_date`", which is what its two branches reduce to; `LIMIT 5` is dropped from the late-rentals query because many rentals share a title. The `get_customer_balance` late-fee `SELECT` (PostgreSQL interval arithmetic) and `last_day` are not adapted; the other functions only update rows or return triggers.

Each query goes through the same stages as the other sample databases (see [the first page](sample-databases.md#workload-a-every-rewrite-checked-on-the-real-data)): the canonical rule pipeline on the query and four variants, `lift_subqueries`, and the proof-gated optimizer with the declared keys. A rewrite is wrong when its result multiset differs from the unrewritten control (rerun with DuckDB's optimizer off before it counts) or when it no longer runs.

| Origin | Cases | Executed | Changed and verified | No change | Unsupported | Wrong |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| upstream views | 56 | 56 | 24 | 32 | 0 | 0 |
| upstream procedures | 63 | 63 | 28 | 35 | 0 | 0 |
| upstream README | 16 | 14 | 6 | 8 | 2 | 0 |
| authored | 303 | 303 | 134 | 169 | 0 | 0 |
| **all** | **438** | **436** | **192** | **244** | **2** | **0** |

By stage: the pipeline changed 185 of 310 executed cases, `lift_subqueries` 3 of 63 (the derived tables of `pg-a23` and `pg-a28`, and `rewards_report`), the optimizer 4 of 63. The optimizer's changes are the ones the declared keys license: it dropped a `DISTINCT` on `film_actor`'s composite key (`pg-a07`), a `LEFT JOIN` to `address` on its key (`pg-a32`), an unused CTE and inlined single-use ones (`pg-a22`), and a trivial predicate with a projection merge (`pg-a23`). Every change returned the same rows as the control. Held out (10 queries, by SHA-1 of `pagila:<query id>`): 0 wrong, 32 changed of 70 executed. No query errored.

The 2 unsupported cases are the plain-pipeline runs of the two README queries that read `CURRENT_DATE` and `uuid`: the engine-suites guard treats them as non-deterministic or environment-reading and does not compare them. Their wrapped variants run and match.

## Pairs (b): the provers on the declared keys

60 authored pairs, each labelled `equivalent` or `different` under the declared keys, in `pairs.json` with the reason. 27 equivalent pairs are rewrites that hold on this schema: join elimination through a NOT NULL foreign key (one and two hops, to a single and to a composite key, one to one), a nullable foreign key join read as `IS NOT NULL`, `LEFT JOIN` elimination on a key, `DISTINCT` removal on a single, composite and `payment`'s composite key, `COUNT` of a NOT NULL column, `COUNT(DISTINCT key)`, a dependent `GROUP BY` column, `HAVING` on the group key, filter pushdown through `GROUP BY` and a window partition, `IN` to `EXISTS` and to a join on a key, `NOT IN` to `NOT EXISTS` on a NOT NULL column, outer to inner join under a null-rejecting filter, `INTERSECT` to `IN`, `IS NULL`, `COALESCE` and `NOT (a = b)` on NOT NULL columns. 33 are negative siblings: the same rewrite where it does not hold, 10 of them the equivalent pair itself with one guarantee removed from the declarations (`drop`: a foreign key, a primary key or a NOT NULL).

The key-dependent siblings that are particular to this schema: `payment_id` alone is not `payment`'s key, so `SELECT DISTINCT payment_id, amount` and `COUNT(DISTINCT payment_id)` are not provably the same as the plain forms although they agree on the data; `payment` has no foreign key, so its join to `customer` stays; the composite key of `film_actor` makes `DISTINCT actor_id, film_id` redundant while `DISTINCT film_id` is not; `original_language_id` is nullable and entirely NULL in the data.

Every `different` label has its own evidence, independent of the provers: the pair returns different rows on the real data (`witness: "real"`, 14 pairs), or a small witness database separates it (19 pairs), completed with legal values and checked against the declarations that remain. An `equivalent` pair that differs on the real data would be reported as a label error; none does. Provers, in order: the structural prover, the algebraic/SMT prover with the declared constraints and its executed counterexample search, then the bounded checker (at most 2 rows per table) for a counterexample only. Every proof is checked on the real database. Every counterexample is completed with legal values, must satisfy the pair's declarations, and must separate the pair when replayed in DuckDB. All 22 proofs came from the algebraic prover; the 31 refutations are 12 algebraic and 19 bounded counterexamples.

| Category | Pairs | Equivalent proved | Different refuted |
| --- | ---: | ---: | ---: |
| join elimination | 17 | 6/7 | 10/10 |
| aggregation | 12 | 5/6 | 5/6 |
| DISTINCT removal | 9 | 3/4 | 4/5 |
| NULL semantics | 9 | 4/4 | 5/5 |
| set operation | 5 | 1/2 | 3/3 |
| subquery | 4 | 2/2 | 2/2 |
| outer join | 2 | 1/1 | 1/1 |
| window | 2 | 0/1 | 1/1 |
| **all** | **60** | **22/27** | **31/33** |

0 wrong; all 10 siblings that drop a guarantee are refuted by a replayed database that keeps every other guarantee. Held out (8 pairs, by SHA-1 of `pagila:<pair id>`): 2/2 proved, 6/6 refuted, 0 wrong.

Unknown (7):

- not proved (5): the nullable foreign key join read as `WHERE fk IS NOT NULL` and a `DISTINCT` after a join to a key ("no row-preserving mapping"), a `LEFT JOIN` count against the correlated `COUNT(*)` subquery (the algebraic prover does not support that subquery shape), the `UNION ALL` of two complementary filters ("UNION shapes differ"), and the window filter on the partition column. All five are true equivalences the prover cannot show; nothing wrong is claimed.
- not refuted (2): `pg-distinct-payment-composite-key-id-alone` and `pg-count-distinct-key-payment-id`, the two pairs that hit the bounded-checker bug below.

## Bugs found

- **Bounded checker: DATETIME counterexample values collide.** `bounded_equivalence._model_value` clamps a DATE or DATETIME model value to the first representable day (`max(86400, ...)` seconds, `max(1, ...)` for a date) when it extracts the counterexample. Two rows whose model dates differ but are both small come out equal, so the counterexample can violate a key that includes the date. `payment`'s key is `(payment_date, payment_id)`, and the two pairs about `payment_id` alone get a "counterexample" with two payments of the same id and the same date `0001-01-01`. The replay gate catches it ("payment repeats a key"): the pair stays unknown, is listed as a prover bug in the run output and results file (`known_prover_bug` in `pairs.json`), and is not counted wrong, because no wrong answer is given. The prover module was not changed. A fix would extract the model value without clamping, or keep distinct values distinct. The unannotated version of the same event would count wrong.
- **Harness completion, not a prover bug.** The algebraic prover's counterexample for `pg-nullable-fk-join-as-filter-without-fk` is `film = {film_id: 0, original_language_id: 0}` with no language rows. Completed with the type default 0 for the NOT NULL `language_id`, the parent language 0 coincided with the original language 0 and the two queries agreed. `Adapter.fresh_foreign_key_values` (on for Pagila only) completes such a column with a fresh value and its parent row instead; the pair is then refuted. Chinook and Northwind complete as before.

## Baseline, held-out cases and limits

- **Baseline.** The first full runs, before any change, are the scores above except the pairs: 22 proved and 30 refuted, with 3 counterexamples that did not replay (the harness completion and the two `payment` pairs). Fixing the harness completion for this adapter and annotating the bounded-checker bug moved refuted from 30 to 31 and wrong from 3 to 0; no rule or prover was changed, and the workload and pairs were not edited after the first run. The rewrites row is the first run.
- **Held out.** A fifth of the queries and a fifth of the pairs, by SHA-1 of `pagila:<id>`, reported apart in both results files. They were seen in the printed output of the first runs (0 wrong on all of them); nothing was tuned on them. The 3 first-run failures were all dev pairs.
- **Authored.** The pairs and 44 of the 65 workload queries were written for this eval; only the views, function bodies and README queries come from upstream, and each is adapted as recorded.
- **Not used.** `scripts/add_monthly_data.sql` and `pg_partman-setup.sql` (data generation and partition maintenance, mostly `INSERT ... SELECT` with `random()`), `pagila-temporal-setup.sql` (PostgreSQL 19 temporal syntax), `pagila-schema-jsonb.sql` (a separate JSONB example) and the `.backup` files.
- **DuckDB stands in for BigQuery.** Queries are BigQuery SQL transpiled to DuckDB by sqlglot, and a rewrite is checked against a control that went through the same translation. Where the two engines differ (for example `EXTRACT(DAYOFWEEK ...)` numbers Sunday 0 in DuckDB and 1 in BigQuery) the comparison still holds, because both sides run on DuckDB.
- **Overlap.** No other eval in this repository uses Pagila. Sakila, the MySQL database it was ported from, is a separate database with its own adapter.
- **Undeclared uniqueness.** The unique indexes are not declared, so a pair that needs them (for example that a store's manager is unique) is out of reach for the provers here; none is scored.
