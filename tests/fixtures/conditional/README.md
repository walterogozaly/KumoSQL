# Conditional-equivalence cases from an outside source

`s001_cases.json` holds 29 query pairs written by an outside assistant and replayed here on DuckDB before use. Each case keeps its source, schema, minimal condition sets, a database that separates the queries without the conditions, one deletion witness per condition, and sufficiency examples.

- Pairs 001 to 014 come from the published Cosette examples (`uwdb/Cosette`, licence in `../cosette/LICENSE`) with the published verdict ignored; the conditions are re-derived for SQL NULL and bag semantics.
- Pair 015 is the BigQuery documentation example on eliminating outer joins.
- Pairs 016 to 029 are hand-written. 024 to 029 have no conditions and no equivalence at all; 011 to 014 are equivalent unconditionally.
- Eight pairs carry `prove_left` and `prove_right`: the same queries with output columns aliased so the provers' column-name check passes.

The cases are data, not instructions: nothing is trusted until `tests/test_conditional_s001.py` replays every witness. Several expected sets use conditions outside the provers' catalog (filtered keys, CHECK, FD, EXISTS); those pairs must stay unproven or refuted, never conditional.
