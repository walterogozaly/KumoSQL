# Window equivalence (windows and ties)

[Plain-language version](../../docs_simple/evals/window-equivalence.md)

Query pairs around window functions, scored on whether the provers prove the equivalent ones, refute the non-equivalent ones, and never prove a pair that only ties tell apart. It is the eval of issue #503 ("Window functions and ties"): the first run, taken before any window rule of that workstream, is the baseline, and the rule changes rebase these numbers. Results file: `window-equivalence`. The tie analysis it leans on is described in [Ties and nondeterministic results](../ties.md).

```
python tools/window_equivalence_bench.py                       # every case (about 40 minutes of CPU)
python tools/window_equivalence_bench.py --split dev --only idiom,derived
python tools/window_equivalence_bench.py --check-labels        # execute the label evidence of every case
python tools/window_equivalence_bench.py --write-results
python tools/window_equivalence_bench.py --make-slt-cases      # re-mine the sqllogictest pairs
python tools/window_equivalence_bench.py --make-corpus-index
```

## Semantics and labels

A query that can return different rows on the same data (ties in a window's `ORDER BY`, a `LIMIT` on a non-unique order, `ANY_VALUE`, `ARRAY_AGG` order) has a *set* of possible results. Two queries are `equivalent` when those sets are equal on every database the declared keys and NOT NULL columns allow, and `not_equivalent` when some database separates them (one side's set differs from the other's, a proper subset included). `tie_dependent` marks a pair whose answer turns on a tie: a non-equivalent pair that agrees on every tie-free database (`ROW_NUMBER() = 1` against `RANK() = 1`, a `ROWS` frame against the default `RANGE` frame), or an equivalent pair whose two sides are both nondeterministic yet pick from the same results (`QUALIFY ROW_NUMBER() = 1` against the same window in a derived table, `MAX_BY(value, ts)` against `ROW_NUMBER() = 1` over `ORDER BY ts DESC`). A refuter that calls such an equivalent pair different because DuckDB broke the tie differently is wrong.

The fixtures are in `benchmarks/window_equivalence/fixtures.json`: `events(id, user_id, ts, value)`, all INT64, with nothing declared (`events`), with every column NOT NULL and `id` unique (`events_key`, so a total order exists), additionally with `(user_id, ts)` unique (`events_uts`, so `ORDER BY ts` within a user never ties), and with only `id` declared (`events_null`, to exercise NULL ordering and null-safe keys).

## Sources

| Source | Cases | Where the pairs come from |
| --- | --- | --- |
| `derived` | 15 | The hand-written window pairs the workstream started from, re-derived: QUALIFY against a derived table, a filter pushed through a window (on the partition column and not), a windowed `SUM` against a correlated subquery and against a join to the grouped `SUM` (null-safe and not), the "max per group" forms (`ROW_NUMBER`, `RANK`, `NOT EXISTS`, join to the grouped minimum), `ROWS` against `RANGE`, `LAG` over tied rows, `RANK` against `DENSE_RANK`. 7 are equivalent, 8 are not. |
| `idiom` | 62 | BigQuery idioms, each with a tie trap: the latest row per key (`ROW_NUMBER`, `RANK`, join to `MAX`, `MAX_BY`, `ARRAY_AGG .. LIMIT 1`, NULL order, filters before and after the window), sessionisation (a `LAG` gap flag and a running `SUM`), running totals and neighbours (`ROWS` against `RANGE`, self-joins, `LAG`/`LEAD`, `FIRST_VALUE`/`LAST_VALUE`, `RANK` against `DENSE_RANK`, an unused window), gaps and islands (`ts - ROW_NUMBER()` against `DENSE_RANK()`, `LEAD` against `LAG`). Authored for this repository; each case's `why` says why it holds. |
| `slt` | 81 | Window queries of the DuckDB sqllogictest suite (MIT, copyright Stichting DuckDB Foundation) read through `tools/engine_suites.py`, from 22 test files under `window/`, `aggregate/qualify/`, `optimizer/topn_window_*` and `subquery/`: the query and the rows of the tables it reads are copied into `slt_cases.jsonl` with the file, line and revision. Pairs per query: the query against the output of KumoSQL's canonical rewrite pipeline as it is on this checkout (`slt-rule`, only when the pipeline changes the query and the result agrees on the data), against itself wrapped in a subquery or behind an unused CTE (`slt-wrap`, equivalent by construction), and against a copy with one window clause changed (`slt-mutant`: sort direction, frame, `ROW_NUMBER` for `RANK`, `LAG` for `LEAD`, a dropped `PARTITION BY`; kept only when the query returns the same rows in every row order and the mutant returns other rows on the source's data). The SQLite sqllogictest corpus holds no window query. |
| `corpus` | 92 | Window pairs of development splits only: VeriEQL LeetCode (every 24th case, the sample the VeriEQL eval developed on: 76 pairs with `OVER`), every VeriEQL Calcite-397 pair with `OVER` (15), and SQLSolver Spark pair 50 (`LIMIT 5` without `ORDER BY` over `ROW_NUMBER()`, listed in `tests/fixtures/sqlsolver/not_provable.json` as a pair that must not be proved). The VeriEQL files are CC BY-NC-SA 4.0 and are not copied: `corpus.json` keeps ids and a checksum of each pair, and the pairs are read from the cache `tools/verieql_bench.py` fills. VeriEQL's files carry no labels and its own run cannot read a window (all 91 are `NSE` or `NIE` in its published outcomes), so these pairs are `unlabelled` and only Spark pair 50 is `not_equivalent`. |

The held-out quarter is a stable hash of the case id (`sha256("window-equivalence\n" + id) % 4 == 0`), not a position, so adding a case moves no other. Develop on `dev` (`--split dev`); a rule author who looks at a held-out case records it as "tuned on test".

## How a case is scored

Each pair gets one outcome without its label, by the same chain as [Paired engine tests](engine-paired-tests.md): `prove_equivalent` (structural) or `prove_equivalent_algebraic` with the fixture's types, keys and NOT NULL columns gives **proven**; the algebraic prover's counterexample search or the targeted refuter (`kumosql.refute`) gives **refuted** only when its database keeps the declared facts and separates the two queries in DuckDB (one thread, some row order of the database, optimizer off agrees); anything else is **unknown**. VeriEQL corpus pairs use the VeriEQL harness verdict (`tools/verieql_bench.py`: an executed counterexample, or a proof that survives 1,000 more random databases).

**Wrong** is a proof of a pair not labelled `equivalent`, or a claim (replayed or not) that an `equivalent` pair differs; for an unlabelled VeriEQL pair, a proof the harness's own executed search then contradicts. Unknown beats wrong.

## Label evidence

Labels of `derived`, `idiom` and `slt` cases are checked by execution, not only by reasoning (`--check-labels`, and a test per case). `possible_results` stores the table in every row order (a sample of 720 above six rows) on DuckDB with one thread, which breaks ties in windows and `LIMIT` by storage order, and collects the result bags. An `equivalent` case needs equal sets on every random database that respects the fixture (four rows, three distinct values, so rows tie) and on its source's rows; a `not_equivalent` case carries a `witness` database on which the two sets differ (found by search, at most five rows). Two idiom cases use `ARRAY_AGG .. LIMIT`, which DuckDB cannot run: they are labelled by reasoning (the case says so) and one carries a hand-made witness.

## Baseline

2026-10-05, master `04af1a9e` with this eval added and no other change (250 cases: 77 hand-written, 81 sqllogictest, 92 corpus; about 40 minutes of CPU, 4 cores shared):

| Group | Equivalent proved | Non-equivalent refuted | Other |
| --- | --- | --- | --- |
| `derived` (re-derived) | 1/7 | 7/8 | |
| `idiom` (authored) | 5/31 | 30/31 | |
| `slt` rule pairs | 2/2 | | |
| `slt` wrap pairs | 56/60 | | |
| `slt` mutants | | 18/19 | |
| `corpus` (unlabelled) | | | 6 proved, 9 refuted, 76 unknown of 91; Spark pair 50 unknown (not proved) |
| **All labelled** | **64/100** | **55/59** (tie-dependent 17/20, none proved) | **0 wrong** |

Held-out quarter (62 of the 250 cases): 16/26 equivalent proved (hand-written 1/10), 7/7 non-equivalent refuted, 0 wrong.

Read the 64/100 with care: 58 of the 64 proofs are the sqllogictest pairs, which are mostly a query against itself wrapped in a subquery or an unused CTE. On the hand-written pairs the baseline proves 6 of 38 equivalent pairs (16%): QUALIFY against the same window in a derived table (with a total order and, because both sides keep an arbitrary tied row, with ties), `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` against the default frame, the BigQuery default NULL order of `DESC`, and an unused window. None of the rank-one forms (`ROW_NUMBER`, `RANK`, join to the grouped `MAX`, `NOT EXISTS`, `MAX_BY`), the windowed aggregate against its join form, `ROWS` against `RANGE` over unique keys, `LAG` against `LEAD`, a filter pushed through a window, or the sessionisation and islands idioms is proved yet; they are the targets of the rule PRs. The tie traps hold: none of the 20 tie-dependent non-equivalent pairs is proved, 17 are refuted by a replayed database and 3 stay unknown (Spark pair 50, `RANK() <= 2` against `DENSE_RANK() <= 2`, and an `ARRAY_AGG .. LIMIT` pair DuckDB cannot run). No equivalent pair is refuted, including the two equivalent pairs that are nondeterministic on both sides (`MAX_BY` against `ROW_NUMBER() = 1`) and the ones that only look tie-dependent (only `ts` is read after the window).

Of the 91 unlabelled VeriEQL pairs the harness proves 6 (all Calcite), refutes 9 by an executed counterexample and leaves 76 unknown; none of its proofs is contradicted by a further search.


## Targets of the workstream

At least 70% of the equivalent pairs proved, every tie-dependent non-equivalent pair refuted or unknown (never proved), 0 wrong, and no prover floor dropped. These are targets for the finished workstream; the baseline above was taken before the rules.

## Limits of the evidence

- **Not independent.** The `derived` and `idiom` cases and the mutants were written by the author of this eval, who also reads the prover; this is a regression and honesty check, not an independent benchmark. The sqllogictest queries and the corpus pairs are other people's.
- **Rule-made pairs are not proofs.** A `slt-rule` pair is labelled `equivalent` because the pipeline's rules are sound and the two queries agree on the source's rows; that is evidence, not a proof. Only 2 of the mined queries are changed by the pipeline at all.
- **Execution is a sample.** The sets of possible results come from row orders of small databases on DuckDB, which may not realise every tie-break a real engine can. A witness is exact (it shows a difference); an `equivalent` label rests on the sample plus the reason in `why`.
- **Corpus pairs are mostly unlabelled.** They count for coverage and for wrong, not for the equivalent-proof rate.
- **Development splits only.** The corpus part reads the development samples; held-out VeriEQL, mined Calcite, Singh and SQLancer cases are not used. Rule authors working from this eval: the held-out quarter above is for the final measurement only.
