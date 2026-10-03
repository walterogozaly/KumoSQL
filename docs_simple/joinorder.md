# Estimating join sizes and choosing an order

[All simple guides](README.md) · [Full reference](../docs/joinorder.md)

Joining large tables can create large intermediate results. Choosing which tables to join first can affect how much work an engine does.

KumoSQL estimates the row counts of smaller joins inside a query, then chooses a join tree using those estimates. This is an estimate of work, not a proof of faster execution.

## Example

You join orders, customers, and countries, but keep only customers in one country. If that filter removes most customers, applying it before a large join may keep intermediate results small.

Simply multiplying table sizes misses two important facts: some keys are much more common than others, and a filter can select mostly those common keys.

KumoSQL gathers row counts, samples, and counts grouped into join-key bins. Frequent values get their own bins. It uses these statistics to estimate filters and joins, then searches join orders. Small tables can be stored in full for more exact filter checks.

Statistics collection uses DuckDB. Planning from those stored statistics runs in Python without contacting a database or using a language model.

Saved statistics use compressed JSON with checks on the format and join-key bins. Old pickle files are refused. Collect them again after upgrading; the benchmark commands create the new cache automatically.

## Understand the measurements

| Measurement | Meaning |
| --- | --- |
| Q-error | How far an estimated row count is from the true count; 1 is exact and 2 is a factor of two away |
| Plan cost | Intermediate row counts summed under a chosen join tree |
| Runtime | Time taken when the chosen tree is executed in the benchmark engine |

Q-error treats counts below 1 as 1. Runtime and estimated plan cost are separate: a good estimate does not guarantee the fastest query.

The full guide compares STATS-CEB and JOB workloads and includes commands for gathering data and running measurements. Different key groups are treated as independent, which can miss correlations. The estimates also depend on the samples and statistics you collected.
