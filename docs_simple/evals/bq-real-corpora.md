# Testing real BigQuery projects

[Simple eval index](README.md) · [Full reference](../../docs/evals/bq-real-corpora.md)

Small syntax examples cannot capture everything people write in projects. This suite reads pinned open-source Dataform projects and BigQuery SQL, with their licenses preserved.

It includes operation scripts, JavaScript-generated SQL, machine-learning statements, wildcard tables, nested values, and project layouts. dbt/Jinja projects are excluded because KumoSQL does not render Jinja.

Thirteen projects are in it. The five added most recently are Dataform's own Stack Overflow example, Google's marketing-analytics and security-analytics Dataform projects, and two parts of Google's bigquery-utils repository (five large audit-log views, and a small data-vault project). For example, the marketing project writes the dataset of a table as a call into its own code, `functions.baseSchema("ga4")`. Two tables with the same name in different such datasets used to overwrite each other, so more than half of that project's references pointed nowhere; the loader now tells them apart by the text of the call.

## What is measured?

The suite checks loading, cleanup, formatting, literal dependencies, unchanged quoted names, analysis gaps, and stage timings. A cleanup or format change accepted without a proof is a failure.

Coverage asks how many statements and columns are traced and how many files finish without blocking gaps. “No failures” does not mean every template or column was resolved.

Three references in the marketing project still count as failures: they point at tables that a JavaScript file publishes, and KumoSQL does not run that JavaScript, so those tables are not part of the graph. The score is reported with the 3 failures, not hidden.

A fifth of the files, picked by a hash of their name, is scored apart as a held-out set. For the five newest projects this is not a clean test: the bugs were fixed while looking at every file's failures, held-out ones included, so the page marks it “tuned on test”. The first eight projects were never held out. The recorded scores, per-project table and the before-and-after numbers for the five newest projects are in the full guide.

From a development checkout:

```sh
python tools/bq_corpus_bench.py
```

This analyzes source files; it does not establish that all project outputs were executed in native BigQuery. The full guide lists the projects, fixture-refresh process, known gaps, and recorded results.
