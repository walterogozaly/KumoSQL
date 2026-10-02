# BigQuery behaviour eval (GoogleSQL compliance queries and edge cases)

Does a KumoSQL rewrite (inline single-use CTEs, trivial-predicate and parenthesis cleanup, CTE dedupe, unused-CTE removal, redundant DISTINCT, and optionally subquery lifting) keep the query's behaviour on BigQuery-specific syntax and semantics, or does it say so and decline?

```
python tools/bq_behavior_eval.py                      # both corpora, pipeline "semantic"
python tools/bq_behavior_eval.py --pipeline lift      # plus subquery lifting
python tools/bq_behavior_eval.py --corpus edge --failures --details
python -m pytest tests/test_bq_behavior_eval.py       # floors; the full GoogleSQL run is marked slow
```

Needs `duckdb` and `z3-solver` (the `dev` extra); neither is a runtime requirement.

## How a case is scored

The rewrite pipeline runs on the query. If the text only changes layout, or does not change, the case is **declined**. Otherwise both versions are executed in DuckDB (after sqlglot's BigQuery to DuckDB transpile) and the results compared as bags (as lists when there is an `ORDER BY`). DuckDB runs on one thread so the same query gives the same answer every time (with several threads it can build `ARRAY(SELECT .. UNION ALL ..)` in a different order on each run).

| Outcome | Meaning |
|---|---|
| handled | KumoSQL rewrote it, accepted the rewrite as proven, and the results match |
| declined | unchanged, layout only, or the rewrite was not accepted (unproven) |
| unsupported | sqlglot does not parse it (opaque command or parse error) |
| error | KumoSQL crashed |
| **WRONG** | KumoSQL accepted a rewrite whose results differ. This must stay at 0 |
| not executable | DuckDB cannot run the original, so there is no verdict |

Correctness (WRONG), coverage (handled / declined / unsupported / error) and performance (seconds) are reported separately. DuckDB is a differential oracle only: the same engine runs both sides, so an engine difference from BigQuery cannot hide or fake a change, except where the rewrite itself folds a constant. Those folds were checked on real BigQuery (project `kumosql`, zero bytes billed): `1 = 1.0`, `'a' = 'A'`, `9007199254740993 = 9007199254740992.0` (true in BigQuery, which is why KumoSQL only folds INT64 pairs and identical literal text), `NULL AND FALSE`, `NULL OR TRUE`.

## Corpora

- **GoogleSQL compliance queries** (`tests/fixtures/googlesql/queries.json.gz`): the SQL text of every self-contained `SELECT`/`WITH` case without parameters and not expected to error, from `googlesql/compliance/testdata/*.test` in [google/googlesql](https://github.com/google/googlesql) (formerly ZetaSQL), Apache-2.0, 7,870 queries. Only the SQL is used; the expected results are ignored, so nothing is fitted to them. Regenerate with `python tools/bq_behavior_eval.py --googlesql <testdata dir>`.
- **BigQuery edge cases** (`tests/fixtures/bq_edge/cases.json`): 967 queries over two small tables, all executable in DuckDB. By tag: three-valued logic and literal folding in predicates, parentheses and operator precedence (including BigQuery's `^`, `<<`, `||`), CTE inlining and dedupe (nondeterministic CTEs, shadowing, recursion, UNNEST, `SELECT * EXCEPT/REPLACE`), DISTINCT, QUALIFY and window ties, SAFE_ functions, casts and numeric boundaries, UNNEST/ARRAY/STRUCT, PIVOT/UNPIVOT, and 588 seeded fuzz cases (random predicate trees in WHERE/HAVING/ON/QUALIFY/CASE/IF/COUNTIF, random operator trees with redundant parentheses). Cases DuckDB cannot run are dropped.

## What this found and fixed

- **Subquery lifting dropped `PIVOT`, `UNPIVOT` and `TABLESAMPLE`** attached to the lifted subquery (`FROM (SELECT ...) UNPIVOT (...)` became `FROM cte`), changing the result's columns and rows.
- **The SMT prover called that pair, and any query with `PIVOT`/`UNPIVOT` against its source, equivalent.** It now declines any query containing a pivot.
- **`FLOAT` (32-bit in GoogleSQL) and `UUID`** were printed as `FLOAT64` and `STRING` by the BigQuery generator on both sides of the check, so a rewrite passed while changing the type. Such queries are now not accepted.

## Gaps and checks still wanted

- 1,356 GoogleSQL queries do not parse in sqlglot's BigQuery dialect (GoogleSQL-only features: protos, enums, graph queries, `FLOAT32`, newer pipe and table syntax). They are counted as unsupported, not hidden.
- The 81 pipe-syntax (`|>`) queries are left as written. sqlglot parses pipe syntax into nested CTEs, so the rewrites that used to count for 7 of them were of that translation, printed as standard SQL.
- Most GoogleSQL cases are declined because KumoSQL has nothing to rewrite in a bare `SELECT`; the handled count measures rewrites that happened and held.
- Still wanted on real BigQuery (to run on the work-laptop replica): every rewritten before/after pair from the edge suite (`--failures` lists none; use `evaluate()` for the pairs), especially the CTE-inlining cases with `RAND()`, `GENERATE_UUID()` and `CURRENT_*`, and `SAFE_`/cast cases whose result a DuckDB transpile may not model.

## Scores (2026-10-02)

| Corpus | Pipeline | Cases | Rewritten and identical | Wrong | Declined | Unsupported | Not executable |
|---|---|---:|---:|---:|---:|---:|---:|
| GoogleSQL compliance (googlesql @ d82db99, 7,870 original queries) | semantic | 7,870 | 31 | 0 | 6,264 | 1,356 | 219 |
| GoogleSQL compliance | lift | 7,870 | 253 | 0 | 5,662 | 1,356 | 599 |
| Edge cases (967 custom: 379 hand-written, 588 seeded fuzz) | semantic | 967 | 428 | 0 | 539 | 0 | 0 |
| Edge cases | lift | 967 | 434 | 0 | 533 | 0 | 0 |
| Held-out fuzz (662, seeds 101 and 103, not used while fixing) | semantic / lift | 662 | 421 | 0 | 241 | 0 | 0 |

Baseline before the fixes above: edge cases 1 wrong with lifting (UNPIVOT), GoogleSQL 3 wrong with lifting (FLOAT and UUID types); no wrong without lifting. Each found failure stays in the corpus as a regression case. A pipeline can score 0 wrong by changing nothing, so the rewritten count is reported beside it. `heldout.json` is for final measurement only: add new bug-hunting cases to `cases.json`.
