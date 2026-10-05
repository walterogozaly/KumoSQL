# Sample databases

Complete public sample databases for `tools/sample_db_bench.py` ([docs](../../../docs/evals/sample-databases.md)). One folder per database:

| Path | Content |
| --- | --- |
| `<db>/upstream/` | the pinned upstream files, **unchanged**, with the upstream licence |
| `<db>/adapted/schema.sql` | **adapted**: the BigQuery DDL the harness creates the tables from; its header lists every adaptation |
| `<db>/workload.json` | workload queries; `origin` says whether a query is upstream (adapted to BigQuery, `adaptation` says how) or authored |
| `<db>/pairs.json` | authored query pairs with labels and witnesses |

The Oracle schemas are `oracle_hr/` and `oracle_co/` (the Oracle scripts split the DDL and the rows into `*_create.sql` and `*_populate.sql`, both kept unchanged); each has results files of its own.

The data is never copied out of the upstream scripts: the harness reads the INSERT statements at run time. Sakila's DDL and INSERTs are two files (`sakila-mv-schema.sql`, `sakila-mv-data.sql`); its `pairs.json` also holds a `not_scored` list (a pair the harness cannot replay, with the reason).

| Database | Source | Commit | File | SHA-256 | Licence |
| --- | --- | --- | --- | --- | --- |
| Chinook 1.4.5 | [lerocha/chinook-database](https://github.com/lerocha/chinook-database) | `7f67772503d71ba90f19283c38e93923addb43fa` | `ChinookDatabase/DataSources/Chinook_Sqlite.sql` | `caf31d698a4a79c628215b552dfe6575e71be052ae02b8f18e763498f55f5d44` | MIT-style, Copyright (c) 2008-2024 Luis Rocha (`chinook/upstream/LICENSE.md`) |
| Northwind | [microsoft/sql-server-samples](https://github.com/microsoft/sql-server-samples) | `beaab06ef72831089ca80e5355d65e661fd19b26` | `samples/databases/northwind-pubs/instnwnd.sql` | `3cc62b3fca6d244a47dbde698b809331e4f85988a0685b2b370717d431e94871` | MIT, Copyright (c) Microsoft Corporation (`northwind/upstream/license.txt`) |
| Sakila (Spatial 0.9) | [datacharmer/test_db](https://github.com/datacharmer/test_db) | `e324b56193ca506ab7cc1ab143a9153d8c4535d7` | `sakila/sakila-mv-schema.sql` | `61c30abd47a0126e9e901a911b8a115e1920f0910ce1b11ff3abc764ad65df53` | New BSD, Copyright (c) 2014 Oracle Corporation (the header of each file) |
| Sakila | the same | `e324b56193ca506ab7cc1ab143a9153d8c4535d7` | `sakila/sakila-mv-data.sql` | `cf9328c055ed43c6862332438670fdad68c6236d051fa090c6bcb56cf5895bc2` | the same (the header of the file); the mirror's note `sakila/README.md` has SHA-256 `05fe520851c87f662d5cbd8a50655d361e08b2ad56bedf495ed4d3b3bc1fb3e0` |
| Pagila 4.1.1 | [devrimgunduz/pagila](https://github.com/devrimgunduz/pagila) | `9baf49c4149e43229f6021e6218d6b2ac8ef4f34` | `pagila-schema.sql` | `071ee940a73c8f4fad2997185788065291607e2c55559e3747959e8275391536` | PostgreSQL licence, Copyright (c) Devrim Gündüz (`pagila/upstream/LICENSE.txt`) |
| | | | `pagila-data.sql` (13 MB, `COPY` blocks) | `a88efa94c7ae8bc9cf55def4efc9f164d064d5b9cd93f11719ba3b5ace1602f7` | |
| Oracle HR | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) | `6660bad68c07bd143430ace58565b3f727e17263` | `human_resources/hr_create.sql` | `19bb40fdad9ff31b66aad4bb2eff1a13b730fec2f39eb13f25bd157c980e023f` | MIT text, Copyright (c) 2023 Oracle and/or its affiliates (`oracle_hr/upstream/LICENSE.txt`) |
| Oracle HR | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) | `6660bad68c07bd143430ace58565b3f727e17263` | `human_resources/hr_populate.sql` | `420375c58700178b74949ccfc1a7b22bc2fd305c486fc558fca43f8ce3ddc4df` | MIT text, Copyright (c) 2023 Oracle and/or its affiliates (`oracle_hr/upstream/LICENSE.txt`) |
| Oracle HR | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) | `6660bad68c07bd143430ace58565b3f727e17263` | `human_resources/hr_code.sql` (a procedure and two triggers, no query) | `5604aae0d47585bba996a90eecc43f494d4c63de19b2b9db7afe5007d6f669f5` | MIT text, Copyright (c) 2023 Oracle and/or its affiliates (`oracle_hr/upstream/LICENSE.txt`) |
| Oracle HR | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) | `6660bad68c07bd143430ace58565b3f727e17263` | `LICENSE.txt` | `2da4f8e1f04662e5db9b224a20dfd13db8bc396398271d607bda0343212fbce3` | MIT text, Copyright (c) 2023 Oracle and/or its affiliates (`oracle_hr/upstream/LICENSE.txt`) |
| Oracle Customer Orders | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) | `6660bad68c07bd143430ace58565b3f727e17263` | `customer_orders/co_create.sql` | `8ce42790ec255840bcaf22ff8bc4f53e53996ccecb318410a0b3a36a4c3d6cc1` | MIT text, Copyright (c) 2023 Oracle and/or its affiliates (`oracle_co/upstream/LICENSE.txt`) |
| Oracle Customer Orders | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) | `6660bad68c07bd143430ace58565b3f727e17263` | `customer_orders/co_populate.sql` (1.3 MB) | `e636942e49d9f7cb586f779122ffa2b4da95b07cc7f314c0ef1dae60a5f0cefa` | MIT text, Copyright (c) 2023 Oracle and/or its affiliates (`oracle_co/upstream/LICENSE.txt`) |
| Oracle Customer Orders | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) | `6660bad68c07bd143430ace58565b3f727e17263` | `LICENSE.txt` | `2da4f8e1f04662e5db9b224a20dfd13db8bc396398271d607bda0343212fbce3` | MIT text, Copyright (c) 2023 Oracle and/or its affiliates (`oracle_co/upstream/LICENSE.txt`) |

Pagila's DDL and data are two files; `LICENSE.txt` of Pagila has the SHA-256 `516e7dac679ac1eeb62d5614b01c4e7318154e9a147377d6264954215997ff38`. Its rows are `COPY` blocks, which the harness reads (`read_copy`); `pagila-insert-data.sql` (19 MB, the same data as `INSERT`s) is not committed.

Employees (`employees/`) is the exception: `datacharmer/test_db` at commit `e324b56193ca506ab7cc1ab143a9153d8c4535d7` is licensed Creative Commons Attribution-Share Alike 3.0 and is 167 MB, so **none of its files is committed**. `tools/sample_db_employees.py` downloads the 13 pinned files (each SHA-256 is listed there) into `$KUMOSQL_BENCH_DATA/sample-db-employees` and loads them into a DuckDB file; the folder has only `adapted/schema.sql`, `workload.json` and `pairs.json`, shared under the same licence, and `NOTICE.md`.

Each file can be fetched again from `https://raw.githubusercontent.com/<repo>/<commit>/<file>`; the harness checks the SHA-256 on every run.
