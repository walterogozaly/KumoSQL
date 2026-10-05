# QED's CockroachDB cases

[Plain-language version](../../docs_simple/evals/qed-cockroach.md)

`python tools/qed_cockroach_bench.py` scores the CockroachDB optimizer cases shipped with the [QED prover](https://github.com/qed-solver/prover) (VLDB 2024, MIT). It is the head-to-head companion of the Calcite cases in [the SQLSolver page](sqlsolver.md#qeds-calcite-cases): the same prover, a second and larger suite, and a published number to compare with.

## The suite

QED's repository holds 1,287 CockroachDB cases (`tests/cockroach/memo`, `norm` and `xform`). Each is a pair of relational-algebra trees, the plan CockroachDB's optimizer had before and after one rewrite, in QED's JSON form. According to QED's extended paper they come from CockroachDB v23.1.3's optimizer tests. The paper reports 1,086 of 1,400 CockroachDB pairs proved (Table 1) and 1,051 in its abstract; the 1,400 are not all in the repository.

Provenance and licences. The repository is MIT and its LICENSE is kept next to the fixtures. The cases derive from CockroachDB's tests, which have their own licence terms (CockroachDB v23.1 was released under the Business Source License); nothing in QED's repository states which terms apply to the test data and the individual files were not checked, so the fixtures hold only what the MIT repository already redistributes, converted: SQL generated from the JSON trees with every table and column renamed (`t0`, `t0_c0`), no plan dumps, no CockroachDB names or commentary. [`tests/fixtures/qed_cockroach/README.md`](../../tests/fixtures/qed_cockroach/README.md) records this, the pinned QED commit (`9e9c262`) and how original and adapted material are separated.

## Conversion

`tools/qed_cockroach_to_sql.py` turns a case into two MySQL-flavoured SQL queries and a `CREATE TABLE` script, or skips it with a reason, never a guess (the Calcite converter, `tools/qed_to_sql.py`, is its base). **820 of 1,287 convert** (`tests/fixtures/qed_cockroach/`); **467 are skipped**. The skip reasons are listed per case in `qed_cockroach_skipped.jsonl` and tallied in `summary.json`. The largest:

| Reason | Cases |
| --- | ---: |
| `FUNCTION`, `UDF` or `SCALAR LIST` (QED's JSON keeps no function name or arguments) | 108 |
| a column or literal of a type with no exact encoding (OID, JSONB, UUID, arrays, TIMESTAMPTZ, ...) used in an expression | 101 |
| `LIMIT`/`OFFSET` without `ORDER BY` (which rows survive is undefined) | 92 |
| operators CockroachDB and DuckDB treat differently (`DIV`, `MOD`, `CONCAT`, `SIMILAR TO`, `COLLATE`, regular-expression match) | 49 |
| a projection or relation with no columns | 41 |
| a plan that relies on a `CHECK` constraint or computed column that QED's JSON drops | 22 |
| other aggregates (`CONST AGG`, `ARRAY AGG`, `STRING AGG`, ...), casts and literals | 54 |

Opaque types (OID, JSONB, geometry and the like) stay in the table as placeholder columns and may pass through filters, sorts and joins, but a pair that computes on one is skipped.

An earlier version of the converter read the ordinal of an opaque column wrongly inside an apply join (it forgot that ordinals count the enclosing relation's columns first). That showed up as `norm/337`, a pair QED proves and KumoSQL refuted (the wrong conversion, not a real difference; KumoSQL now proves it). It converted ten other pairs wrongly and skipped 22 that convert; all were regenerated and `tests/test_qed_cockroach_to_sql.py` holds the regression.

## Verdicts

Verdicts are those of the other QED harnesses (`tools/sqlsolver_bench.py` does the checking):

- **proved**: the prover proved the pair, and 60 random DuckDB databases (30 plain, 30 skewed towards the queries' literals; NOT NULL and keys respected; real BOOLEAN columns) show no difference. A proved pair whose SQL DuckDB cannot run would be listed as unchecked; there are none.
- **different**: not proved, and a database separates the two queries. A counterexample counts only if it reproduces with DuckDB's optimizer switched off (`kumosql.duckdb_load.run_unoptimized`), since DuckDB 1.5's optimizer returns wrong rows for some correlated subqueries. Such a pair leaves the score's denominator: it is not equivalent as converted.
- **unknown**: neither.
- **wrong**: proved, yet a counterexample exists. This must be 0.

## Results

Measured on master f4206ff plus this change, QED commit `9e9c262`, DuckDB 1.5.6, z3 5.1.0:

| | cases | share of 1,287 |
| --- | ---: | ---: |
| KumoSQL proved | 707 | 54.9% |
| KumoSQL refuted (counterexample) | 5 | 0.4% |
| KumoSQL unknown | 108 | 8.4% |
| not converted (unsupported) | 467 | 36.3% |
| **wrong** | **0** | |

Score: **707/815 proved, 0 wrong** (815 = 820 converted pairs minus the 5 refuted). Compared with QED, which proves **939 of the same 1,287** when run from its repository (published: 1,086 of 1,400 in the paper's Table 1, 1,051 in its abstract; this repo's cases are fewer than the paper's 1,400 and the binary was rebuilt from the repository, so only the second number is a like-for-like comparison):

| QED \ KumoSQL | proved | unknown | refuted | not converted |
| --- | ---: | ---: | ---: | ---: |
| QED proves (939) | 647 | 87 | 0 | 205 |
| QED does not prove (348: 323 notprovable, 25 error) | 60 | 21 | 5 | 262 |

QED is ahead on this suite: it proves 292 cases KumoSQL does not (87 converted but unknown, 205 not converted), and KumoSQL proves 60 that QED does not. KumoSQL's 707/815 is a score over the pairs it converts exactly, which flatters it next to a score over all cases; the 54.9% of all 1,287 is the comparable figure.

What the 292 cases are (the per-case QED verdicts are in `tests/fixtures/qed_cockroach/qed_verdicts.json`, and `python tools/qed_cockroach_bench.py` prints the cross-table above; the groups below are diagnoses read from the plan dumps and the prover's reason strings, not tested by implementing the rules):

- **Not independently checkable, 77.** QED treats an unnamed `FUNCTION`/`UDF`/`SCALAR LIST` call as an uninterpreted operator and a `LIMIT` without `ORDER BY` as a function of its source's bag, so its proofs there hold for any functions and any row choice. DuckDB cannot confirm or refute them and KumoSQL does not claim them.
- **Checkable, converted, unknown, 87.** The prover reads every number as a real (so `x > 10` is not `x >= 11`: 14 pairs, plus 7 more that crash Z3 on index-scan plans), reads BOOLEAN columns as integers (11), does not remove apply-join (`LATERAL`) correlation that an equality makes redundant (21), does not turn outer joins into inner joins on keys or null-rejecting filters (15), nested EXISTS (4), DISTINCT aggregates over a key (3), identity arithmetic against a decimal cast (5), cross-join normalization (1); six integer-overflow constants are refused on purpose (sound). Every one is an exact conversion that DuckDB could test; none shows a difference.
- **Checkable in principle, not converted, 128.** Zero-column relations (32), types without an exact DuckDB encoding such as JSONB, UUID, arrays and TIMESTAMPTZ (44), operators whose semantics differ between engines (30), plans that rely on a constraint QED's JSON drops (9) and query parameters, literals and casts the converter refuses (13).

The 5 refuted pairs (`memo/395`, `memo/414`, `memo/418`, `norm/487`, `xform/404`) are QED's own `notprovable` pairs, and the counterexamples are real differences between the queries as the JSON states them: three CockroachDB index constraints whose inclusive bound `(/NULL - /9]` became `< 9` in QED's JSON, one plan that relies on a unique index (`i`, `s`) that the JSON's schema omits, and one CockroachDB key-less `GROUP BY` (no rows on empty input) that the JSON cannot tell from a global aggregate (one NULL row). They are not CockroachDB bugs. They stay in the suite as guards: a proof of any must fail the test.


## The 87 converted cases QED proves and KumoSQL leaves unknown

Each pair is an exact conversion, so DuckDB can test it; none shows a difference.

| Group | Cases | Case ids |
| --- | ---: | --- |
| Integer range tightening (`x > 10` is `x >= 11`) | 14 | memo/349, memo/351, memo/363, memo/365, memo/391, memo/425, memo/171, norm/1264, norm/1266, norm/1269, norm/1279, xform/128, xform/133, xform/978 |
| BOOLEAN columns read as numbers | 11 | memo/43, memo/44, norm/43, norm/44, norm/45, norm/150, norm/152, norm/154, norm/155, norm/156, norm/157 |
| Z3 crash on index-scan pairs | 7 | memo/357, memo/360, memo/369, memo/372, xform/1008, xform/1009, xform/1010 |
| Apply join (LATERAL) decorrelation | 21 | norm/312, norm/314, norm/315, norm/316, norm/317, norm/318, norm/319, norm/321, norm/322, norm/323, norm/327, norm/328, norm/331, norm/332, norm/334, norm/339, norm/341, norm/343, norm/811, norm/812, xform/450 |
| Outer, full and cross joins changed to inner joins | 15 | memo/304, memo/306, memo/335, norm/717, norm/718, norm/719, norm/721, norm/801, norm/802, norm/923, norm/1164, xform/483, xform/485, xform/519, xform/521 |
| EXISTS, NOT EXISTS and semi/anti joins | 4 | norm/244, norm/746, norm/1363, xform/348 |
| Aggregates | 3 | norm/479, norm/498, norm/500 |
| Identity arithmetic against a cast | 5 | norm/882, norm/883, norm/885, norm/888, norm/889 |
| Integer overflow constants | 6 | norm/67, norm/71, norm/75, norm/361, norm/363, norm/365 |
| Cross join and filter normalization | 1 | norm/766 |

## Rerun and tests

```
python tools/qed_cockroach_bench.py [--workers N] [--verdicts out.json]
python tools/qed_cockroach_to_sql.py --src <prover>/tests/cockroach      # regenerate the fixtures
python tools/run_tests.py --label "QED CockroachDB" --target tests/test_qed_cockroach_benchmarks.py tests/test_qed_cockroach_benchmarks.py tests/test_qed_cockroach_to_sql.py
```

`tests/test_qed_cockroach_benchmarks.py` fails on any wrong proof, below its floor of 707 proved, or if a guard pair is proved. `tests/test_qed_cockroach_to_sql.py` checks the fixture, the provenance record and the converter's rules. The benchmark takes about three minutes with four workers.

## Limits of this evidence

- **No rule was developed on these pairs.** This is the first measurement, so nothing is tuned on test and no held-out split exists; later rules that use the unknown pairs as targets must say so in the results file.
- A proof here is an unbounded proof by the prover, tested by 60 random databases; it is not a proof for CockroachDB (its types, collations and error behaviour are mapped onto DuckDB's, and the conversion is approximate where a type has no exact encoding, which is why such pairs are skipped).
- The 5 refuted pairs leave the denominator. If a conversion error rather than QED's JSON made one of them different, the score would be overstated by at most 5 pairs.
- The QED figures are QED's own verdicts from a build at the pinned commit with a 10-second SMT timeout and a 120-second outer limit per case; they are the number to beat, not an audit of QED.
