# Dataform preservation eval

[Plain-language version](../../docs_simple/evals/dataform-bench.md)

Rewrites must never touch what they do not understand. This eval builds SQLX files from parts whose role is known and scores three things apart. No language model runs at evaluation time.

`python tools/dataform_bench.py [--scale] [--write-results]`. The floors are in `tests/test_lineage_benchmarks.py`; the scoreboard row is `benchmarks/results/dataform-preservation.json`.

## What is built

Each file has a `config` block (with nested braces, braces and apostrophes inside strings and comments, `columns`, `bigquery`, `assertions`, `tags`), and some of: `js` blocks (template literals, comments with braces, several blocks), `${ref(...)}` in every form (`"a"`, `'a'`, `"schema", "a"`, `{schema, name}`, `ctx.ref`, extra spaces), nested interpolation (`${when(incremental(), \`... ${self()} ...\`)}` and a `ref()` inside it), `pre_operations` and `post_operations` with `${self()}` and `${ref()}`, incremental branches, templates KumoSQL cannot resolve (`FROM ${tbl}`, `${ref(variable)}`, `${helpers.table('x')}`, a computed ref), and text forms (CRLF, a byte-order mark, tabs, unicode). The `declared_refs` family reads tables declared outside the default schema or database, by a `declaration` sqlx file, a `declare()` call, a loop over a literal list, or constants; the expected dependency is the exact `database.schema.name`. A declaration computed in JavaScript must leave the ref unresolved and flagged, never the default schema. The SQL part holds a subquery and a trivial predicate that rewrite rules can fix.

## What is scored

| | Measure |
| --- | --- |
| Correctness | Every registered rewrite rule is applied to every file. Each protected span (config, js, pre/post operations, each `${...}`) must come out byte for byte and the same number of times. Also: crashes, dependencies claimed that the file does not have, and templates that cannot be resolved but were neither flagged nor left alone, and any column called dead near one. All must be 0 |
| Analysis quality | Dependency precision and recall against what Dataform evaluates: every `ref()` while compiling any part of an action (the query, `pre_operations`, `post_operations`, `when(...)` arguments, config `dependencies`); `resolve()` and `self()` are not dependencies |
| Coverage | Templates resolved, templates flagged as unsupported, and SQL parts still fixed by a rewrite around protected text |
| Performance | Seconds to load 500 and 2,000 files |

**Score: 7,032/7,032 protected-text checks kept, 0 damaged; 370/370 dependencies found, 0 wrong; 36/36 unresolvable templates flagged; 216/216 fixable files still rewritten around protected text.** Loading takes 2.1 s for 496 files and 7.7 s for 2,000.

**Held-out families, first run** (144 files): 0 protected spans damaged and 0 wrong dependencies, but dependency recall was 108/144: every `ref()` inside `pre_operations` was missed. Fixed afterwards, so those families no longer count as held out. Before the dev fixes below, the dev families rewrote 108 of 144 fixable files; the rest were declined safely.

## Bugs found and fixed

1. `ref()` inside `pre_operations` or `post_operations` was not a dependency (only the SQL sections were read), so the graph and change impact missed the edge.
2. A table named by an unresolved template (`FROM ${tbl}`) showed up as an external table called `__sqlx_token_000__`, an internal placeholder. It is now an `unresolved_template` gap ("Template not resolved"): its dependency is unknown, it is an unknown reader for every change, and no column in the pipeline is called dead while it exists.
3. `WHERE a > 0 ${when(incremental(), \`AND b > 1\`)}`, the usual incremental idiom, could not be parsed around, so every rule declined the file. The placeholder now continues the condition and the rules rewrite the SQL around it; the `${...}` text still comes out byte for byte.

4. `ref()` to a table declared in a `.js` file (`declare(...)`, in a loop or in `includes/`) fell back to the default schema and named the wrong table. Declarations are now read from JavaScript; a ref KumoSQL cannot settle (a name declared in two schemas, or any unlisted name while a file declares tables by computation) is left unresolved, or settled from Dataform's own compilation when credentials allow (see docs/dataform-repositories.md). This family was added with that fix and is a dev family.

## Limits

Files are generated from templates the author wrote; real projects have more shapes. Dev families were tuned against. The dependency truth is how Dataform compiles an action, written from its documented behaviour, not by running Dataform.
