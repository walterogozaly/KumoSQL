# Reusing a model you already have

[All simple guides](README.md) · [Full reference](../docs/model-reuse.md)

Before building a new table, ask whether an existing table can supply the result. KumoSQL separates three questions.

## 1. Can I read an existing model instead?

The helper `rewrite_over_model` proposes a query reading the model and checks it against the original with the model's SQL expanded. It returns a replacement only when the prover establishes equivalence.

A model can also be a union, or an `INTERSECT ALL` or `EXCEPT ALL` of two queries. KumoSQL then compares the query with each part of the model and proposes reading the model with the query's filter. Because these operations count duplicates, a query such as "all names" is not answered from a model that is "names minus the names dependents also have". An operation that removes duplicates (`UNION`) is answered from a duplicate-keeping model (`UNION ALL`) by adding `DISTINCT`. The evidence is the prover plus the random-database check, so a pair it cannot read is left alone. Details: [full reference](../docs/model-reuse.md).

For example, a model containing all orders with customer IDs might supply a query filtering those orders to one customer. A model that discarded a needed column cannot supply it.

## 2. Is one result contained in another?

Containment asks whether every row from query A is also returned by query B.

There are two versions:

- **Set containment** ignores how many copies of a row exist.
- **Bag containment** also requires B to return at least as many copies as A.

`SELECT x FROM t` can be set-contained in `SELECT DISTINCT x FROM t`. Bag containment can fail because DISTINCT removes copies. The `check_containment` result names its semantics and evidence. Unknown means containment was not established.

## 3. Can a finer summary build a coarser one?

Daily sales can build monthly sales by summing daily sums. Counts can be added, and minimums and maximums can be combined.

Averages need the sum and count: averaging daily averages gives the wrong weight when days have different numbers of sales. Distinct counts usually cannot be added because the same value may appear in several groups.

This rebuilding is called aggregate decomposition. The report checks whether the existing summary kept enough information, then proves a supported replacement or declines it.

These checks depend on the schema, declared constraints, and the features the prover supports. The full guide contains API signatures and regression examples. [Pipeline analysis](pipeline-analysis.md) explains how to find candidate overlaps and rollups across a project.
