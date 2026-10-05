# Refutation strength

[Plain-language version](../../docs_simple/evals/refutation-strength.md)

How often does `prove_equivalent_algebraic(..., search_counterexample=True)` refute a pair of queries that is known to return different rows, with a database that anyone can replay? Every pair here differs on some database, so a **proof is wrong**; the score counts refutations. Results file: `refutation-strength`.

```
python tools/refutation_strength_bench.py --jobs 4                     # about two minutes
python tools/refutation_strength_bench.py --show unknown
python tools/refutation_strength_bench.py --only r024,optimizer-bugs   # sources or case ids
python tools/refutation_strength_bench.py --baseline                   # the search before the synthesizer
python tools/refutation_strength_bench.py --write-results
```

## What is scored

| Source | Pairs | Where they come from |
| --- | --- | --- |
| `r024` | 42 (41 refutable) | An outside research assistant's sweep (R024) of pairs confirmed to differ: correlated domain joins (R006), aggregation pushdown (R009), windows (R011), documented rewrites (R012b) and decarrelation (R002), each with its witness database. Pinned in `tests/fixtures/refutation_strength/r024.jsonl` ([sources and adaptation](../../tests/fixtures/refutation_strength/README.md)) |
| `optimizer-bugs` | 24 (23 refutable) | The pairs of the [optimizer wrong-result bugs](optimizer-bugs.md) eval, with its held-out split |
| `verieql` | 4 | VeriEQL's Literature 46 and 47 (which need more than 1,000 rows to differ) and Calcite 12 and 231, read from the VeriEQL download (CC BY-NC-SA 4.0, never stored here) |

Two more sources are reported beside the score, not in it:

* `targeted-escapes`: the 108 mutants of `tests/fixtures/targeted_data/cases.json` that slip past eight random databases but not the targeted suite ([targeted test data](targeted-test-data.md)). Two differ only where a divisor is zero, and BigQuery raises an error there, so no database shows the difference: they are not counted as refutable.
* `unsafe-controls`: the 340 non-equivalent pairs of `tests/fixtures/unsafe_rewrite_cases.jsonl` (the [unsafe-rewrite](fuzzing.md) eval).

A pair is **refuted** when the prover returns `not_equivalent` with a database, and the harness, replaying that database independently (`kumosql.refutation_replay.replay_counterexample`), finds the two queries return different bags. It is **proven** (wrong) when the prover proves it, and **unknown** otherwise. A refutation that does not replay also counts as wrong. Before anything is scored, each case's own witness is replayed (DuckDB with the optimizer off, `kumosql.duckdb_load.run_unoptimized`, or SQLite for the one pair DuckDB cannot run), so every pair is known to differ. Pairs whose difference rests on an arbitrary pick or an approximation (bug-025's `DISTINCT ON`, R012b-18's `APPROX_COUNT_DISTINCT`) are not counted as refutable.

## Scores

2026-10-05: **66/68 refuted, 0 proved, 0 wrong**, median 0.3 to 0.5 s per pair (0.29 s on an idle machine, 0.52 s with the machine shared). Every witness confirmed.

| Source | Refuted |
| --- | --- |
| `r024` | 41/41 |
| `optimizer-bugs` | 21/23 (held out: 4/5) |
| `verieql` | 4/4 |
| `targeted-escapes` (beside the score) | 106/106 |
| `unsafe-controls` (beside the score) | 340/340 |

Unknown: bug-001 (held out: the solver's counterexample does not separate the pair when run, so it is dropped and nothing else finds one in time) and bug-005 (runs only on SQLite; the search runs on DuckDB). Not counted as refutable: bug-025 and R012b-18, as above. Master's search alone (`--baseline`, which switches the synthesizer off with `KUMOSQL_SYNTHESIS=0`) refutes 9 of the 70 main pairs.

**Median time** is per pair for the 70 main pairs, prover time limit 10 s, counting the proof attempt before the search.

## How a refutation is built

`kumosql.refutation_synthesis.synthesize` runs three stages and a judge (`kumosql.refutation_replay.Judge`) confirms every candidate database before it is returned:

1. **search**: corner-case and targeted databases (`kumosql.targeted_data`), narrow databases with few values per column (respecting NOT NULL columns and keys), then random seeds;
2. **bounded**: the bounded checker (`kumosql.bounded_equivalence`) finds a small model, replayed by the judge;
3. **multiplicity**: `kumosql.refute_bounded` finds a model where rows carry copy counts, for pairs that differ only when a count passes a literal (Literature 46 and 47 need about 600 copies of two rows to pass `HAVING COUNT(*) > 1000`).

The judge says `differs` only when the declared NOT NULL columns, keys and foreign keys hold, both queries run, the result bags differ (also after rounding floats to six significant digits), the optimizer-off run agrees, and the difference survives reversed, rotated and shuffled row orders. A solver counterexample goes through the same judge first: one that returns the same rows on both queries when run is dropped (the pair becomes `not_proven`) and the synthesizer gets its turn. See [Equivalence provers](../provers.md).

## Held out and caveats

* Only optimizer-bugs' held-out pairs (bug-001, 003, 008, 021, 026) are held out. Their SQL was never read while building the refuter; bug-001's solver counterexample failing on replay is what led to replaying solver counterexamples, so it counts as **tuned on test**.
* Every R024 and VeriEQL pair, and every non-held-out optimizer-bugs pair, was seen while building the refuter, as were the escapes and controls. The score measures what the refuter does on pairs it was built around, not on new ones.
* Most of the pairs are small and chosen to differ on empty inputs, NULLs or duplicates, which the search is built to try.
* The VeriEQL pairs need the VeriEQL download. Without it (no network) the harness skips that source, the printed summary lists three sources and `--write-results` refuses to write; `tests/test_refutation_strength_eval.py` skips its VeriEQL check.
* A refutation is a claim about DuckDB's reading of the queries. For BigQuery SQL the judge runs them through [Running BigQuery SQL on DuckDB](../bigquery-on-duckdb.md) and gives no verdict where DuckDB would diverge. Finding no difference proves nothing.
* The 0-wrong claim covers the pairs here. It is not a proof that the refuter never contradicts a true equivalence.
