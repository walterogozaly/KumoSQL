# Reusing a model you already have

[All simple guides](README.md) · [Full reference](../docs/model-reuse.md)

Before building a new table, ask whether an existing table can supply the result. KumoSQL separates three questions.

## 1. Can I read an existing model instead?

The helper `rewrite_over_model` proposes a query reading the model and checks it against the original with the model's SQL expanded. It returns a replacement only when the prover establishes equivalence.

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

### Joins that change nothing

Suppose a model joins orders to customers, and your query reads only orders. If every order must have a customer (a declared foreign key onto the customer's unique id, and the order's customer column cannot be empty), that join neither drops nor repeats an order, so the model's rows are your orders. KumoSQL uses this: it leaves such joins out when it compares the query with the model, then lets the prover check the answer. The same holds for a `LEFT JOIN` onto a unique key that nothing reads. If the customer column can be empty, the join quietly drops those orders, so the query must also say `customer IS NOT NULL`. A filter on the customer, a join on a column that is not unique, or no declared constraint at all means the join stays and the model cannot be used.

A grouped model can also answer a query that joins one more table, for example sales by product joined to product names: the model's rows are joined to the names and summed again. A total over a column of the extra table (say a budget) is multiplied by the model's row count first.

These checks depend on the schema, declared constraints, and the features the prover supports. The full guide contains API signatures and regression examples. [Pipeline analysis](pipeline-analysis.md) explains how to find candidate overlaps and rollups across a project.
