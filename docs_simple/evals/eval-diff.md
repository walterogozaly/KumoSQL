# Compare evals between code versions

[All simple guides](../README.md) · [Full eval-diff reference](../../docs/evals/eval-diff.md)

`python tools/eval_diff.py` reruns benchmark commands on a base version and your checkout, then reports changed results. It discovers commands from the benchmark results files.

Elapsed-time fields such as `*_seconds`, `median_ms`, and `p95_ms` are ignored during comparison. Counts and verdicts are still compared.

For slow suites, select the eval names with `--only` and use `--sample 25`. Both versions get the same deterministic sample: either a fixed-seed selection or the first cases in the harness's stable order. The sample is useful for spotting regressions, but it is not a benchmark score. The report shows how long each side took and says which evals could not be compared.

```powershell
python tools/eval_diff.py --base origin/master --only targeted-test-data bounded-leetcode conditional-equivalence-singh engine-duckdb-slt engine-sqlite-slt singh-bedathur-leetcode --sample 25 --serial --timings eval-diff-sample-timings.json
```

Full runs give each slow eval its own two-hour timeout. If a run times out, the eval and its worker processes are stopped. See the full reference for the sample units, coverage limits, and timeout options.
