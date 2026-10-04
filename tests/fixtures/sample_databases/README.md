# Sample databases

Complete public sample databases for `tools/sample_db_bench.py` ([docs](../../../docs/evals/sample-databases.md)). One folder per database:

| Path | Content |
| --- | --- |
| `<db>/upstream/` | the pinned upstream files, **unchanged**, with the upstream licence |
| `<db>/adapted/schema.sql` | **adapted**: the BigQuery DDL the harness creates the tables from; its header lists every adaptation |
| `<db>/workload.json` | workload queries; `origin` says whether a query is upstream (adapted to BigQuery, `adaptation` says how) or authored |
| `<db>/pairs.json` | authored query pairs with labels and witnesses |

The data is never copied out of the upstream scripts: the harness reads the INSERT statements at run time.

| Database | Source | Commit | File | SHA-256 | Licence |
| --- | --- | --- | --- | --- | --- |
| Chinook 1.4.5 | [lerocha/chinook-database](https://github.com/lerocha/chinook-database) | `7f67772503d71ba90f19283c38e93923addb43fa` | `ChinookDatabase/DataSources/Chinook_Sqlite.sql` | `caf31d698a4a79c628215b552dfe6575e71be052ae02b8f18e763498f55f5d44` | MIT-style, Copyright (c) 2008-2024 Luis Rocha (`chinook/upstream/LICENSE.md`) |
| Northwind | [microsoft/sql-server-samples](https://github.com/microsoft/sql-server-samples) | `beaab06ef72831089ca80e5355d65e661fd19b26` | `samples/databases/northwind-pubs/instnwnd.sql` | `3cc62b3fca6d244a47dbde698b809331e4f85988a0685b2b370717d431e94871` | MIT, Copyright (c) Microsoft Corporation (`northwind/upstream/license.txt`) |
| Pagila 4.1.1 | [devrimgunduz/pagila](https://github.com/devrimgunduz/pagila) | `9baf49c4149e43229f6021e6218d6b2ac8ef4f34` | `pagila-schema.sql` | `071ee940a73c8f4fad2997185788065291607e2c55559e3747959e8275391536` | PostgreSQL licence, Copyright (c) Devrim Gündüz (`pagila/upstream/LICENSE.txt`) |
| | | | `pagila-data.sql` (13 MB, `COPY` blocks) | `a88efa94c7ae8bc9cf55def4efc9f164d064d5b9cd93f11719ba3b5ace1602f7` | |

Pagila's DDL and data are two files; `LICENSE.txt` of Pagila has the SHA-256 `516e7dac679ac1eeb62d5614b01c4e7318154e9a147377d6264954215997ff38`. Its rows are `COPY` blocks, which the harness reads (`read_copy`); `pagila-insert-data.sql` (19 MB, the same data as `INSERT`s) is not committed.

Each file can be fetched again from `https://raw.githubusercontent.com/<repo>/<commit>/<file>`; the harness checks the SHA-256 on every run.
