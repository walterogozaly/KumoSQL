# Project reduction: the smallest Dataform project that keeps chosen outputs

Point KumoSQL at a Dataform project and name the outputs you need. It returns the smallest project it can **prove** still produces each of them under the same name with the same output (the same columns in the same order and the same rows), as a patch on the project's `.sqlx` files:

- actions no kept output needs are deleted, with the declarations and assertions that only they used;
- single-use intermediates are folded into their readers, equal tables merged, columns nobody reads pruned;
- a query repeated in several places becomes one new shared table;
- each remaining query is simplified.

Every step is kept only when each kept output is proved equal to the original. A step the prover cannot prove is rejected, so the worst outcome is the project with only the unneeded actions removed. The patch is then applied to a copy of the project, loaded again and every kept output re-proved before it is returned.

## Use it

- **CLI:** `python -m kumosql reduce-project DIR --keep NAME [--keep NAME ...]` prints the result as JSON and changes no file (preview by default). `--patch FILE` (or `--patch -` for stdout) writes the unified diff, which `git apply` accepts; `--write` is the only option that edits `DIR`: it applies the patch (only when it verified). Other options: `--drop-only` (delete what the kept outputs do not need and rewrite nothing), `--keep-assertions`, `--strict`, `--no-factor`, `--table-type view|table` (the type of new shared tables), `--max-seconds` (default 300), `--timeout-ms` (per proof). The exit code is 0 when the reduction verified, 1 when it did not and 2 on bad input.
- **Python:** `kumosql.project_reduction.reduce_project(root, keep, ...)` returns a `ProjectReduction`: `.files` (each a `FileChange` with `path`, `action` add/modify/delete, `before`, `after` and `.diff()`), `.patch()`, `.apply(root)`, `.proofs` (per kept output: `unchanged` or `proved`, with the prover's assumptions), `.removed` and `.dropped_assertions` (each with the reason), `.changed`, `.added`, `.fixed` (actions kept exactly as written, with the reason), `.checks`, `.moves`, `.rejected_moves`, `.score_before`/`.score_after`, `.actions_before`/`.actions_after`, `.verified`, `.notes` and `.to_json()`. The JSON carries the [Shared models](shared-models.md) patch shape: `diff`, `changed_files`, `verdict` (`proven`, `proven_with_assumptions` or `unknown`), `assumptions`, `diagnostics` and `checks` (one per kept output, role `kept`, and per surviving table with assertions, role `checked`). `source_columns` adds columns and keys of declared sources (the format `minimize_tables` takes) when the project does not say them.
- **Kept outputs** are named by key (`project.dataset.name`), `dataset.name`, name or file path. An unknown or ambiguous name, or a declaration, raises `ReductionError`.

The score is `project_score`: the [table-minimization complexity](table-minimization.md#complexity) of every action's SQL plus one per action. Declarations are not actions.

## What stays, what goes

An action stays when a kept output reads it, directly or through other actions, or lists it in `dependencies`. An operations script (or a model's `pre_operations`/`post_operations`) stays when what it writes is not known, when it writes a table a staying action reads, or when it creates a function or procedure a staying action calls; one whose writes are all known and unread is deleted. A declaration stays when a staying action reads it.

These are never rewritten. They stay exactly as written, and every table they read is kept and proved unchanged:

- incremental tables, whose rows depend on earlier runs;
- operations, and models with `pre_operations` or `post_operations`;
- models whose `${...}` expressions depend on the file they are written in (`self()`, `when(incremental(), ...)`, constants from a `js` block);
- models that are not a single query;
- actions that JavaScript in `definitions/` names, and actions another staying action lists in `dependencies` (these keep their output, but may be rewritten).

`${dataform.projectConfig.vars.X}` and constants from `includes/` mean the same in every file, so models that use only these can be folded and moved. The prover treats each one as an unknown constant: a query with a project variable is never taken to equal one with the variable's current value, since a run can override it.

A variable used as a **name** (`FROM ${vars.schema}.orders`) is one project-wide token, so two models that write it mean the same table. A variable used as a **value**, a string literal that is exactly one expression (`WHERE status = "${vars.paid}"` or `'${vars.paid}'`), is replaced before anything is proved by a call to an unknown zero-argument function, `__kumo_v_<hash>_<quote>()`. The prover reads such a call as an uninterpreted value that is the same wherever it is written (equal expressions are equal), may be NULL, and is not equal to any literal, so no rule can fold it, compare it with `'paid'` or call `status = <variable> AND status = 'paid'` empty. It is written back as the original text, quotes included; `'${v}'` and `"${v}"` are two unknowns, so they are never merged. Two things stay as written, with the reason in `fixed`: a variable inside a longer string (`"pre_${vars.x}"`, which is text built from the value) and one in a raw, bytes, triple-quoted or adjacent string, or after a type name (`DATE '${vars.d}'`). A variable written without quotes (`amount > ${vars.min}`) is not a literal either: the prover reads the token as one more column it knows nothing about, and a proof over it holds for any value per row, so also for one constant per run. Every proof that rests on a variable used as a value carries the assumption that it holds one plain string (no quote, backslash or SQL in it), since a value that closes the quote would change what a rewrite means.

**Assertions.** A standalone assertion that reads only kept outputs and actions kept as written stays as it is. Other assertions are deleted and listed in `dropped_assertions` with the reason, unless `--keep-assertions` (then each must keep returning the same rows, so the tables it reads can be rewritten only when that is proved) or they are named in `--keep`. An assertion a staying action lists in `dependencies` is kept the same way. A table with `assertions` in its config that survives must stay proved equal on the columns it keeps, and columns its config names are never pruned; when it is folded away, its config assertions are listed as dropped, unless an action waits for them in `dependencies` (then the table stays). `--strict` asks the same of every surviving table.

When a table that another action's `dependencies` names is folded into a reader, the reader inherits the entry, so it still waits for the same assertions.

## How the patch is written

- A changed file keeps every `config`, `js`, `pre_operations` and `post_operations` block and every leading comment byte for byte; only its SQL is replaced. Tables are written with `ref()` as the project already writes them (`${ref("name")}`, or with the schema when the name is ambiguous), and `${...}` expressions are restored as they were.
- A new shared table is a file next to its first reader, with that reader's `database` and `schema`, the tags of every reader, and type `view` unless `--table-type table`. It is named after the CTE it replaces when there is one.
- Deleted actions, assertions and declarations are deleted files.
- The diff carries git's `new file mode` and `deleted file mode` lines, so `git apply` creates and deletes the files.

To move one repeated CTE into a shared model and change nothing else, use [Shared models](shared-models.md); project reduction finds shared tables as one of its steps.

## Under the hood

`reduce_project` works out the needed actions from the dependency graph, then hands their SQL to [`minimize_tables`](table-minimization.md) with the kept outputs protected, the actions kept as written passed as `fixed`, tables with config assertions as `checked`, `factor=True` and `lower_score_only=True` (a rewrite has to lower the score; equal-score rewrites would only churn the files). Masked `${...}` expressions become one token per distinct expression across the project, so the prover sees the same name or constant wherever the same expression is written. The prover itself also refuses a string literal that holds one of these tokens (`kumosql.sqlx_fragments.masked_template_problem`), so a literal the reduction did not replace can never be read as a fixed string.

Factoring finds a query that is written in at least two places (a derived table in `FROM` or `JOIN`, a top-level CTE, or a whole table), with the same canonical form (table aliases and column qualifiers do not matter) and output names, that stands on its own (it reads only tables and sources whose columns are known). It moves the query into a new table and replaces each copy with a read of it, or reuses an existing table that already returns it. The step is proved like any other.

## Eval

The [project-reduction eval](evals/project-reduction.md) converts every table-minimization case into a Dataform project with assertions, a project variable, incremental tables and `dependencies` mixed in, checks each patched project on DuckDB, and runs the open-source Dataform projects in `tests/fixtures/bq_corpora` (`python tools/reduction_bench.py`).

## Limits

- Greedy search, bounded by `--max-seconds`: a good answer, not always the smallest.
- Proofs are as sound as KumoSQL's prover, and steps it cannot prove are rejected and listed in `rejected_moves`. Known false proofs still open in the prover apply here too.
- A project variable used as a value is assumed to hold one plain string. A value that contains a quote or SQL (`x' OR '1'='1`) would make the original query mean something else than any rewrite of it, so the proofs list that assumption. The variable's type does not matter to the proof: it is an unknown value, never folded, so `CAST`, `LIKE` and `IN` over it are kept as written or proved only when both sides hold the same call.
- The project is read the way KumoSQL's loader reads it: tables named in JavaScript outside `definitions/*.js` are not seen, and a `schema` set from a project variable resolves to the default dataset.
- Rewritten SQL is pretty-printed by sqlglot and tables may be qualified by their full name where the original used an alias.
