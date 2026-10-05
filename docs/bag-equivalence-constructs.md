# Constructs the bag-equivalence backend reads

The bag-equivalence backend (`src/kumosql/uexpr/`) turns a query into a function from tuples to multiplicities and compares two of them with Z3. This page lists the SQL constructs its translator reads beyond plain select, join, group and set operations, how each is read, and what each proof then assumes. A construct that is not listed, or that is listed with a condition it does not meet, makes the backend answer "not proven". It never refutes.

Each proof is also run on random databases by `tools/uexpr_bench.py`; the focused cases are in `tests/test_uexpr_constructs.py`.

| Construct | Reading | Stays unsupported |
| --- | --- | --- |
| `ROLLUP`, `CUBE`, `GROUPING SETS` | The `UNION ALL` of one plain `GROUP BY` per set, using the algebraic prover's rewrite (`grouping_sets.py`): a key missing from a set reads as NULL and `GROUPING(k, ..)` is the bit mask of the missing keys. `GROUPING(k)` over a plain `GROUP BY` on `k` is 0. | Repeated sets, non-column keys, more than 64 sets |
| `agg(x) FILTER (WHERE c)` | The aggregate over the rows where `c` is TRUE: `c` joins the membership condition of the aggregate's bag. | A filter that contains an aggregate or a subquery |
| `COUNT([DISTINCT] a, b, ..)` | Counts the rows (distinct tuples) where every argument is non-NULL. The tuple is a strict, uninterpreted function of its arguments, so a proof holds for the real, injective pairing. `COUNT(a, b)` used to be read as `COUNT(a)`. | `COUNT(*, x)` and nested aggregates |
| `FETCH NEXT n ROWS ONLY` | `LIMIT n`. | `PERCENT`, `WITH TIES` |
| `ORDER BY .. LIMIT` over `SELECT *` | The star is spelled out from the schema before the top-level limit is split off. | A table without a declared schema |
| `ORDER BY` on a parenthesized query | Ignored: the bag does not change. | |
| `LIMIT n` over a constant select list | Every row is the same tuple, so the limit keeps `min(n, count - m)` copies (`LIMIT 1` is an existence test). | |
| A `LIMIT` query inside the query | An opaque relation named by the query's text, table aliases renamed by position. The same text over the same database returns the same rows (`LIMIT_SOURCE_ASSUMPTION`). `LIMIT 1` also declares at most one row. | A query that reads an outer column or a CTE, a non-literal limit or offset |
| A top-level `LIMIT` | The existing splitter: both sides must cut alike (`TIE_ASSUMPTION` unless the ordering covers every column). | A top-level `LIMIT` without `ORDER BY` always picks arbitrary rows and is never proved |
| `SELECT k AS a FROM (SELECT * FROM t ORDER BY k LIMIT n) s` | The limit is pulled out: `SELECT k AS a FROM t ORDER BY k LIMIT n`. Tied rows may differ, but the key values they carry do not. | A projection of a column that is not an `ORDER BY` key |
| `JOIN LATERAL (subquery) alias` | The derived table is translated in a scope that sees the columns of the rows to its left. | `RIGHT`/`FULL` join, `LATERAL VIEW`, `OUTER APPLY` |
| A select with a window function | An opaque relation named by the select's text (`WINDOW_SOURCE_ASSUMPTION`); `NTH_VALUE(x, 1)` is spelled `FIRST_VALUE(x)` first. | A star list, a select that reads an outer column or a CTE, named windows |
| `(a JOIN b ON .. JOIN c ON ..)` as a join operand | Read as the join of its own operands first. | A parenthesized join with an alias |
| An int compared with a string (any two type families) | An uninterpreted strict function of the two values, one per operator. A proof holds for whatever the engine does, provided both queries compare the same operands. | |

## Why the opaque readings are sound

An opaque relation stands for the rows of a query the procedure cannot compute. The proof treats it as an arbitrary relation, so it holds for the real one. What it adds is the assumption that two spellings of the same text, over the same database, return the same rows: a `LIMIT` without a total order, and a window function with ties, depend on how the engine breaks ties. Both assumptions are listed in the proof's assumptions (the SMT prover lists the same ones). A query that reads an outer column is not one relation, since it changes with the outer row, and is refused.

## Where the backend still stops

The `GROUPING SETS` rewrite and `FILTER` are read now, but several Calcite pairs that use them with `COUNT(DISTINCT ..)` still need the aggregate decomposition of step 3 (a distinct count as a sum over the groups). They are reported as "no proof found", not as unsupported.

## Measured effect

`python tools/uexpr_bench.py calcite spark --trials 20` with 0 wrong: the SQLSolver Calcite suite went from 172 to 180 of 232 pairs proved (pairs 4, 5, 34, 68, 93, 98, 157, 223 are new) and the Spark suite from 104 to 114 of 123 (46, 52, 53, 56, 57, 118, 119, 121, 122, 126). TPC-H and TPC-C are unchanged. The must-not-prove pairs (Calcite 100, Spark 50, 60, 61) stay unproven.

The count of proved pairs can differ by a pair or two between runs: whether a proof is found depends on the numbering of the fresh variables, which depends on how many queries the process has already translated.
