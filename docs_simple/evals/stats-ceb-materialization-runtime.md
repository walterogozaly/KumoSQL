# STATS-CEB runtime prediction

[Full guide](../../docs/evals/stats-ceb-materialization-runtime.md)

This diagnostic checks whether a model can predict when a proven query rewrite will run faster over a stored join view.

It uses the 146-query STATS-CEB workload. The queries are split by a fixed hash: views are mined from development queries, and the model is fit on development measurements before scoring held-out queries.

Run the full workload with the pinned source checkout:

```powershell
git clone https://github.com/Nathaniel-Han/End-to-End-CardEst-Benchmark
git -C End-to-End-CardEst-Benchmark checkout 670cb8d4bf4cbfa32f94fdf17f33973d3fd67d1b
python tools/materialize_stats_bench.py --repo End-to-End-CardEst-Benchmark
```

A 24-query plumbing sample measured 20 held-out query/view pairs with no wrong results. It is far too small to establish a score or choose a production model. The complete workload has not been measured yet, and the advisor’s total cost after refreshes still needs a separate comparison.
