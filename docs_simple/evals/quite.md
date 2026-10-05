# Checking what language models do to SQL

[Simple eval index](README.md) · [Full reference](../../docs/evals/quite.md)

Several research systems ask a language model to rewrite a slow SQL query into a faster one. The QUITE paper published every rewrite that 13 such systems produced for four public benchmarks (TPC-H, DSB, Calcite's test queries and StackOverflow queries from SQLStorm), together with a flag saying whether the rewrite returned the same rows as the original on the authors' PostgreSQL database. This eval takes those published pairs and runs them through KumoSQL's checkers. No model is called while it runs.

## What it checks

- **Flagged equal.** Can the prover show the rewrite is equivalent to the original for every possible database? A proof is the strongest answer. When the prover cannot decide, the answer is "unknown", which is allowed.
- **Flagged unequal.** The authors' flag says the rewrite failed. KumoSQL must not prove such a pair equal when a database exists that separates the two queries. Where it can build a small database on which DuckDB returns different rows, with DuckDB's optimizer turned off to rule out an optimizer bug, the pair counts as refuted.
- **Zero wrong.** A proof that a replayed database contradicts would be a wrong answer. None is allowed.

## A concrete example

A rewrite replaces `FROM emp, dept WHERE emp.deptno = dept.deptno` with `FROM emp JOIN dept ON emp.deptno = dept.deptno`. KumoSQL proves the two spellings return the same rows. The authors also document a TPC-H rewrite that drops a filter on the `region` table yet returns the same rows on their database. KumoSQL builds a database in which a supplier outside Europe ties the cheapest European supplier, the two queries then return different rows, and the pair is refuted.

## Why a flag is not the truth

The flag comes from running both queries once, on one database. Two queries can agree on that database and still differ on another, as the TPC-H rewrite above does. A pair is also flagged unequal when a query timed out after 300 seconds or when the rewrite failed to run, and a rewrite that failed to run on PostgreSQL can still be equivalent as written. So KumoSQL never reads the flag while deciding; it only uses it afterwards, to sort the pairs into the two groups above.

## Limits of the evidence

- The authors' databases are not public. A refutation uses a database KumoSQL builds, so a flagged-equal pair that is refuted is a disagreement about one database, not a mistake by either side.
- Most pairs that are not proved end as "unknown", which is not a failure: many LLM rewrites need reasoning the prover does not have.
- Results are compared as bags of rows. Where a query keeps only the first rows by a sort key that has ties, either answer can be correct, and the full reference counts the pairs this applies to.
- The data has no licence. It is downloaded from a pinned version when the eval runs and is never stored in this repository.
- One fifth of the queries, chosen by hash, was held out and never inspected case by case. No KumoSQL rule was written or changed for this eval, but two small harness readings were adjusted on development pairs; the full reference lists both.

The recorded scores, the pinned version and file hashes, the pair counts per benchmark and the overlap with other evals are in the [full reference](../../docs/evals/quite.md) and the two `quite-*` results files.

```sh
python tools/quite_bench.py --sample 48
```
