# Testing SQL translations between dialects

[Simple eval index](README.md) · [Full reference](../../docs/evals/dlbench.md)

DLBench pairs a query with its translation into another database's dialect. KumoSQL uses it to check parsing and whether supported translations can be proved equivalent to their sources.

The pinned subset includes approximate-label pairs and a sample of exact-label pairs. Labels came from language models and human review upstream; KumoSQL does not call a model while checking them.

## Why translation is hard

The same text can have different behavior across engines: LIKE can have different case rules, integer division can truncate, and division by zero can produce NULL, infinity, or an error.

The harness reads each side in its dialect, handles supported renamed columns, and tries proof. Known dialect gaps stay unknown rather than being accepted as equivalent.

ORDER BY on the source can require an ordered comparison. Output names are otherwise ignored under this evaluation's policy.

```sh
python tools/dlbench_bench.py
```

The full guide describes the subset, dialect approximations, proof assumptions, gaps, source provenance, and scores. Parsing both statements is coverage evidence; it does not establish faithful translation.
