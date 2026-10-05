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

These checks depend on the schema, declared constraints, and the features the prover supports. The full guide contains API signatures and regression examples. [Pipeline analysis](pipeline-analysis.md) explains how to find candidate overlaps and rollups across a project.

## 4. Which models in my project could read another model?

For a loaded Dataform project, `python -m kumosql model-proposals DIR` goes through the pairs of models and lists "model A can read model B" for every pair where the prover proved the answer is the same. Each entry shows the replacement SQL, the assumptions behind the proof and a rough estimate of the bytes saved per run. It is a list of suggestions for the Dataform project refactoring workflow: it changes no file, and there is no screen for it yet.

For example, if `wide_orders` keeps all orders with an amount and `paid_totals` sums the paid ones from the same raw table, the list can say `paid_totals` could read `wide_orders` and show the shorter query. A model that dropped rows or columns the other one needs is simply not listed.

Limits to keep in mind:

- Only proven pairs appear. A pair that is missing is not shown to be impossible; the matcher gives up on shapes it cannot read yet.
- The savings come only from numbers you supply (table sizes and exported job history). Without them the figure says `unknown`; it is never guessed. Even with them it is a planning estimate that assumes the whole other table is read, so measure before and after you change anything.
- Reading a view saves nothing, because the view runs its query again.
- Models that run incrementally, run extra statements, use the clock or random values, or take an unordered `LIMIT` are left out.
- Applying one proposal can rule out another (two models that could each read the other, for instance); each entry names the ones it conflicts with.

See the [full reference](../docs/model-reuse.md#proposals-for-a-dataform-project) for the options, the exact estimate rules and the recorded tests.
