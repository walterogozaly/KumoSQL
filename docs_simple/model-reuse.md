# Reusing a model you already have

[All simple guides](README.md) · [Full reference](../docs/model-reuse.md)

Before building a new table, ask whether an existing table can supply the result. KumoSQL separates three questions.

## 1. Can I read an existing model instead?

The helper `rewrite_over_model` proposes a query reading the model and checks it against the original with the model's SQL expanded. It returns a replacement only when the prover establishes equivalence.

For example, a model containing all orders with customer IDs might supply a query filtering those orders to one customer. A model that discarded a needed column cannot supply it.

### When the model covers only part of what you need

Suppose the model keeps orders numbered below 3 and you want orders below 6. The model still holds half the answer. KumoSQL can read the model for the rows it has and read the base table only for the rest (orders 3 to 5), then add the two together. For a summary model the two partial sums or counts are added again. The rest is chosen with "is not true" rather than "is false", so rows where the model's condition is unknown (NULL) are not lost.

This proposal is off unless you ask for it, because the answer still reads the base table. It is proven like every other rewrite, and it is declined when the model lacks a needed column, when an average has no sum and count to combine, or when the model dropped groups. The recorded scores are in the [full reference](../docs/model-reuse.md).

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
