# Testing real BigQuery projects

[Simple eval index](README.md) · [Full reference](../../docs/evals/bq-real-corpora.md)

Small syntax examples cannot capture everything people write in projects. This suite reads pinned open-source Dataform projects and BigQuery SQL, with their licenses preserved.

It includes operation scripts, JavaScript-generated SQL, machine-learning statements, wildcard tables, nested values, and project layouts. dbt/Jinja projects are excluded because KumoSQL does not render Jinja.

## What is measured?

The suite checks loading, cleanup, formatting, literal dependencies, unchanged quoted names, analysis gaps, and stage timings. A cleanup or format change accepted without a proof is a failure.

Coverage asks how many statements and columns are traced and how many files finish without blocking gaps. “No failures” does not mean every template or column was resolved.

From a development checkout:

```sh
python tools/bq_corpus_bench.py
```

This analyzes source files; it does not establish that all project outputs were executed in native BigQuery. The full guide lists the projects, fixture-refresh process, known gaps, and recorded results.
