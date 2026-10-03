# Join rewrites to LEFT JOIN

Can the prover show that a query written with one join type equals one written with a LEFT JOIN? This eval is a hand-checked set of such pairs. Each rewrites a CROSS, comma, INNER, RIGHT or FULL join, or a semi or anti join, into or out of a LEFT JOIN. Results file: `benchmarks/results/join-rewrites.json`.

## What proves

These rewrites prove with no declared facts:

- **CROSS, comma and INNER JOIN.** These are one join: `a, b WHERE c` is `a JOIN b ON c`.
- **INNER to LEFT JOIN.** The LEFT JOIN must drop its null-extended rows. Any of these does it:
  - a WHERE test on the right side that is never TRUE on NULL (`b.y > 0`, `b.k IS NOT NULL` on a joined column, `IN (list)` or `IN (subquery)`, a correlated `EXISTS` whose WHERE needs the row, strict arithmetic, `MOD`, `CONCAT`);
  - a later inner join whose ON reads the right side;
  - a later outer join whose rows are rejected and can only match through it.
- **CROSS to LEFT JOIN ON TRUE.** This proves only when the right side can't be empty:
  - an aggregate without GROUP BY, which always returns one row;
  - a filter on a column that is never NULL in a real row.
- **RIGHT JOIN.** `a RIGHT JOIN b` is `b LEFT JOIN a`, including `a JOIN b RIGHT JOIN c`, which is `c LEFT JOIN (a JOIN b)`. A filter on `a` makes it inner.
- **FULL JOIN.** A filter that rejects NULLs on one side leaves a LEFT JOIN. Filters on both sides leave an inner join. A later inner join that reads one side counts as such a filter.
- **Anti join.** `a LEFT JOIN b ON a.k = b.k WHERE b.k IS NULL` is `NOT EXISTS`. It also proves with extra ON tests, which move into the subquery. It is `NOT IN` when both columns are NOT NULL.
- **Semi join.** `SELECT DISTINCT ... LEFT JOIN ... WHERE b.k IS NOT NULL` is `EXISTS` or `IN`. Without DISTINCT, it proves when the joined column is a key.
- **Aggregates.** A grouped LEFT JOIN is inner when two things hold:
  - every aggregate skips null-extended rows (`COUNT(b.k)`, `SUM(b.y)`, `MAX(b.y)`, `COUNT(DISTINCT b.y)`);
  - HAVING drops a group with no match (`COUNT(b.k) > 0`, `SUM(b.y) > 10`).

These need a declared fact:

- **Foreign key.** `a LEFT JOIN b ON a.k = b.id` is inner when `a.k` is NOT NULL and references `b.id`. The `a` rows must not come from an earlier outer join.
- **NOT NULL column.** `WHERE b.id IS NOT NULL` keeps exactly the matched rows when `b.id` is NOT NULL. `b.id IS NULL` then marks the anti join.

The prover does not pick these facts for you. Declare them in `constraints`. The "equivalent under conditions" verdict, which names the facts a rewrite would need, is separate work.

## What does not prove

Each of these pairs is refuted with a counterexample:

- `CROSS JOIN b` against `LEFT JOIN b ON TRUE` when `b` can be empty.
- `JOIN` against `LEFT JOIN` with no rejecting filter.
- A filter moved between ON and WHERE.
- `COALESCE`, `IS NULL`, `NOT IN`, `NOT EXISTS` or `OR` with a preserved-side test as the only filter.
- `COUNT(*)`, or aggregates of the preserved side, under the HAVING.
- A foreign key that is nullable, points the other way, or comes through an earlier outer join.
- `NOT IN` against an anti join over a nullable column.
- A semi join without DISTINCT or a key.
- A nested join group rewritten as a flat chain.

## Data and scoring

The pairs are in `tests/fixtures/join_rewrites/`: 87 development pairs (`pairs.jsonl`) and 16 held-out pairs (`held_out.jsonl`). They were written for this eval, not adapted from a public benchmark. All use tables `a(id, k, x)`, `b(id, k, y)` and `c(id, k, z)`. Each pair names its declared facts, and `why` says why the label holds. A non-equivalent pair also carries the smallest database found on which the two queries differ.

Every label was checked on 400 to 600 random DuckDB databases with at most four rows per table. A difference counts only when DuckDB also shows it with its optimizer off.

`python tools/join_rewrite_bench.py` runs the prover on each pair, with the executed counterexample search on. It then checks three things:

- a proof holds on 100 random databases;
- a refutation never hits an equivalent pair;
- each stored counterexample still separates its pair.

`--held-out` runs the held-out pairs. `tests/test_join_rewrite_bench.py` keeps the floors.

## Results

| Split | Proved | Refuted | Unknown | Wrong |
| --- | ---: | ---: | ---: | ---: |
| Development (87 pairs) | 50/50 | 37/37 | 0 | 0 |
| Held out (16 pairs) | 10/10 | 6/6 | 0 | 0 |

Before the rules added with this eval, master proved 36 of the 50 equivalent development pairs. The 14 gaps were:

- the foreign-key LEFT JOIN (three pairs);
- HAVING over a LEFT JOIN (six);
- `IN (subquery)`, correlated `EXISTS` and `CONCAT` filters (three);
- nested join groups, including the mirrored three-way RIGHT JOIN (two).

The development pairs drove these rules, so the development score is tuned on test. The held-out pairs were written and checked on DuckDB before the prover saw them, then run once.
