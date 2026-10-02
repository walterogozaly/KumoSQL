# Multi-statement scripts

BigQuery scripts (several statements run together, with variables, temporary tables, control flow and stored procedures) are broken apart wherever SQL comes in, and what they read and write is followed from statement to statement. The same code (`kumosql.scripts`) serves every entry point, so a script is understood the same way in each:

- a model written as a script in a `.sql` file or a Dataform `operations` action, and its `pre_operations` and `post_operations` blocks;
- stored-procedure bodies (`CREATE PROCEDURE`), when a script `CALL`s them;
- job history: a script's parent job and its child jobs (about 25,000 jobs in a large project) become one set of edges with the temporary tables folded away;
- observed usage (queries that are scripts are split before each statement is examined), the rewrite engine (rules apply inside blocks, one leaf statement at a time) and pasted SQL.

## Splitting

KumoSQL has its own small lexer for this because sqlglot's BigQuery tokenizer treats `BEGIN` as a command and swallows the rest of the text. The lexer knows strings (single, double, triple-quoted, raw), comments (`--`, `#`, `/* */`), backticked names, and nested `BEGIN ... END`, `IF`, `CASE`, `LOOP`, `WHILE`, `REPEAT` and `FOR` blocks. A semicolon inside any of them never splits a statement. `split_script(text)` returns the statements with blocks opened; `parse_script(text)` keeps the nesting.

## What is kept, ignored and unknown

| | Statements |
| --- | --- |
| Kept | queries, `CREATE [TEMP] TABLE/VIEW ... AS`, `INSERT`, `MERGE`, `UPDATE`, `DELETE`, `EXPORT DATA ... AS SELECT` (reads only), `CALL` of a procedure defined in the project or script, `EXECUTE IMMEDIATE` of literal text (also `||` and `CONCAT` of literals) |
| Ignored | `DECLARE` and `SET` of scalars, `ASSERT`, transactions, `LOAD DATA`, DDL such as `ALTER` and `DROP`, `RAISE`, `RETURN`, `LEAVE`/`ITERATE`/`BREAK`, and the shell of `IF`, loops and `BEGIN ... END` |
| Unknown | `EXECUTE IMMEDIATE` of dynamic text (a variable, `FORMAT`, concatenation with a variable), `CALL` of a procedure nobody defines, a statement that does not parse |

Unknown is never guessed: a dynamic statement adds no edges, and the script is reported incomplete.

## Following data between statements

- **Temporary tables.** `CREATE TEMP TABLE t AS SELECT ...` remembers what it was built from. A later table written from `t` traces to the real sources, and the temporary table is spliced into the final query as a common table expression, so column lineage reaches real columns. Redefinitions (`CREATE OR REPLACE TEMP TABLE t AS SELECT ... FROM t`) are versioned, and `INSERT` into a schema-only temporary table adds its sources by column.
- **`UPDATE` or `MERGE` on a temporary table** makes it opaque: tables it was fed from remain dependencies at table level, but its columns are reported unknown with reason `temporary_table`.
- **Script variables.** `DECLARE x DEFAULT (SELECT ... FROM t)` and `SET x = (SELECT ...)` carry their source tables to the statements that use `x` (edges are marked `via_variable`). A variable that no statement reads adds no edge.
- **Branches and loops.** A statement inside `IF`, `CASE`, a loop or an exception handler is a possible edge, so every arm counts. Loop bodies are followed once.
- **Procedures.** `CALL` expands the body with parameters as variables; recursion is cut at depth 8. A procedure that is defined but never called adds nothing.
- **`EXECUTE IMMEDIATE`.** Literal text is read as the statement it holds; anything computed at run time is unknown.
- **Job history.** A `MERGE`, `INSERT`, `UPDATE` or `DELETE` job that carries its query text but no destination table gets its destination and sources from the statement, so a MERGE-built table (every Dataform incremental run) has its upstream edge in job-history analysis. `expand_script_jobs` joins child jobs to their parent script and replaces anonymous temporary datasets (leading underscore) with the real tables they were built from, ordered by creation time.

## MERGE

A `MERGE` is read as the values that flow into the target's columns: each `WHEN` clause becomes one arm of a `UNION ALL` that selects what the clause assigns from the `USING` source (a table, a subquery with CTEs, `UNNEST`, or a temporary table of the script), joined to the target on the `ON` condition and filtered by the clause's own `AND` condition. The ordinary column tracing then does the rest.

| Clause | Target columns | Read |
| --- | --- | --- |
| `WHEN MATCHED ... UPDATE SET c = e` | `c` from the columns of `e` (`t.v`, `v` and a struct field `t.a.b` all name the root column) | `ON`, the clause condition, any subquery in them |
| `WHEN NOT MATCHED [BY TARGET] ... INSERT (c) VALUES (e)` | `c` from `e`, paired by position; bare names are the source's | the same |
| `... INSERT ROW` | every source column to the target column of the same name | the same |
| `WHEN NOT MATCHED BY SOURCE ... UPDATE SET` | `c` from `e` (constants stay constants) | `ON` and the condition, including target columns |
| `... DELETE` | none | the condition and `ON` |

Several clauses union their sources per column. A script's last unconditional `MERGE` defines its output columns like a final query; earlier ones keep their table dependencies and are counted in `skipped_statements`. An operation that merges into its own table is traced the same way. Only the written columns are known, so the merged table keeps its schema from elsewhere and a `SELECT *` over it is never narrowed to them. Not understood means unknown: `INSERT VALUES (...)` without column names, `INSERT ROW` mixed with explicit columns (by-name, conservative), a delete-only `MERGE` (no columns to trace) and anything that does not parse keep the tables as dependencies, claim no columns and are reported as a gap.

## Diagnostics

Nothing in the UI explains scripts. Each script gets an informational `script_summary` diagnostic with counts (for example `script of 12 statements: 7 kept, 5 ignored, 1 unknown (execute_immediate x1)`), never SQL text. Two codes block completeness: `skipped_statements` (columns are traced for the final query and the temporary tables it reads, so column reads by other statements are missing) and `unparsed_operation` (some pre/post operations could not be read). The graph, impact and dead-column views treat a model with either code as incomplete, as for any other gap.

## Limits

- Columns of a temporary table that `UPDATE`, `MERGE` or `DELETE` changed are unknown; only tables are traced.
- A variable edge can be false if a column of the same name shadows the variable.
- Loop bodies are followed once; a loop whose body depends on the iteration reads the same tables each time anyway, but dynamic table names inside it are unknown.
- Pre/post operations that stay as unresolved Dataform templates are reported at info level, not guessed.
- `EXPORT DATA` is kept for its reads only; the destination is not an edge.

## Evaluation

`python tools/script_bench.py [--cases 100] [--seed 1] [--write-results]` builds scripts from specs in Python, so the answer is known by construction and no parser produces the key: tricky splitting (strings with semicolons, triple-quoted and raw strings, comments), temporary-table chains, redefinitions, `INSERT` into temporary tables, `UPDATE`/`MERGE` on them, `MERGE` into real tables with random clauses (updates, inserts, `INSERT ROW`, deletes, `BY SOURCE`) over a table or a subquery, variables, branches, loops, exception handlers, literal and dynamic `EXECUTE IMMEDIATE`, procedures, ignored statements and random mixes of those. A job-history family turns scripts into child jobs with anonymous temporary tables and shuffled order. A public family runs the 22 scripting examples in `tests/fixtures/bq_syntax/sql/script` against hand-written labels in `benchmarks/script_cases/public_scripting.json`.

Scored apart: **correctness** (an edge, read or column the key lacks, a phantom table from a comment, string or ignored statement, or a guessed dynamic statement; all must be 0), **coverage** (exact, or unknown where the key says unknown) and **analysis** (edge and column precision and recall). Current result: 1,622 of 1,622 exact, 0 wrong (1,400 development-family scripts including 100 `MERGE` statements, 100 mixed, 100 job-history, 22 public examples); edges 3,693 of 3,693, 1,062 of 1,062 traced columns exact (temporary-table chains and `MERGE` targets, with `ON` and condition columns counted as read). Caveat: the families are ones the feature's author could think of and were tuned against while building it; there is no held-out family. `tests/test_script_bench.py` holds the floors; the numbers live in `benchmarks/results/script-splitting.json` and feed the README scoreboard.
