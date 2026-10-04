# Paired engine tests

`cases.jsonl` holds 163 query pairs (157 scored, 6 kept unscored with the reason in `excluded`) from other engines' own tests and bug fixes, plus authored guards. `fixtures.json` holds the small tables the Spark, PostgreSQL, DuckDB and authored pairs run on; the TPC-H tables for the Trino pairs are generated (scale 0.01, Trino's `tiny` schema) by `tools/benchmark_corpora.py` and never committed. The harness is `tools/engine_pairs_bench.py`; the eval page is [`docs/evals/engine-paired-tests.md`](../../../docs/evals/engine-paired-tests.md).

Each case records its `source`, `inventory` row (A-R01 to A-R08 in the [public sources inventory](../../../docs/evals/public-sources.md)), `origin` (`original`: an upstream pair, translated; `adapted`: paired, completed or changed by us; `authored`: written by us), `label` (`equivalent`, `fixture`: same rows on the source's data only, or `not_equivalent`) and a `why` that justifies it. `left` and `right` are the translated queries; `original` keeps the upstream text and `translation` says how it was made. Original and adapted pairs are never mixed: the eval page scores them separately.

## Pinned upstream sources

| Source | Licence | Commit | Path | SHA-256 |
| --- | --- | --- | --- | --- |
| Trino | Apache-2.0 | `2cf6ed7e29ee36a67002a6a5373f81cecdd06f7a` | `testing/trino-testing/src/main/java/io/trino/testing/AbstractTestJoinQueries.java` | `5d11f8436c4d12315199bd08c7ab5338e192324288e71c9a7116901b91c76b78` |
| Spark | Apache-2.0 | `099c3ee2e2e71ef8c826ee06d35081cb6d82b29c` | `sql/core/src/test/scala/org/apache/spark/sql/SubquerySuite.scala` | `b88511d3be4c2d25acb838541748b0c5ccc84659674c2283e180fed3767de367` |
| PostgreSQL | PostgreSQL | `f4c00d138f6dea4c9d8af8ec280b7edc9b0a29e1` | `src/test/regress/sql/join.sql` | `b7e0166efab99eec1e8d5d45bad5cd227d70a51b3c4d6997c1d0ff886e75b53e` |
| DuckDB | MIT | `25a52e4149d331ccf1bf2ab02235549a37b0e71b` | `test/sql/window/test_window_order_collate.test` | `5c4c8321d8b51fd7ebbaf3156c172da41ccac56765d47a0b622ace0c38438478` |

`python tools/engine_pairs_bench.py --check-sources` downloads each file, checks the SHA-256 and that every recorded original query is in it (for Trino, that the extraction yields exactly the recorded pairs). The files themselves are not committed: only the queries the cases need, with the licence texts beside them (`LICENSE-apache-2.0`, `NOTICE-spark`, `LICENSE-postgresql`, `LICENSE-duckdb`).

## Licences and what is not here

* Trino and Spark queries are Apache-2.0; PostgreSQL's regression query is under the PostgreSQL licence; the DuckDB test file is MIT. Each is attributed above.
* JoinEquiv has no licence, so none of its code or data is here: the projection boundary it describes is an authored counterexample (3 cases).
* The jOOQ pairs (8) are written by us from the manual's transformation pages, which are cited and not copied.
* Bug reports on blocked hosts (PG17982, PG17985, DuckDB issues 20483 and 20486) are not recoverable and are not included; the PostgreSQL dropped-qual query is our reading of a commit message.

**Overlap.** None of these pairs is in the optimizer-bug pairs ([`optimizer_bugs`](../optimizer_bugs/README.md)); the DuckDB test file's queries are also run one at a time by the DuckDB SQLLogicTest eval.
