# SQLFluff rule fixtures

[Plain-language version](../../docs_simple/evals/sqlfluff-fixtures.md)

[sqlfluff](https://github.com/sqlfluff/sqlfluff) tests each lint rule with a query it flags (`fail_str`) and the exact query its fixer returns (`fix_str`). Those 850 pairs are a labelled corpus of the refactors and reformatting that SQL teams already apply with sqlfluff, which makes two claims checkable with no model at run time:

- **Semantic fixes** (alias, ambiguity, convention, reference and structure rules): is the fixed query equivalent to the flagged one? KumoSQL's prover tries to prove each pair, and the fixes that change meaning *by design* must never be proved.
- **Layout fixes** (layout and capitalisation rules): did the fixer leave the parse tree, the comments and the string literals alone?

Then KumoSQL's own `format_sql` and structural rewrite rules are scored against the same fixtures where they overlap.

## Source, version and licence

| | |
| --- | --- |
| Source | `test/fixtures/rules/std_rule_cases/*.yml` in [sqlfluff](https://github.com/sqlfluff/sqlfluff) |
| Version | 4.3.0, commit `2e2275713d078f29529250f7ff883fbf66f83e0d` (2026-09-29) |
| Licence | MIT, Copyright (c) 2018-2026 Alan Cruickshank and contributors: [LICENSE.md](../../benchmarks/sqlfluff_rule_cases/LICENSE.md) |
| Stored as | [`benchmarks/sqlfluff_rule_cases/rule-fixtures.json`](../../benchmarks/sqlfluff_rule_cases/rule-fixtures.json), every case with a `fix_str`: 850 of the 2,527 cases in 93 files (the rest only pass or fail without a fix). `python tools/sqlfluff_fixtures_bench.py extract <checkout>` rebuilds it from a checkout of the pinned commit. |

Credit for the cases belongs to the sqlfluff authors; KumoSQL only reads them.

**Overlap with other evals.** None of the 850 flagged queries occurs in this repository's checked-in files, and the other evals score equivalence or rewriting of workload queries (Calcite, VeriEQL, Singh and Bedathur and so on), not lint fixes. What does overlap is function: KumoSQL's `format_sql` rule is sqlfluff behind a verified wrapper, `lift_subqueries` is ST05's fix, `inline_single_use_ctes` its reverse, and sqlfluff's complexity score drives the [Refactor](../refactor.md) page. The structural-rule overlap is measured below.

## What is scored

| Part | Cases | Question |
| --- | ---: | --- |
| [Semantic fixes](#semantic-fixes) | 397 | Rules AL, AM, CV, RF, ST (380 pairs) plus the T-SQL and Oracle structure rules TQ and OR (17). 369 keep the meaning; 28 change it by design |
| [Layout fixes](#layout-fixes) | 453 | Rules LT (372), CP (73) and JJ (8). 425 should change layout only; 28 (CP02) rename identifiers |
| [KumoSQL on the fixtures](#kumosql-on-the-fixtures) | the layout fixtures, plus the semantic ones for rewrite rules | Does `format_sql` reproduce sqlfluff's fix, and does KumoSQL's verification accept it? Do the structural rewrites agree with the fixes? |

**Original and adapted cases are recorded apart.** The *original* cases are the pairs exactly as sqlfluff wrote them, and every score below counts only those. A pair the checker cannot read as written (a script, an `INSERT ... SELECT`, Jinja templating) may also get an *adapted* form: each changed statement's query is compared on its own after checking that the statement around it is unchanged, or the Jinja tags are replaced by placeholders that depend only on the tag's text. Adapted results are reported next to the score and never added to it.

**Reported outcomes.** For every part: `proven`, `refuted` (a counterexample database or a changed tree, comment or literal), `unknown`, `unsupported` (the SQL cannot be read as written, or sqlglot has no such dialect), `timeout`, `error` and `wrong` (a false proof or a false verification), over the full corpus, plus the score on the subset the tools can read. Unknown beats wrong.

**Held out.** One rule code in five, picked by a hash of the code (`sha1("sqlfluff-rule:" + code) % 5 == 0`), is held out of development runs and scored once at the end: AL01, AM05, AM08, CV02, CV04, CV05, CV06, CV08, LT01, LT12, LT13, ST11 and TQ02. That is 189 of the 850 pairs. Run `--split dev` while developing and `--split held-out` only for a final score.

## Semantic fixes

`python tools/sqlfluff_fixtures_bench.py semantic` (about a minute on 4 cores).

The fixtures have no schema, so one is read off the queries: every column a query mentions (qualified by a table or alias, or unqualified in a select over one table, or read through a `SELECT *` derived table or CTE) belongs to its table, and every table gets two placeholder columns so that `SELECT *` has a width. A verdict therefore holds for tables with those columns, which is a weaker claim than "for every schema".

For each pair `prove_equivalent_algebraic` runs on the two queries as written (output names compared), in the case's dialect. A proof is then re-run on 360 random DuckDB databases built from that schema; a database that separates the pair would make it **wrong**. When there is no proof, 120 random databases look for a counterexample, and one that separates the pair makes it **refuted**. A database that comes up twice (every ninth seed gives the all-empty one) is run once, and two queries that translate to the same DuckDB text run once per database (`kumosql.random_check.run_all`). DuckDB can crash on odd joins, so each execution check runs in a child process; a crash only means the proof is not re-checkable.

**Pairs that change meaning by design** are labelled from what the rule does and the shape of the pair, never from a verdict:

| Rule | Why the fix changes the meaning |
| --- | --- |
| CV05 | `= NULL` never holds; the fix's `IS NULL` can |
| ST06 | moves calculated select targets after simple ones, so the result's columns come back in a new order (also through a bare `*` over a reordered CTE) |
| ST07, CV08 | `USING` to `ON`, and `RIGHT JOIN` to `LEFT JOIN`, change which columns a bare `*` returns and in what order (only the pairs with a bare `*`) |

These 28 pairs must be refuted or left unknown; a proof would be a false proof.

### Results

Measured 2026-10-02 over all 397 pairs. The first run, with no implementation change, proved 214 and refuted 4 meaning-keeping pairs; the only change since is the string-literal canonicaliser for [#314](https://github.com/walterogozaly/KumoSQL/issues/314), which moved those 4 to proved:

| | Proven | Refuted | Unknown | Unsupported | Timeout | Error | Wrong |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Meaning kept (369) | **218** | 0 | 7 | 144 | 0 | 0 | 0 |
| Changes by design (28) | 0 | **18** | 4 | 6 | 0 | 0 | 0 |

**218/369 fixes that keep the meaning are proved, 218/225 of those the prover can read, and no fix that changes meaning is proved: 0 wrong.** Developing rules (308 pairs): 168/286 and 12/22 refuted. Held-out rules (89 pairs): 50/83 proved (all 50 supported ones), 6/6 by-design changes refuted. One held-out case was read after it exposed a labelling error in the layout part, which makes the held-out split slightly weaker than the others.

Adapted: 33 of the 150 unsupported pairs have an adapted form (a script or a query inside `INSERT ... SELECT` or `CREATE ... AS`); 24 are proved, 1 refuted (an ST06 view that reorders its columns) and 8 stay unsupported. 12 of the adapted proofs are CV06 scripts (a terminator added to each statement); 4 pairs are Jinja-templated and are adapted by the same tag masking as in the layout part.

| Rule | Pairs | Proven | Refuted | Unknown | Unsupported | Adapted (proven/total) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| AL01 | 15 | 12 | 0 | 0 | 3 | 2/2 |
| AL02 | 9 | 6 | 0 | 0 | 3 | 1/1 |
| AL05 (with AL05+CV12) | 30 | 10 | 0 | 1 | 19 | 0/1 |
| AL07 | 10 | 8 | 0 | 0 | 2 | – |
| AL09 | 5 | 5 | 0 | 0 | 0 | – |
| AM02 | 4 | 4 | 0 | 0 | 0 | – |
| AM03 | 3 | 3 | 0 | 0 | 0 | – |
| AM05 | 14 | 14 | 0 | 0 | 0 | – |
| CV01 | 8 | 6 | 0 | 0 | 2 | – |
| CV02 | 2 | 2 | 0 | 0 | 0 | – |
| CV03 | 3 | 2 | 0 | 0 | 1 | 1/1 |
| CV04 | 9 | 9 | 0 | 0 | 0 | – |
| CV05 (by design) | 6 | 0 | 6 | 0 | 0 | – |
| CV06 | 36 | 10 | 0 | 0 | 26 | 12/12 |
| CV07 | 6 | 6 | 0 | 0 | 0 | – |
| CV10 | 16 | 14 | 0 | 0 | 2 | – |
| CV11 | 41 | 20 | 0 | 2 | 19 | 1/1 |
| CV12 | 14 | 8 | 0 | 0 | 6 | 1/1 |
| OR01 | 6 | 0 | 0 | 0 | 6 | – |
| RF03 | 12 | 7 | 0 | 0 | 5 | – |
| RF06 | 16 | 14 | 0 | 0 | 2 | – |
| ST01 | 2 | 2 | 0 | 0 | 0 | – |
| ST02 | 17 | 10 | 0 | 0 | 7 | – |
| ST04 | 12 | 8 | 0 | 3 | 1 | – |
| ST05 | 31 | 14 | 0 | 0 | 17 | 5/7 |
| ST06 (mostly by design) | 23 | 1 | 10 | 4 | 8 | 0/2 |
| ST07 (partly by design) | 8 | 4 | 2 | 0 | 2 | – |
| ST08 | 7 | 0 | 0 | 1 | 6 | – |
| ST09 | 20 | 18 | 0 | 0 | 2 | 0/1 |
| ST12 | 1 | 0 | 0 | 0 | 1 | 1/1 |
| TQ02, TQ03, TQ04 | 11 | 1 | 0 | 0 | 10 | 0/3 |

Why pairs are unsupported: queries whose columns come from nowhere (`SELECT col1 "alias"` with no `FROM`); dialect syntax sqlglot cannot parse (`CONVERT(int, 1)` in ANSI, Snowflake `LATERAL FLATTEN`, T-SQL `GO` batches, Oracle `/`); dialects sqlglot lacks (Exasol, DB2); `NATURAL`, `SEMI` and `ANTI` joins; array subscripts; `CURRENT_DATE`; DML such as `DELETE`; and the 4 Jinja-templated pairs.

### Findings

Every pair that is not proved stays in the test file as a regression case: it may improve, but it must never become a wrong verdict.

- **sqlglot reads some BigQuery literals differently from BigQuery (fixed, [#314](https://github.com/walterogozaly/KumoSQL/issues/314)).** Four pairs that keep the meaning were refuted at first because DuckDB separated them: CV10 `test_fail_unnecessary_escaping`, `test_fail_tripple_quoted_strings_dont_remove_escapes_single_quotes` and `..._double_quotes` (sqlglot keeps a backslash escape such as `\"` in the literal's text, so `'a\"b'` and `'a"b'` differ, and writes the first back as `'a\\"b'`, a different string), and AL09 `test_fail_bigquery_quoted_column_no_space_without_as` (sqlglot reads ``col``col`` as one identifier containing a backtick, where BigQuery sees `col` aliased as `col`). `kumosql.string_literals.canonical_literals` now gives each plain string and each pair of adjacent quoted names one spelling before the provers and the execution check read them, and all four are proved.
- **No sqlfluff fix was found unsafe.** No pair that should keep its meaning has a counterexample.
- **The by-design changes behave.** CV05 refutes in 6 of 6, ST06 in 10 of its 20 by-design pairs (4 stay unknown because an output column is named differently on each side, 6 are unsupported), ST07 in 2 of 2 pairs with a bare `*`. None is proved.
- **Seven pairs that keep the meaning stay unknown**: ST04 with comments between the nested `CASE`s (the output column name comes out as `value2` on one side and unnamed on the other), ST08 `SELECT DISTINCT(field_1)`, AL05 on a Spark `VALUES` clause, and two T-SQL `CONVERT` rewrites.

## Layout fixes

`python tools/sqlfluff_fixtures_bench.py layout` (seconds).

Each pair is read with sqlglot in the case's dialect. A layout fix may change spacing, line breaks, indentation and the case of keywords and function names, and nothing else, so three things must stay the same:

1. **The parse tree** (comments aside; function-name case is ignored).
2. **The comments**, their words and their order. Re-indenting the lines of a block comment is layout.
3. **The string literals**, token by token.

Passing means identical trees, a syntactic identity rather than a solver proof. CP02 re-cases or re-spells identifiers, and a case-sensitive engine (BigQuery table names) reads that as a different name, so a CP02 fix that renames an identifier is labelled as changing meaning and must **not** pass.

| | Proven (unchanged) | Refuted | Unsupported | Wrong |
| --- | ---: | ---: | ---: | ---: |
| Layout only (425) | **330** | 1 | 94 | 0 |
| Renames identifiers (28) | 0 | **25** | 3 | 0 |

**330/425 layout fixes leave the tree, comments and literals alone, and no identifier rename passes: 0 wrong.** Developing rules: 253/328 layout-only, 23/25 renames refuted. Held-out rules (LT01, LT12, LT13, CP02+LT01): 77/97, 2/3 renames refuted, the rest unsupported.

The unsupported 97 are 50 Jinja-templated pairs, 15 where sqlglot falls back to a raw command, 30 with Oracle, T-SQL and Snowflake syntax it cannot parse (PL/SQL blocks, `EXCEPTION` handlers) and 2 in Exasol, which sqlglot lacks. With each Jinja tag replaced by a placeholder that depends only on its text, **41 of the 50 templated pairs pass** (not in the score above): JJ01's padding fixes no longer matter, and everything outside the tags is compared as usual.

The one flagged layout-only pair is CP05 `test_fail_postgres_create_type`: `CREATE TABLE t (name MOOD)` becomes `name mood`, which PostgreSQL reads as the same type but sqlglot keeps as two identifiers. The check is stricter than PostgreSQL here.

## KumoSQL on the fixtures

`python tools/sqlfluff_fixtures_bench.py kumosql` (about 20 seconds).

**`format_sql`.** KumoSQL formats BigQuery with sqlfluff through [its formatting wrapper](../ui.md): BigQuery dialect, repeat until stable, quoted names restored, and every result verified. Each layout fixture that its preferences can express (ANSI or BigQuery, no Jinja, a configuration made of line length, indentation, comma position or a capitalisation policy) is run with only that fixture's rule switched on.

| | Fixtures |
| --- | ---: |
| Supported by KumoSQL's preferences | 169 |
| Output equal to sqlfluff's fix | **167** |
| Output differs | 2 |
| Verified by KumoSQL (proven equal, or unchanged) | **161** |
| Unverified (refused as unproven) | 8 |
| Verified but the output changes the tree, a comment or a literal | **0** |
| Unsupported (another dialect 105, configuration KumoSQL cannot express 121, Jinja 49, sqlfluff cannot parse 9) | 284 |

All 167 reproductions are exact, and the 8 unverified runs are right to be: seven rename identifiers (CP02) and one is a BigQuery `WEEK(monday)` whose keyword sqlglot reads as a column.

- **Finding, fixed ([#313](https://github.com/walterogozaly/KumoSQL/issues/313)): sqlfluff's fixer can turn spaced unary signs into a comment.** LT01 on `SELECT 1 * - - - 5` returns `SELECT 1 * ---5` (the fixture expects `- - -5`), and `--5` starts a comment; the second fixture lost `AS c, 2 AS d` the same way. KumoSQL's verification marked both runs unproven, so nothing wrongly passed, but `format_sql` still produced the text. It now keeps the text from before any pass that creates or removes a comment, so these two fixtures come back unchanged: they differ from the fixture's fix on purpose and count as verified (unchanged). With sqlfluff 4.4 installed (what `pip install` picks today), the fixer spaces the signs correctly (`- - -5`), so `format_sql` reproduces both fixtures' fixes and they are proven; the scoreboard numbers are measured with sqlfluff 4.3.0.

**Structural rewrite rules.** The rules in the registry (other than `format_sql`) are applied to every semantic fixture's flagged query that BigQuery can read (ANSI or BigQuery dialect, one query). Each one that changes the query is counted, and its verification status and whether its output equals sqlfluff's fix (same parse tree, or proved equivalent) are recorded; a rule whose verified output a random database separates would be **wrong**.

| Rule | Fired | Verified | Same as sqlfluff's fix |
| --- | ---: | ---: | ---: |
| `lift_subqueries` (ST05's fix) | 36 | 29 | 16 |
| `inline_single_use_ctes` | 18 | 15 | 11 |
| `remove_redundant_parentheses` | 20 | 20 | 8 |
| `remove_trivial_predicates` | 6 | 6 | – |
| `remove_unused_ctes` | 4 | 4 | 1 |
| `deduplicate_ctes`, `remove_redundant_distinct` | 0 | – | – |

None is wrong. The seven unverified `lift_subqueries` results are ones KumoSQL's verification could not prove and refused: four ST05 fixtures with set operations or a `WITH` inside the subquery, one CV07 query, and two fixtures whose input BigQuery would reject (`select foo.bar from (select 1 as bar) AS bar, (select 1 as foo) AS foo` and a query reading `a.y` from a derived table that outputs only `a` and `x`), which the input check refuses to prove (see [Rewrite rules](../rewrite-rules.md#inputs-bigquery-would-reject)). Before that check they counted as 32 verified. One fixture fewer is fired and one fewer verified than before the lifter learned to keep a subquery whose body reads a column no relation binds (a lint fixture with no `FROM`, such as `WITH cte1 AS (SELECT a)`, where `a` could belong to an enclosing query); `inline_single_use_ctes` verifies one fewer for the same reason. Differing from sqlfluff's fix is not an error: the two tools name lifted CTEs differently and flatten different parts.

## Caveats

- The fixtures are what sqlfluff's authors expect, not ground-truth equivalence, and some fixes change meaning on purpose; hence the by-design labels, which come from reading each rule rather than from a verdict.
- Verdicts hold for the inferred schema, not for every schema. Two placeholder columns make `SELECT *` well defined; they do not make a proof about every width.
- A pair that cannot be read is `unsupported`, not failed; the supported-subset scores leave those out, and the full-corpus numbers keep them in.
- Layout passing is parse-tree identity under sqlglot's reading of the dialect.

## Rerun

```shell
python tools/sqlfluff_fixtures_bench.py semantic [--split dev|held-out] [--show unknown|unsupported|refuted|wrong|all] [--rule ST05]
python tools/sqlfluff_fixtures_bench.py layout
python tools/sqlfluff_fixtures_bench.py kumosql --show different
python -m pytest tests/test_sqlfluff_fixtures_bench.py
```

The scoreboard rows are `benchmarks/results/sqlfluff-semantic-fixes.json`, `sqlfluff-layout-fixes.json` and `sqlfluff-kumosql-formatter.json`.
