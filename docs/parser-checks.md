# Parser checks

[Plain-language version](../docs_simple/parser-checks.md)

Every prover starts from sqlglot's reading of the text. When sqlglot groups operators differently from the engine, drops a `NOT`, or reads code as a comment, the provers reason about a query nobody wrote, and the proof is wrong although no rule is. `kumosql.parse_check` reads each query a proof depends on a second time, with its own tokenizer and its own precedence tables, and compares the two readings. Any difference turns the proof into `not_proven` with the reason `parser disagreement: ...`. The check can only remove a proof; it never adds one.

## Where it runs

One decorator, `parse_check.refuse_misread_proofs`, wraps the public entry points where a proof is accepted:

| Entry point | Dialect used |
| --- | --- |
| `equivalence.prove_equivalent` | BigQuery |
| `algebraic_equivalence.prove_equivalent_algebraic` | the call's `dialect` (BigQuery by default) |
| `smt_equivalence.prove_equivalent_smt` | the call's `dialect` |
| `sqlsolver_backend.prove_equivalent` and `prove_equivalent_sqlsolver` | BigQuery |

A proven or conditionally proven result is checked against the two texts the caller passed; a disagreement replaces it with `not_proven` (`dataclasses.replace`, so `proof_checks`, diagnostics and the other fields stay; a conditional proof also loses its conditions). Only the outermost call checks, so texts the provers write for their own stages are not read again. A result that is not a proof, including a counterexample, passes through untouched. A failure inside the checker never fails the prover: the proof stands and the query counts as unchecked. The rewrite verifier, the pipeline checks and the model-reuse checks reach the provers through these functions and need no change.

The reason names the construct, for example `parser disagreement: GoogleSQL reads (a) | (b & c); sqlglot's tree does not`. `parse_check.check_query(sql, dialect)` returns `agree`, `disagree` or `unchecked`; `disagreement(sql, dialect)` adds the round trip below; `reading(sql, dialect)` prints the independent reading with every grouping in parentheses.

## What is compared

1. **An independent reader.** `parse_check` tokenizes the text itself and parses expressions with precedence tables taken from each engine's grammar: GoogleSQL (`bigquery`), MySQL 8.0 `sql_yacc.yy` (`mysql`) and PostgreSQL `gram.y` as DuckDB builds on it (`postgres`, `duckdb`). Any other dialect, and any construct the reader does not know (`LATERAL`, `PIVOT`, `UNPIVOT`, `TABLESAMPLE`, set operations `BY NAME`, window frame `EXCLUDE`, MySQL index hints, scripts and DDL, pipe syntax), is `unchecked`, never a disagreement. The one exception is `MATCH_RECOGNIZE`: its clause is a language of its own that the reader cannot check, so `guarded` refuses a proof of any text that holds it ([match-recognize.md](match-recognize.md)).

   Both readings are reduced to operation keys: for each operator, clause, set operation or `LIMIT`, its kind and the source positions of the names and literals under each operand. Every key of the independent reading must appear in sqlglot's tree (AND and OR chains are flattened through parentheses on both sides). A few operations must also not appear without a source: a `NOT`, a unary minus or `~`, a `DESC`, a `SELECT DISTINCT` or an aggregate `DISTINCT` in sqlglot's tree that the text does not have is a disagreement too, since adding one flips what a query returns just as dropping one does. Three more checks run on top: the token stream against sqlglot's own tokenizer (a comment or string that one side ends earlier), the names (compared as a multiset by text), and the literals. sqlglot 30 records where each name and literal starts; for sqlglot 26, which records nothing, the check makes the parser record it as each node is made. sqlglot gives `NULL`, `TRUE` and `FALSE` no position in any release, so they are placed by order between positioned neighbours; without that, `!2 IS NULL` looked the same under both readings.
2. **A round trip.** sqlglot prints the tree and reads it back; the operator grouping must come back the same. The provers print trees and read them again, so a printing that regroups operators would turn one query into another between two stages. Only grouping is compared: MySQL has no `FULL JOIN` and no `DESC NULLS FIRST` (sqlglot prints an emulation) and prints `a DIV b` as a rounding `CAST`, none of which is a reading of the text; `ast_utils.faithful_sql` already declines the pairs where such a printing matters.
3. **Build agreement.** `tools/parse_check_matrix.py` optionally reads the dev corpora under pure-Python and compiled SQLGlot 30.21.0 in temporary virtual environments and lists every query whose verdict, round trip or operator grouping differs. This focused diagnostic does not add another routine full-suite CI run. The older cross-version measurements below are historical.

## What sqlglot gets wrong

| Dialect | Text | sqlglot | The engine |
| --- | --- | --- | --- |
| BigQuery | `2 \| 1 & 0` | `(2 \| 1) & 0` = 0 | `2 \| (1 & 0)` = 2 (checked on BigQuery) |
| BigQuery | `1 ^ 1 << 1`, `4 \| 1 << 1` | one left-associative level for `\|`, `^`, `&`, `<<`, `>>` and `\|\|` | `<<`/`>>`, then `&`, then `^`, then `\|`; `\|\|` beside `*`: 3 and 6 (sqlglot: 0 and 10) |
| BigQuery | `a > 10 IS TRUE`, `a IS NULL = FALSE`, `a = b IS NOT DISTINCT FROM c` | reads them | rejects them: comparisons are not associative |
| MySQL | `a = b < c` | `a = (b < c)` | `(a = b) < c` |
| MySQL | `1--1` | `--1` is a comment | a comment only before whitespace; the value is 2 |
| MySQL | `a XOR b AND c`, `!a = b` | XOR below AND at the wrong level; `!` as a low-precedence `NOT` | `a XOR (b AND c)`; `(!a) = b` |
| MySQL | `!2 IS NULL` | `NOT (2 IS NULL)` | `(!2) IS NULL` |
| PostgreSQL, DuckDB | `a UNION b INTERSECT c` | left to right | `INTERSECT` binds tighter |
| PostgreSQL, DuckDB | `~1 + 1` | `(~1) + 1` | `~(1 + 1)` (DuckDB: -3) |
| MySQL, PostgreSQL, DuckDB | `a = b IS NULL`, `NULL > 2 % 3 IS NULL` | `a = (b IS NULL)` | `(a = b) IS NULL` |
| PostgreSQL | `'a' 'b'` on one line | concatenates | syntax error |
| DuckDB | `1 \| NOT 1` | reads it | syntax error (the grammar still has postfix operators) |

The last two `IS` rows are the one misread KumoSQL repairs itself: `ast_utils.check_modeled`, which every prover calls, re-reads an `IS` test after a comparison as the engines do. The check compares the independent reading with that repaired tree for MySQL, PostgreSQL and DuckDB, so such proofs stay (`tests/test_is_after_comparison.py`). BigQuery is not repaired, because GoogleSQL rejects the text, and stays a disagreement. Every other misread is declined, not rewritten, since a tree fix would change printed SQL everywhere. The decision for the rest is to decline, not to rewrite the tree: a tree fix would change printed SQL everywhere. A false proof was possible before the check: `SELECT a | b & c FROM t` against `SELECT (a | b) & c FROM t` gave identical sqlglot trees and `prove_equivalent_smt` and `prove_equivalent` proved them equal; both now answer `not_proven`.

sqlglot 26.0.0 has misreads the later releases fixed, and the check finds them (it is the reason a text can be `agree` under 30.21 and `disagree` under 26): `WEEK(TUESDAY)` loses `TUESDAY`, a column list on a table alias (`VALUES ... AS t (x, y)`) and bytes literals (`b"..."`) are dropped.

## Evidence from real engines

The precedence tables are checked against the engines, not only against their grammars. `tools/parser_oracle.py` generates random constant expressions (integers 0 to 3 and `NULL`, joined by the dialect's operators, mostly without parentheses; also `BETWEEN`, `IN`, `LIKE` and `CASE`), runs each text on the engine, then runs sqlglot's reading and the independent reading printed with every grouping parenthesized. Three counts matter:

- **Table errors**: the independent reading gives a different answer from the text. Must be 0.
- **Misreads**: sqlglot's reading gives a different answer from the text (or the engine rejects a text sqlglot reads). The check must disagree on every one.
- **Harmless**: the check disagrees but both readings give the engine's answer on these constants. The texts are still read two ways; they are listed, not counted against the check.

| Engine | Expressions | sqlglot misreads | Caught | Table errors |
| --- | --- | --- | --- | --- |
| MySQL 8.0.46 (real server), three seeds, 5,000 each | 15,000 | 4,780 | 4,780 | 0 |
| DuckDB 1.5.6, three seeds, 5,000 each | 15,000 | 3,180 | 3,180 | 0 |
| BigQuery (typed integer and boolean trees, one query, `tools/parser_oracle.py bigquery-emit`) | 40 | 4 | 4 | 0 |

Run on sqlglot 30.21.0; the same runs on sqlglot 26.0.0 also catch every misread. MySQL's own evidence answers the open precedence question: comparisons are one left-to-right level (`a = b < c` is `(a = b) < c`, `2 = 2 < 2` is 1), `XOR` sits between `OR` and `AND`, `!` binds tighter than `=` and `^` (`!2 = 1` is 0), and `--` starts a comment only before whitespace (`1--1` is 2). BigQuery has no local engine, so its 40 cases were run once with `execute_sql_readonly` (0 bytes billed): the text, the independent reading and the fully parenthesized tree gave the same answer every time, and sqlglot's reading differed on 4. BigQuery's rejection of non-associative comparisons was confirmed on BigQuery earlier. The sample is small and is evidence for the tables, not a measure of how often misreads occur in real queries.

## Fault injection

`tests/test_parse_check_faults.py` takes probe queries the check reads as `agree`, injects one fault into sqlglot's tree, and requires a disagreement: an operator rotated over its neighbour, a `NOT` or unary minus dropped or invented, an operator replaced by another (`+`/`-`, `AND`/`OR`, `<`/`>`, `|`/`&`), `DESC`, `DISTINCT` and `ALL` flipped, and the operands of a non-commutative operator swapped. All 417 injected faults are caught across the four dialects. The first run missed a few (an invented `DESC` and an invented `DISTINCT`, which the one-sided comparison could not see); that is why the "must not appear without a source" rule above exists. Same-class AND/OR rotations are not injected: they are associative.

## What the corpora show

Counts are distinct queries of the dev splits read with `tools/parse_check_sweep.py` (held-out files and rows are skipped and never printed).

| Corpus (dialect) | Agree | Unchecked | Disagree |
| --- | --- | --- | --- |
| GoogleSQL compliance queries (BigQuery) | 5,303 | 2,565 | 0 |
| Spider2 (BigQuery) | 127 | 15 | 0 |
| BigQuery edge cases, dev (BigQuery) | 831 | 12 | 20 |
| Calcite mined, QED, R-Bot, SPES, Cosette (MySQL) | 1,907 | 98 | 0 |
| dlbench (read as BigQuery) | 1,208 | 287 | 2 |
| Constraint, containment, decomposition, join-rewrite, documented-rewrite, LLM SQL solver, targeted-data, output-property, incremental, optimizer-bug, DBGPT corpora (BigQuery) | all agree or unchecked | | 0 |

Every disagreement was triaged by hand:

- **BigQuery edge cases, dev: 20 texts, all real.** 15 hold a non-associative comparison BigQuery rejects (`a > 10 IS TRUE`, `a IS NULL = FALSE`, `(f = a) BETWEEN ...`), 5 hold bitwise operators sqlglot groups differently from GoogleSQL (`NULL | 2 || (id || s)`, `1 & NULL >> -1 >= f`, `NULL ^ ~(-1) << ~(3)`). No checker bug.
- **dlbench: 2 texts**, SQLite chained comparisons (`1000 <= x <= 2000`) read under BigQuery; the benchmark does not read them as BigQuery.
- **Round trip (an earlier version refused 36 dev proofs of the Calcite family):** a checker bug, not misreads. MySQL printing idioms (`DIV` as `CAST`, `FULL JOIN`, `ANTI` and `SEMI JOIN` as `NOT EXISTS` and `EXISTS`, `DESC NULLS FIRST`, `TRUE` as `1`, `VALUES`) are not regroupings. Fixed by comparing operator grouping only; the 36 pairs prove again.

- **GoogleSQL compliance queries, round trip: 11 texts** that sqlglot prints differently from how it read them (an `ARRAY_FILTER` lambda, `IF(...)` folded into `LIMIT NULL`, `LIKE ANY UNNEST([...])` with a collation, an `INTERVAL` followed by a comment). They are sqlglot's own rewrites, harmless as a reading but still refused; no prover eval proves them.

- **DLBench, round trip: 1 proof.** `eval_diff` found that `BUTTERTrans/mariadb/578` (`ISNULL(1/0 = null)`, a MySQL test-suite line) is no longer proved. sqlglot prints it as `1 / 0 = NULL IS NULL`, which MySQL reads as `(1 / 0 = NULL) IS NULL` and sqlglot reads as `1 / 0 = (NULL IS NULL)`: a real sqlglot misread of its own output, caught by the round trip. The source and the translation are the same text, so the answer was right, but the proof went through the regrouped print. The floor of 109 exact pairs still holds (master measures 110, the recorded score is 109).

- **SQLSolver TPC-H, round trip: 3 proofs, a checker bug.** `eval_diff` showed TPC-H falling from 22 proved to 19. The rewritten sides use `LEFT ANTI JOIN`, which sqlglot prints as `WHERE NOT EXISTS (...)` in MySQL: a different tree by design, not a regrouping. The ANTI and SEMI joins are now exempt like `FULL JOIN`, and all 22 prove again.

A hand check of the 22 refused dev texts of the BigQuery edge-case corpus and dlbench found no false decline: each one is rejected by the engine or read differently by it.

### Proofs that relied on a misread

The BigQuery edge-case eval (`tools/bq_behavior_eval.py --corpus edge --pipeline lift`) handled 435 of 967 dev cases before the check and 417 after. The 18 cases it no longer handles are all seeded `fuzz-paren` cases whose text is one of the 20 refused above (a non-associative comparison BigQuery rejects, or a bitwise operator chain sqlglot groups differently): the pipeline's rewrite was accepted as a proof about a text BigQuery rejects or reads another way. They count as proofs that relied on a misread. VeriEQL LeetCode loses 6 proofs (4,793 to 4,787): of the 24,169 distinct texts of that suite and the literature suite, 17 are refused, 16 of them using `||` as OR (`WHERE POPULATION >= 25000000 || AREA >= 3000000`, MySQL's default reading; sqlglot reads concatenation) and one `a = b IS NULL` over a LEFT JOIN. Six of the 17 pairs had been proved, all with `||`; the other 11 were not. The sweep also found a checker bug, fixed: sqlglot's MySQL tokenizer reads `.49` as a dot and a number, and the check refused such texts as a different token split. VeriEQL Calcite-397 keeps its 335 proofs: its `a = b IS TRUE` texts are the misread KumoSQL repairs before proving (before that repair landed, the check refused 4 of them). The executed comparison was on DuckDB, so `wrong` stays 0 and no rewritten result differed. The held-out fuzz run (662 cases) went from 421 to 395 handled in the same way, with `wrong` 0; it was run once to update the recorded score and nothing was changed from it.

## Cross-version agreement

The historical cross-version run of `python tools/parse_check_matrix.py` read up to 250 dev queries of each fixture directory (2,448 dialect-and-text readings, 2,305 distinct texts) under sqlglot 26.0.0, 30.20.0, 30.21.0 and 30.21.0 with the compiled `sqlglotc`, and compares the verdict of each. Result:

| Release | Agree | Unchecked | Disagree |
| --- | --- | --- | --- |
| 26.0.0 | 1,980 | 316 | 9 |
| 30.20.0, 30.21.0 and 30.21.0 compiled | 1,982 | 315 | 8 |

The three 30.x runs give the same verdict and the same operator grouping on every text; the compiled build never differs from the pure one. Under 26.0.0 the verdict differs on 2 texts:

- **`DATE_TRUNC(d, WEEK(TUESDAY))` (BigQuery).** sqlglot 26.0.0 reads the whole unit as one string literal, `'WEEK(TUESDAY)'`, that has no source token, so the check reports a disagreement. This is a refusal, not a proof, and only under 26.0.0.
- **A MySQL column named `CASE`** (a Calcite SPES text). sqlglot 26.0.0 does not parse it, so it is unchecked and no prover reads it; the 30.x releases parse it and the check agrees.

Two more texts group operators differently in the releases' own trees while every release agrees with the independent reading: `NET.HOST(...)` and `SAFE.LOG(...)`, which 26.0.0 builds as a dotted access around a call and 30.x as one function name. No operand is regrouped, so this is harmless.

The first run of the matrix found that byte and raw string literals (`b"..."`, `r'...'`) had no positions under 26.0.0, which refused 6 more texts there. The positions hook now covers them.

## Limits

- The check covers the text each prover entry point receives. A layer that prints sqlglot trees and proves the printed text (a pipeline stage, a rewrite) is checked on what it hands the prover; a misread of the original text above it is caught only where the original is also passed to a prover.
- Counterexamples and refutations are not checked. A misread could in principle produce a wrong `not_equivalent`; the executed comparisons that back those run the original text on an engine.
- Unchecked constructs (see above) and dialects without a table keep sqlglot's reading. 2,565 of 7,868 GoogleSQL compliance queries are unchecked, most of them syntax sqlglot does not read (the prover declines those anyway).
- Positions for `NULL`, `TRUE` and `FALSE` are placed by order; a tree that visits its names out of text order keeps no positions for them.
- MySQL's rejection of `FULL JOIN` and DuckDB's parser as of 1.5.6 are not modeled beyond what is listed.
- `ast_utils.check_distinct_from_grouping` is unchanged; this check generalizes it.

## Held-out exposure

Possible "tuned on test" exposure, recorded for the held-out rule: an early sweep printed disagreements from all of `tests/fixtures`, which may have included calcite_mined pairs with `new=True`, one sweep printed 20 queries of the held-out BigQuery edge-case file, and the held-out fuzz eval was run once to re-measure its score (counts only). No checker change was made from those queries, and every fix since came from dev cases, the random expressions of `parser_oracle.py` or the injected faults. The sweep tools skip held-out files and rows.

## Tools

| Tool | Use |
| --- | --- |
| `tools/parser_oracle.py duckdb\|mysql` | Run random expressions on the engine and count misreads, caught and table errors. MySQL needs a server (`KUMOSQL_MYSQL_SOCKET`, `pymysql`). |
| `tools/parser_oracle.py bigquery-emit` / `bigquery-check` | Write the BigQuery query and check the rows it returned. |
| `tools/parse_check_sweep.py dir:dialect ...` | Count agree, unchecked and disagree over a fixture directory. `SHOW=n` prints n disagreements. |
| `tools/parse_check_matrix.py` | Compare readings across sqlglot releases and the compiled build. |
| `tests/test_parse_check.py`, `tests/test_parse_check_faults.py` | The misreads above as cases, the hook, and fault injection. |
