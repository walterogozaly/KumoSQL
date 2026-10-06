# Eval diff

`python tools/eval_diff.py` discovers commands from `benchmarks/results/*.json`, removes `--write-results`, and compares each successful run on the selected base revision and the current checkout. Timing fields are masked before output comparison, including `*_seconds`, `median_ms`, and `p95_ms`; result counts and verdicts remain part of the comparison. Each row reports separate base and checkout wall times.

## Slow evals

| Results rows | Sample unit | Full-mode cap | Sample-mode cap |
| --- | --- | ---: | ---: |
| `targeted-test-data`, `multi-database-semantic`, `counterexample-minimization`, `unsafe-rewrite-variants` | First N originals per source suite, in fixed corpus order | 7,200 s | 1,800 s |
| `singh-bedathur-leetcode` | N query pairs, sampled with the harness's fixed seed (`2024`) | 7,200 s | 1,800 s |
| `conditional-equivalence-*` | Singh uses the fixed seed (`2024`); VeriEQL uses the first N cases after its configured `--every` stride | 7,200 s | 1,800 s |
| `bounded-*` | First N pairs/cases after any configured `--every` stride | 7,200 s | 1,800 s |
| `engine-*-slt-*` | First N test files after the configured stride, in sorted path order | 7,200 s | 1,800 s |

The sample uses each harness's existing deterministic selector, so both base and checkout receive the same subset without requiring the base revision to support new flags. The seeded harness selector uses seed `2024`; the remaining selectors take a fixed prefix from their ordered source list after any existing stride. A sample is a quick regression comparison, not a replacement score. Use `--only` with the slow result-row names when requesting `--sample`; commands without a sample-capable harness are reported as `UNCOMPARED` instead of silently running in full mode. `--sample N` means N cases per source suite for targeted data, N files for engine suites, and N pairs/cases for the other listed harnesses.

```powershell
python tools/eval_diff.py --base origin/master --only targeted-test-data multi-database-semantic unsafe-rewrite-variants bounded-leetcode conditional-equivalence-singh engine-duckdb-slt engine-sqlite-slt singh-bedathur-leetcode --sample 25 --serial --timings eval-diff-sample-timings.json
```

`--serial` runs the base command and checkout command one at a time. It is useful when capturing standalone timings. The JSON file records a base and checkout duration, exit code, and state beside every eval row; it can be retained with the run notes. Full runs use a 7,200-second cap for the listed slow evals and 3,600 seconds for other commands. Override these with `--slow-timeout` and `--timeout`; sample comparisons use the same `--sample-timeout` for both sides.

## What is covered

The inventory is the distinct, runnable `command` values in `benchmarks/results/*.json`. Multiple result rows that share a command run once and appear together in one table row. Result files without a command and commands containing unresolved `<...>` or `PATH` placeholders are omitted. Other commands that require network-fetched corpora, optional database clients, credentials, or local workspaces remain discoverable; if either side cannot complete successfully, the report names the eval under `UNCOMPARED`, gives the exit/timeout state and elapsed time, and makes the overall command exit nonzero.

Only two successful exits count as a comparison. Matching nonzero exits, timeouts, or an unsupported sample request never count as agreement. A timeout terminates the evaluator process tree, including child workers. The final summary reports completed comparisons, differences, and evals that could not be compared.
