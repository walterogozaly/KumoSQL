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
