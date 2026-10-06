# Verified difference explanations

[Full reference and results](../../docs/evals/difference-explanations.md)

This eval checks whether KumoSQL can show a short SQL condition that exactly describes a query difference. Every counted query pair was first refuted by its source suite. Every predicate KumoSQL returned also passed an independent DuckDB replay and two proof checks.

In the development sample, **29 of 35** refuted pairs that compile to one select-project-join block got a verified predicate with at most three atoms. Across all 152 syntactic SPJ pairs, the score was **29/152 (19.1%)**. Most misses came from outer joins that the compiler expands into multiple internal branches; this explanation path does not handle those yet. The one-block score is above the 60% goal, but the broader score is below it.

In the Singh and Bedathur held-out sample, **3/3** one-block pairs got verified predicates. Across all nine held-out syntactic SPJ pairs, the score was **3/9 (33.3%)**; the other six compiled into multiple branches. Unsafe rewrites and pipeline refutations supplied no eligible held-out pairs, and VeriEQL has no held-out split. These are small diagnostic samples, not full source-corpus scores, and the SQL data stays outside the repository.
