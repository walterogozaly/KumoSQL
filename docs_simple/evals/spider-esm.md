# Checking Spider's hand-labelled "this is also correct" queries

[Simple eval index](README.md) · [Full reference](../../docs/evals/spider-esm.md)

Spider is a benchmark where a model turns a question into SQL. Its usual metric compares the pieces of the predicted query with the pieces of the answer key, so it marks many correct queries wrong. The authors of TestSuiteEval looked through those cases and listed 558 predictions they judged equivalent to the answer key, with a short reason each. This eval asks KumoSQL about each pair. No language model is called.

## What it checks

- **Proved.** The prover shows the two queries return the same rows for every possible database. KumoSQL is given the column types and nothing else about Spider's keys, so this only happens when the equivalence does not depend on them.
- **Refuted.** KumoSQL builds a database that follows the schema (keys unique, foreign keys pointing at real rows) and finds one on which the two queries return different rows. Here the authors said "equivalent", so this is a **dispute about the label**, not a mistake by KumoSQL. Usually the label assumes something the schema does not say, for example that a foreign key is never empty.
- **Zero wrong.** Every proof is checked again on many generated databases. A proof that a database contradicts would be a wrong answer. None is allowed.

## A concrete example

The answer key reads `SELECT Other_Details FROM Paragraphs WHERE paragraph_text = 'Korea'` and the prediction adds a join with a parent table. The authors say the join is redundant because the key links each row to a parent. The schema lets that link be empty, so KumoSQL builds a database where one row has no parent: the join drops it, the answer key keeps it, and the pair is a label dispute.

## Limits of the evidence

- Spider's own databases are not reachable from here, so every refutation uses a database KumoSQL builds from the published schema. It shows what the declared schema allows, not what Spider's data happens to contain.
- Most of the pairs are not proved. That is expected: the labels lean on facts the schema does not declare.
- The 558 rows are the authors' own picks of false alarms from one metric. They say nothing about how often a wrong prediction would be accepted.
- The data has no licence, so it is downloaded from a pinned version when the eval runs and is never stored in this repository. One fifth of the pairs was held out and scored once at the end.

The recorded scores, the pinned version and file hashes, and the causes of the label disputes are in the [full reference](../../docs/evals/spider-esm.md) and the `spider-esm` results file.

```sh
python tools/spider_esm_bench.py
```
