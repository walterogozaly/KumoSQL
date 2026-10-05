# Reusing a model you already have

[All simple guides](README.md) · [Full reference](../docs/model-reuse.md)

Before building a new table, ask whether an existing table can supply the result. KumoSQL separates three questions.

## 1. Can I read an existing model instead?

The helper `rewrite_over_model` proposes a query reading the model and checks it against the original with the model's SQL expanded. It returns a replacement only when the prover establishes equivalence.

For example, a model containing all orders with customer IDs might supply a query filtering those orders to one customer. A model that discarded a needed column cannot supply it.

A few more shapes work. A view of events per second can supply events per minute, hour, day, month or year, because cutting a timestamp to a minute after cutting it to a second gives the same result as cutting it once. It cannot supply a finer unit, and a view by week cannot supply months, since a week can span two months. A view that is already filtered to one group (a view per customer name) can supply a total for one named customer, and a view that kept only groups above 10 can supply the groups above 20 by filtering again. A view column that is the same number written as a bigger type can supply a filter on the original column when the column types are known. A `SELECT *` over a join written with `USING` lists the joined column first, as the standard says, and the rewrite keeps that order.

These are only proposals: the prover still has to establish that the replacement and the original give the same rows, and the random-database check still has to agree. The full guide lists the open shapes (outer joins, set operations, joins on keys, unions of a view with base rows).

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
