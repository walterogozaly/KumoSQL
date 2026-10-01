# Benchmark results format

The README scoreboard is generated from the files here: one `results/<eval>.json` per eval, one object each. Edit your file, then run `python tools/scoreboard.py` (the tool fails on a missing required key or an unknown value). `python tools/scoreboard.py --check` is what the test suite runs.

Required keys:

| Key | Meaning |
| --- | --- |
| `suite` | Row name, for example `SQLSolver Calcite` |
| `order` | Sort position; leave gaps (10, 11, 20, ...) so new rows slot in |
| `size` | Number of items scored |
| `score` | Headline score, such as `192/232, 0 wrong` or `83.24% (1157/1390)` |
| `metric` | One sentence on what the score means |
| `evidence` | Strength of the evidence behind each positive answer: `proof` (unbounded proof), `bounded` (bounded verification, VeriEQL-style bounded model checking) or `executed` (agreement on executed datasets). Keep these distinct; a suite that mixes them gets one results file per level |
| `correctness` | False proofs, incorrect counterexamples, behaviour-changing rewrites. Say how it was checked |
| `coverage` | Object with any of `proven`, `refuted`, `unknown`, `unsupported`, `timeout`, `error` (counts; `{}` if the eval has no such outcomes) |
| `held_out` | Score on a held-out split, or `none` if every case was available while developing |
| `docs` | Link (relative path, anchor allowed) to the eval's docs |
| `command` | Command that reruns the eval |
| `date` | `YYYY-MM-DD` the numbers were measured |
| `caveats` | Honest limits: `Tuned on test`, subset only, number copied rather than rerun, and so on |

Optional keys, shown in their own table when any row has them: `usefulness` (how often a rewrite gives a verified improvement), `analysis` (lineage and duplicate-detection precision and recall) and `performance` (runtime and memory by query complexity).

Rule: report like `X/Y, 0 wrong`; unknown beats wrong. Counts in `coverage` should add up to `size` where the eval has such outcomes.
