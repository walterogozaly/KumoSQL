# Window equivalence cases

Data of the `window-equivalence` eval ([docs](../../docs/evals/window-equivalence.md), runner `tools/window_equivalence_bench.py`).

- `fixtures.json`: the four `events` fixtures (what is declared about the table).
- `cases.jsonl`: the `derived` and `idiom` cases, written for this repository, with the witness database of each non-equivalent one.
- `slt_cases.jsonl`: window queries copied from the DuckDB test suite (`test/sql`, MIT, notice in `LICENSE-duckdb`) with the rows of the tables they read, and the pairs made from them. Remade by `python tools/window_equivalence_bench.py --make-slt-cases`.
- `corpus.json`: ids and checksums of the development-split pairs read at run time from VeriEQL (CC BY-NC-SA 4.0, never copied) and SQLSolver (`tests/fixtures/sqlsolver`).
