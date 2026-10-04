# Equivalent under conditions

The [fourth verdict](../conditional-equivalence.md) is `proven_conditionally`: the pair is equal on every database where a listed, minimal set of NOT NULL, unique-key and foreign-key facts holds. This page scores it on two corpora whose "equivalent" labels assume keys and NOT NULL rules their files drop or keep only in part, and on three hand-checked suites. The verdict is off by default, so no existing score moves (the Singh and VeriEQL pages are unchanged).

## What is counted

`tools/conditional_bench.py` runs `prove_equivalent_algebraic(..., conditional=True)` on each pair and reports four counts, kept apart:

| Count | Meaning |
| --- | --- |
| Proved outright | The prover needs no condition. Scored in the [Singh](singh-bedathur.md) and [VeriEQL](verieql.md) pages; left out of the scoreboard row here. |
| Proved under named conditions | `proven_conditionally` with a minimal set. |
| Refuted with every candidate holding | A database that meets **every** candidate condition still separates the queries, so no set from the catalog can make the pair equal. |
| Unknown | Anything else. |

Nothing reads the published labels while deciding; they are compared afterwards. A label is not ground truth for a conditional pair: the Singh files list no keys and no NOT NULL facts, so a pair labelled equivalent can differ on a NULL or a duplicate row, and a pair labelled different can be equal once NULLs are excluded.

Every conditional proof is checked three ways, and a failure of the first or the second makes the pair **wrong** (the count must stay 0):

1. The pair is re-run on 900 random DuckDB databases repaired to meet the conditions (NULLs filled in, duplicate keys dropped, orphaned foreign keys pointed at a parent or dropped). Both queries must return the same bag on all of them.
2. For VeriEQL, the executed counterexample search of `kumosql.counterexample` runs with the conditions added to the spec; it must find nothing.
3. The prover loses the proof when any one condition is dropped (reported as the minimal count). The share of pairs for which a random database that breaks the conditions separates the queries is reported too ("separated without the conditions"); agreement on sampled databases is evidence, never the ground truth.

## Singh and Bedathur LeetCode pairs

All 2,800 pairs, MySQL dialect, table and column names only (the [Singh page](singh-bedathur.md) has the data and its licence note). Measured 2026-10-03 on the branch merged with master (the Singh eval then proves 846 of these pairs outright).

| Count | Pairs |
| --- | ---: |
| Proved outright | 846 |
| **Proved under named conditions** | **610** |
| Refuted with every candidate holding | 357 |
| Unknown | 987 |
| Wrong | 0 |
| Harness crashes | 0 |

- The 610 conditional proofs use 1,189 conditions: 811 NOT NULL, 330 unique keys, 48 single-column foreign keys. One condition alone suffices for 205 pairs, two for 274, three for 99, and four to six for 32.
- All 610 were re-checked on 900 databases each (about 547,000 in total) with 0 differences, and each is minimal for the prover. For 553 of them a random database that breaks the conditions separates the queries; for the other 57 no separating database turned up in 300 tries, which does not mean there is none.
- Against the files' labels: 175 conditional pairs are labelled equivalent (the published answer assumed a key or NOT NULL rule the file does not state), 435 are labelled different (the labelled difference is a NULL or a duplicate; the pair is equal once it is excluded).
- Held-out fifth (hash of the pair divisible by 5, 580 pairs): **131 conditional, 0 wrong**, run once at the end; development used `--split dev` (479 of 2,220 conditional).

```bash
python tools/conditional_bench.py singh                  # all 2,800 pairs, about 45 minutes on 4 cores
python tools/conditional_bench.py singh --split dev      # development
python tools/conditional_bench.py singh --sample 120     # the test sample
```

## VeriEQL LeetCode

The same problems as VeriEQL ships them, with types, primary keys, NOT NULL columns and foreign keys already declared; a condition here is a fact **beyond** what the spec states. The run takes every eighth of the 23,994 cases (3,000), `--every 8`.

| Count | Cases |
| --- | ---: |
| Proved outright | 861 |
| **Proved under named conditions** | **316** |
| Refuted with every candidate holding | 0 |
| Unknown | 1,818 |
| Conditional but not re-checkable | 5 |
| Wrong | 0 |
| Harness crashes | 0 |

The 316 use 471 conditions (224 NOT NULL, 187 unique keys, 60 foreign keys). All 316 pass the 900-database re-check and the executed counterexample search with the conditions in the spec; 119 are separated by a database that breaks them. Five more conditional proofs rest on a composite foreign key that the executed check cannot impose, so they are not re-checked and not scored. Fewer conditionals per case than Singh is expected: the declared keys already carry what the prover can use. No held-out split is reserved for this sample. The harness was developed against Singh dev pairs and an early 80-case VeriEQL sample (tuned on test); the full sample was run afterwards.

```bash
python tools/conditional_bench.py verieql --every 8 --workers 4   # about 15 minutes on 4 cores
```

## Constraint-touched candidate pairs

A list of 655 benchmark pairs whose queries read a declared key, NOT NULL column or foreign key in a WHERE, join, GROUP BY, DISTINCT, IN or COUNT (VeriEQL Calcite and Literature, SQLSolver Calcite, WeTune Calcite, Cosette examples) was mined syntactically; no prover had run on it. `tools/conditional_candidates.py <candidates.json>` runs the verdict on the 508 pairs of VeriEQL Calcite (321), VeriEQL Literature (5 of 6) and SQLSolver Calcite (182), with the schema's tables, columns and types kept and **every declared constraint removed**, so a proof has to name the facts it needs. It compares those with what the schema declares (a declared key covers every superset of its columns) and re-checks each conditional proof on databases that meet only the returned conditions. WeTune Calcite (142 pairs, a catalog format with no loader here), the four Cosette pairs and one Literature pair whose text does not match the pinned file are not run. None of the three corpora has a held-out split of its own, and mined Calcite pairs and adapted Cosette pairs, which do, are not in the list; the harness was not tuned on these pairs.

| Corpus | Pairs | Proved with no condition | Proved under conditions | Unknown | Of the unknown, proved when the schema's constraints are given |
| --- | ---: | ---: | ---: | ---: | ---: |
| VeriEQL Calcite | 321 | 233 | 30 | 58 | 2 |
| VeriEQL Literature | 5 | 0 | 3 | 2 | 1 |
| SQLSolver Calcite | 182 | 153 | 16 | 13 | 2 |

0 wrong and 0 crashes. All 49 conditional proofs were re-checked on databases that meet only their conditions, and each is minimal for the prover. What the run shows:

- **Touching a constraint is not depending on it.** 386 of the 508 pairs are proved with no constraint at all.
- **49 pairs need conditions** (85 conditions: 46 unique keys, 39 NOT NULL, no foreign key; one condition for 25 pairs, two for 14, three for 8, four for 2). 35 use only facts the schema declares (a primary key gives unique and NOT NULL, and a declared key makes every superset of its columns unique). 14 use a fact the schema does not state. Three read by hand: `WHERE DEPTNO IN (SELECT DEPTNO FROM DEPT)` against an inner join needs `DEPT.DEPTNO` unique, and VeriEQL's schema gives DEPT no key; a self join of `R2` on its key with `Y.B = Z.B` dropped needs `R2.b` NOT NULL (the two joined rows are one row, and `b = b` still rejects it when `b` is NULL); `(empno, deptno) IN (...)` against a LEFT JOIN needs NOT NULL on both columns and a unique pair, which the declared key on `empno` provides.
- **11 of the 49 are not shown to be semantically needed.** A database without the conditions separated the queries for 38; for these 11 none turned up in 300 tries, so the prover needs the conditions and the queries may not. Examples: the Calcite `INTERSECT` of `DEPTNO = 10`, `20` and `30` (a row cannot have two `DEPTNO` values, so the left side is empty on every database), and one pair (`MGR IS NULL` under a returned `NOT NULL mgr`) whose conditions empty both sides; its query already compares `HIREDATE = CURRENT_TIMESTAMP`, which the constant check treats as a query written to be empty, so the verdict stands.
- **5 pairs are proved with the schema's constraints but not conditionally** (2 VeriEQL Calcite, 1 Literature, 2 SQLSolver Calcite): the catalog, at most 40 single-column candidates, leaves out what they need.

```bash
python tools/conditional_candidates.py <candidates.json> --json results.json   # about 2 minutes on 4 cores
```

## Hand-checked suites

| Suite | Cases | What it checks |
| --- | ---: | --- |
| `tests/test_conditional_suite.py` | 17 cases, 5 controls, 4 decoys | Each case has an answer worked out by hand with and without its conditions: DISTINCT on a unique id, LEFT JOIN removal on a unique right key, INNER JOIN removal with a non-null foreign key to a unique parent, composite keys, NULLs, bag semantics. A database that breaks a condition separates the queries on DuckDB; random databases that meet the conditions never do; dropping any condition loses the proof. Controls stay unproven under every candidate, a set no database satisfies is refused, and a set that empties the query is passed over. |
| `tests/test_conditional_s001.py` | 29 pairs from an outside source | Every counterexample, deletion witness and sufficiency example is replayed on DuckDB; the verdict of both provers is pinned. 13 pairs get a conditional verdict from the algebraic prover, 11 from the SMT prover (the SMT prover has no use for foreign keys and no outer-join aggregates). Pairs whose expected conditions are outside the catalog (filtered keys, CHECK, FD, EXISTS) must stay unproven or refuted. |
| `tests/test_conditional_vendor.py` | 2 pairs from Databricks' documentation on `RELY` constraints | DISTINCT dropped on a key (needs unique **and** NOT NULL: repeated NULLs collapse under DISTINCT) and an unused LEFT JOIN dropped on a unique key (no NOT NULL needed). |

The outside cases are data in `tests/fixtures/conditional/` with a source note per case ([README](../../tests/fixtures/conditional/README.md)). Where a pinned set is stronger than the minimal set the file expects (a plain unique key where the file asks for a key that holds only on a filtered subset), the prover is sound but incomplete, never wrong.

## Limits

- Minimal means minimal for the prover: every single condition, the last one included, was tried against the final set and dropping it loses the proof. That is not a proof the condition is necessary (a "not proven" after a deletion is the prover giving up), and the search returns one sufficient set, not every alternative. Two different minimal sets can exist; the search keeps NOT NULL conditions longest and leaves unique keys out first.
- The catalog is NOT NULL, unique keys and single-column foreign keys read off the queries, at most 40 candidates. Non-empty tables, CHECK and value ranges, filtered keys and functional dependencies are outside it; pairs that need them stay unproven or refuted.
- A condition is data the owner must make true: the verdict carries a SQL check for each, and BigQuery does not enforce keys.
- Results: [`conditional-equivalence-singh`](../../benchmarks/results/conditional-equivalence-singh.json), [`conditional-equivalence-verieql`](../../benchmarks/results/conditional-equivalence-verieql.json).
