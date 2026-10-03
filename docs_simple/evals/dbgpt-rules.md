# Checking DB-GPT's rewrite examples

[Simple eval index](README.md) · [Full reference](../../docs/evals/dbgpt-rules.md)

DB-GPT supplies PostgreSQL before/after examples for its query rewriter. The originals lack schemas and correctness labels, so this evaluation adds reviewed schemas and labels and checks them on DuckDB.

Some examples preserve results. Others are invalid or change meaning: splitting an OR condition into UNION ALL can return a matching row twice.

## How it decides

The algebraic prover uses the supplied keys and non-NULL facts under exact arithmetic for these integer schemas. Otherwise, a confirmed DuckDB difference can refute the pair. Cases that cannot be decided stay unknown.

Output names are ignored and results are compared as bags. A pair can therefore agree here while changing ORDER BY behavior. Read that comparison policy before applying a fix.

```sh
python tools/dbgpt_rules_bench.py
```

Run from a development checkout. The full guide describes invalid cases, labels, execution cross-checks, adaptations, and scores. Random agreement supports the reviewed labels on tested data; it is not universal proof by itself.
