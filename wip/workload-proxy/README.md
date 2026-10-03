# Work in progress: STATS-CEB materialization proxy (prototype scripts)

Scratch prototypes for the workload-ranked materialization backtest. Not part of the package and not tested.
They read the STATS-CEB checkout and DuckDB database from the joinorder bench cache directory and write
their outputs next to themselves (edit `OUT` in `proto.py`).

- `proto.py`: mine join views from the dev queries (`view_candidates.mine`), cap their true size, prove reader rewrites (`model_reuse.rewrite_over_model`).
- `proto2.py`: cardinality features (scan, C_out) for queries, queries over each view, and view builds.
- `proto3.py`: measure baseline runtimes and, per stored view, its build time and its proven readers' runtimes (median of 3, 4 threads).
- `proto4.py`, `pairs.py`, `proto5.py`: calibrate on dev baselines and compare predicted with measured savings.
- `measure.json`: measurements for the first 10 sampled views (146 reader pairs, every rewritten reader returned the same count).

See the handoff notes in the pull request or issue for the findings.
