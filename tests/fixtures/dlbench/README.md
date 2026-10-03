# DLBench translation pairs (pinned subset)

A subset of DLBench's SQL translation pairs, scored by `tools/dlbench_bench.py`.

* Source: https://github.com/DLBenchll/DLBench, commit `a3525919033faac73e60a13a88c2d14ab6953f23`
  (2025-07-25), files `datasets/{BIRDTrans,BUTTERTrans}/{mysql,mariadb,postgresql,clickhouse,monetdb,duckdb}.json`.
* Licence: Apache-2.0 (see `LICENSE`, copied from the repository; it has no NOTICE file).
* Paper: DLBench, ASE 2025. Labels come from language models and human review, not proofs.

## The whole benchmark and the subset

| | pairs | exact | approximate |
| --- | ---: | ---: | ---: |
| BIRDTrans (SQLite source, BIRD queries) | 3,206 | 3,003 | 203 |
| BUTTERTrans (MySQL and PostgreSQL test-suite queries) | 3,196 | 3,196 | 0 |
| **all** | **6,402** | **6,199** | **203** |
| pinned subset | 807 | 604 | 203 |

The subset is every "approximate" pair plus one exact pair in ten, chosen by
`sha1("dlbench\n<dataset>\n<target>\n<sql_id>") % 10 == 0`. `python tools/dlbench_bench.py --data
<checkout> --write-subset` rebuilds it; `--data <checkout>` scores all 6,402 pairs. One subset pair in
five (`sha1("dlbench-held-out\n<id>") % 5 == 0`, 103 exact pairs) is held out.

## Files

| file | content |
| --- | --- |
| `pairs.jsonl` | one pair per line with the published fields `sql_id`, `database` (`database_name`), `source_dbms`, `target_dbms`, `source_query`, `target_query` and `label_raw` (`semantic_equivalent_type` as published), plus `dataset` |
| `schemas.json` | `<dataset>/<database>/<source dbms>` -> the published `source_related_schemas` texts, once per table |
| `renames.json` | `<dataset>/<database>/<target dbms>` -> translation column name -> source column name, where BIRDTrans's translated schema renamed a column (for example `_Type` for `Type`, `Date_received` for `Date received`); derived by matching `source_related_schemas` and `target_related_schemas` column by column |
| `LICENSE` | DLBench's Apache-2.0 licence |

The published label spells "approximate" three ways (`appr_equivalence` in the README,
`approximate_equivalence` in the MySQL, MariaDB and MonetDB files, `Approximate equivalence` in the
others); `label_raw` keeps the spelling and the harness reads any of them as `approximate`.
The dialect-knowledge fields and the translated schemas (most of the 35 MB) are not copied.

## Original and adapted

Queries are stored as published. The harness renames translated columns back (`renames.json`),
lower-cases identifiers, reads MariaDB as MySQL and MonetDB as PostgreSQL (sqlglot has neither
dialect) and writes the translation in the source's dialect with sqlglot before proving.

## Overlap with other evals

Checked 2026-10-03 on whitespace- and case-normalised text over all 6,402 pairs: no source or
translated query appears in SQL-IQ's Equivalence Judge pairs or SQL Judge candidates (SQL-IQ commit
`fc290c9f4e72d501e05b665f9abbdd2d88d7b2b0`, which also draws on BIRD) or in LLM-SQL-Solver's Spider pairs.
