# Metamorphic fuzzing and unsafe-rewrite detection

Three seeded suites check KumoSQL's rewrites and its equivalence prover without any LLM at run time. They share one oracle (random small DuckDB databases with NULLs, duplicates and empty tables) and one report format. Every case id names its seed, so a failure is reproduced by rerunning the same command.

```
python tools/unsafe_fuzz.py fuzz --count 60 --seed 1     # SQLancer-style TLP, NoREC and mutants
python tools/unsafe_fuzz.py unsafe --count 20            # plausible-looking unsafe rewrites
python tools/unsafe_fuzz.py compose --count 60          # rule chains (tools/compose_fuzz.py)
```

`--show` prints every false proof or bad counterexample, `--json FILE` writes the summary. Tests live in `tests/test_unsafe_fuzz.py`; the long runs are marked `slow`.

## What is measured

Keep these apart: they are different kinds of evidence.

| Level | Meaning |
| --- | --- |
| Unbounded proof | The prover claims the queries agree on every database. |
| Replayed counterexample | The prover returns a database; both queries are run on it in DuckDB and must really differ. |
| Agreement on executed datasets | Both queries agree on N random databases. Evidence only. |

**Correctness** (all must be 0): `false_proofs` (proved, but the oracle finds a separating database), `bad_counterexamples` (a prover counterexample that does not separate the queries when replayed), `label_errors` (a pair constructed to be equivalent that the oracle separates, which means a bug in the generator), and for the composition suite `behaviour_changes` (a rule step whose output differs from its input on some database).

**Coverage**: proved / refuted / unknown / unsupported / timeout / error for the prover, and, for pairs the prover does not refute, whether KumoSQL's own executed check (`check_result_equivalence`) finds the difference. For pairs that really differ the report gives the share refuted by the prover, the share whose counterexample replays, and the share found either way.

**Held out**: some families are marked `heldout` (TLP aggregates, NoREC, a handful of unsafe-rewrite templates). They were not looked at while fixing bugs. A fix that only helps the development families does not move the held-out numbers. Exception: the outer-join flattening rules (`outer_join_flatten.py`) handle an unaliased derived table because another thread described the NoREC pairs that stayed unknown (`SELECT k FROM (SELECT .., (p) AS f FROM t LEFT JOIN u ..) WHERE f`); NoREC went from 108 to 122 of 420 held-out pairs proved with them (seed 2, count 60, 0 false proofs), so that gain is not a clean held-out result.

## Suites

**`fuzz`**: for random sources (one table, inner/left/cross joins) and random three-valued predicates, TLP (`Q` = `Q[p]` UNION ALL `Q[NOT p]` UNION ALL `Q[p IS NULL]`, also with DISTINCT/UNION and recombined COUNT/SUM/MIN/MAX) and NoREC (`WHERE p` against `COUNT(CASE WHEN p ...)` and a projected-then-filtered subquery). Mutated TLP/NoREC pairs (lost or duplicated partition, `SUM` instead of `COUNT`) and single-site mutations of generated multi-CTE queries (shapes: filters, DISTINCT, inner/left joins, GROUP BY with HAVING, CASE, COALESCE/IFNULL, UNION [ALL], IN / NOT IN / EXISTS subqueries) (flipped comparison, AND/OR swap, dropped conjunct or DISTINCT or WHERE or NOT, LEFT JOIN made INNER, UNION flipped, COUNT(*) made COUNT(col), constant bumped). A mutant is labelled *different* only if the oracle separates it.

**`unsafe`**: `NOT IN` against `NOT EXISTS` / anti-join, `COUNT(*)` against `COUNT(col)`, duplicate-producing joins against `IN`/`EXISTS`, `UNION` against `UNION ALL`, and WHERE/ON predicates moved across outer joins, each next to the variant that is correct.

**`compose`**: chains of the registered rules (random subset, random order, one to three repeats) over generated queries with CTE chains, duplicated and unused CTEs, subqueries and trivial predicates. After every step the output is compared with the step's input; the prover's verdict on each changed step is counted. The canonical pipeline (`canonical_rule_order()`) must reach a fixed point after one application (idempotence), within a bounded number of rounds (termination), and the same chain must give the same text twice (determinism).

## Bugs these suites found

* The executed check (`check_result_equivalence`) errored on any query that qualified a column by an unaliased table name (`SELECT t.a FROM t`): renaming the table dropped the name `t.a` refers to. It now keeps the original name as an alias. Before the fix the check could not run most unsafe-rewrite pairs.
* Prover counterexamples could fill a column the model never constrained with an arbitrary string, so an INT64 column held `''` and the counterexample could not be loaded. Unconstrained values are now `0`; the counterexample is still re-evaluated on the database before it is returned.
* `canonical_rule_order()` was not a fixed point: removing an unused CTE could leave another CTE with a single reader after `inline_single_use_ctes` had already run. Inlining now runs after CTE deduplication and removal.

Findings that are not bugs but gaps: for most non-equivalent unsafe pairs the prover returns *unknown* rather than a counterexample ("no row-preserving mapping", "UNION shapes differ"), so the executed check is what produces the replayable counterexample today. See the scoreboard row for the numbers.

## Reusable cases

`python tools/unsafe_fuzz.py unsafe --count 20 --seed 1 --dump-cases tests/fixtures/unsafe_rewrite_cases.jsonl` writes the cases as JSONL (`id`, `family`, `left`, `right`, `expect`, `heldout`). The committed copy is checked against its seed by the tests; other threads can use it as faulty variants. `expect` is `equivalent` (by construction) or `either` (the oracle decides): 220 cases are equivalent by construction and 340 are `either`, of which the oracle separates 339 (all but `union-intersect-5`).

Every discovered failure becomes a regression test in `tests/test_unsafe_fuzz.py`. The pre-fix baseline for the first run: 2 bad counterexamples on `fuzz` seed 1 (count 3) and one non-idempotent canonical pipeline in the first 15 composed queries.

Seeds 31 (fuzz, count 40: 850 cases) and 31 (compose, 60 queries) over the richer shapes found no further bugs: 0 false proofs, 0 bad counterexamples, 0 behaviour changes.

The generated shapes also cover window functions, QUALIFY, SAFE_*/NULLIF/IF, date arithmetic, UNNEST, STRUCT and NULL-heavy LEFT/RIGHT/FULL joins. Counterexamples that need a fractional value are replayed on DOUBLE columns (they are valid for FLOAT64 only). A fix from the first run over these shapes: when no integer model exists, a counterexample column that the queries only compare with numbers could be given a string; the model now prefers any numeric value before any other.
