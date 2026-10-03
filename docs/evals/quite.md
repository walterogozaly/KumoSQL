# QUITE's LLM query rewrites

[QUITE](https://github.com/Yuyang-Song/QUITE) (Song et al., "QUITE: A Query Rewrite System Beyond Rules with LLM Agents") compares LLM-based query rewriters on TPC-H, DSB, Calcite and SQLStorm (StackOverflow) queries. Its `experiments_results/` folder publishes every rewrite the systems produced, each with an `equivalence` flag from running the original and the rewrite on the authors' PostgreSQL instance. KumoSQL reads those pairs with no model at run time: the algebraic prover, then databases on which both queries are replayed in DuckDB.

```
python tools/quite_bench.py                    # every distinct pair (about an hour on 4 cores)
python tools/quite_bench.py --split dev        # the pairs used while developing
python tools/quite_bench.py --sample 48        # the pinned sample tests/test_quite_bench.py runs
python tools/quite_bench.py --log run.jsonl    # append verdicts as they land; a rerun resumes
python tools/quite_bench.py --write-results    # quite-rewrites and quite-negatives
python tools/quite_bench.py --overlap          # which originals other evals hold (fetch them first, below)
```

## Source

The repository has no licence, so nothing from it is copied into KumoSQL. `tools/quite_bench.py` downloads the 52 flagged result files and the four schema files from commit `0cffd7ce412dcd5c46bf272cd6464a00120db91c` into `~/.cache/kumosql-bench/quite` (or `$KUMOSQL_BENCH_DATA`) and checks each file's SHA-256. Credit for the data belongs to the authors.

| Benchmark | Queries | Systems | Rewrites | Distinct pairs | Flagged equal | Flagged unequal |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
@@SOURCE_TABLE@@

The 13 systems are QUITE and QUITE with hints, LLM-R2, R-Bot and a plain LLM agent on several models (Claude 3.7, DeepSeek R1 and V3, GPT-4o), and LearnedRewrite. A fourteenth file per benchmark (`EXP_QUITE_*`) holds QUITE's raw output without a flag and is not used. "Queries" counts distinct originals: the files spell some queries differently (`interval '30 days'` against `interval '30' day`, quoted and unquoted names) and number them differently, so a query is identified by its parsed, case-folded text. Identical (original, rewrite) texts are scored once, with the number of systems that produced them kept; @@IDENTICAL@@ of the distinct pairs have a rewrite identical to the original up to spaces.

## What a flag means

The flags are execution results on one database, not semantic labels. The authors document one rewrite (TPC-H Q2, `documents/equivalence_definition_and_known_cases.md`) that returns the same rows on their instance but drops a `region` filter, and they set its flag to false by hand. Their evaluation script also flags a pair unequal when either query hits the 300-second timeout or the rewrite fails to run. Each flagged-unequal rewrite is classified from its recorded times:

| Why it was flagged unequal | How it is recognised | Distinct pairs |
| --- | --- | ---: |
@@REASON_TABLE@@

## How a pair is decided

Nothing reads the flag while deciding.

1. **Proven.** `prove_equivalent_algebraic` with the published schema (types, primary keys, NOT NULL, foreign keys), PostgreSQL dialect, output names ignored, results compared as bags. One proof attempt is capped at 60 seconds.
2. **Refuted.** A database on which DuckDB returns different bags for the two queries. Candidates are the prover's counterexample, KumoSQL's targeted databases (`kumosql.refute`, 8 seconds per pair), the database the authors describe for their Q2 case (for TPC-H pairs), and a TPC-H instance at scale factor 0.01 generated with `tpchgen-cli` when it is installed. Every candidate is repaired to respect the keys and foreign keys (composite ones included), rejected if it still breaks one, and replayed: the difference counts only when DuckDB's unoptimized plan agrees (`run_unoptimized`).
3. **Unknown**, **unsupported** (sqlglot cannot parse a query), **timeout** or **error** otherwise.

Every proof goes through step 2 as well; a proof with a replayed difference is **wrong**.

DuckDB runs with PostgreSQL's integer division and NULL ordering, `now()` and `current_date` read one fixed instant on both sides, and `char(n)` padding is ignored. Queries with `random()` are never refuted. When a query's rows can depend on input order (a `LIMIT` whose `ORDER BY` does not cover every output column, `ROW_NUMBER`, `LAG`, `STRING_AGG`, `DISTINCT ON`, a `ROWS` frame), a difference counts only if both queries return the same bags with every table loaded in three row orders, and the generated instance (whose row order cannot be changed) is not used.

Two harness-side readings, no KumoSQL change: identifiers are folded to lower case, as PostgreSQL folds unquoted ones (the Calcite queries quote `"EMP"` against tables created as `emp`; @@ADAPTED@@ pairs needed a quoted name folded and are counted apart below), and `FETCH FIRST n ROWS ONLY` is read as `LIMIT n`, which the prover handles.

**Held out.** One query in five, by SHA-1 of `quite/<benchmark>/<query hash>`, is held out with all its rewrites; `--split dev` runs the rest. No KumoSQL rule was written for this eval.

## Scores

@@SCORES@@

## Overlap with other evals

`python tools/benchmark_corpora.py fetch llm-r2 sqlstorm dsb`, then `python tools/quite_bench.py --overlap`, matches each QUITE original against the other evals' queries, as the same text (case, quotes and layout ignored) and as the same shape (every literal masked, so another instance of the same template):

| Other eval's queries | QUITE queries | Same text | Same shape |
| --- | ---: | ---: | ---: |
@@OVERLAP_TABLE@@

The TPC-H and DSB originals are template instances of the kind [LLM-R2](llmr2-bench.md) and [analytical coverage](analytical-sql-coverage.md) already run, and most TPC-H originals are verbatim LLM-R2 queries; what is new here is the rewrites and their flags, which no other eval has. The Calcite originals overlap [R-Bot's Calcite pairs](sqlsolver.md#r-bots-calcite-pairs) for @@RBOT@@ of 58 queries, but the rewrites differ (R-Bot's are Calcite's rule outputs). Five of the 43 SQLStorm queries are verbatim SQLStorm v1.0 StackOverflow queries; the others are not in SQLStorm v1.0 as published.

## Limits

* The authors' database instances are not public, so a refutation comes from a database KumoSQL builds; a flagged-equal pair that is refuted is an instance-label disagreement, not an error of either side.
* Results are compared as bags. The authors compare as lists when the original has an outer `ORDER BY`; @@ORDER_DROPPED@@ distinct pairs drop the original's top-level `ORDER BY`.
* PostgreSQL and DuckDB can still differ in places the harness does not cover (string collation, numeric precision of `AVG` beyond six decimal places); a replayed difference is shown with its database, so each can be checked by hand.
