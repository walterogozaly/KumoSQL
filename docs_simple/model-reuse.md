# Reusing a model you already have

[All simple guides](README.md) · [Full reference](../docs/model-reuse.md)

Before building a new table, ask whether an existing table can supply the result. KumoSQL separates three questions.

## 1. Can I read an existing model instead?

The helper `rewrite_over_model` proposes a query reading the model and checks it against the original with the model's SQL expanded. It returns a replacement only when the prover establishes equivalence.

For example, a model containing all orders with customer IDs might supply a query filtering those orders to one customer. A model that discarded a needed column cannot supply it.

Outer joins work too. Suppose the model is `orders LEFT JOIN customers` (every order, with its customer when there is one). A query that wants only orders with a customer and a region of 'EU' can read the model, keeping the rows where the customer columns are present and the region matches. A query that wants the orders without a customer reads the other rows. A query that wants customers even when they have no order cannot be read from that model, because those rows were never kept, and KumoSQL says no rather than guess. Both the query and the model must join the same tables for now, and the model must expose a column that is never empty when its table is present, to tell the two kinds of row apart. The proof step is the same, so a wrong proposal is never returned. The full guide has the rules and the scores on the development split; they are not held-out numbers.

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

## How the reuse check is tested

The reuse check is scored on four sets of materialized-view cases: Calcite's own tests, cases written for shared models, Apache Doris's outer-join view tests, and a set written for KumoSQL about outer joins, declared keys, views that cover only part of a query's range, and set operations. In each case the question is the same: can the query be answered from the view, and is the replacement really the same query?

For example, a view that lists every order with its customer (a left join) can answer a query that only wants orders that have a customer, by keeping the view rows where a customer was found. Reading the view without that filter would give extra rows, so the check must refuse it. Every proposed replacement is proven, and then also run against the original on many random small databases.

Those random databases respect declared foreign keys: a child row's key is copied from a real parent row, or left empty when the column allows it. Without that, a rewrite that is valid only because every order has a customer would be wrongly refuted by a made-up orphan order. When declared keys make the proof fail only because the two sides were simplified differently, the check tries again assuming just the NOT NULL columns, which is a weaker and therefore still safe assumption.

The limits: the Calcite and Doris verdicts only say what those systems did, not what is possible, and most outer-join cases are not answered yet. The cases written for KumoSQL carry a checked answer (a replacement that works, or a counterexample for each tempting replacement that does not), but they are small. See the [full reference](../docs/model-reuse.md) for the recorded scores.

These checks depend on the schema, declared constraints, and the features the prover supports. The full guide contains API signatures and regression examples. [Pipeline analysis](pipeline-analysis.md) explains how to find candidate overlaps and rollups across a project.
