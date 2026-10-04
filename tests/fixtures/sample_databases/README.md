# Sample databases

Complete public sample databases for `tools/sample_db_bench.py` ([docs](../../../docs/evals/sample-databases.md)). One folder per database:

| Path | Content |
| --- | --- |
| `<db>/upstream/` | the pinned upstream files, **unchanged**, with the upstream licence |
| `<db>/adapted/schema.sql` | **adapted**: the BigQuery DDL the harness creates the tables from; its header lists every adaptation |
| `<db>/workload.json` | workload queries; `origin` says whether a query is upstream (adapted to BigQuery, `adaptation` says how) or authored |
| `<db>/pairs.json` | authored query pairs with labels and witnesses |

The Oracle schemas are `oracle_hr/` and `oracle_co/` (the Oracle scripts split the DDL and the rows into `*_create.sql` and `*_populate.sql`, both kept unchanged); their rows are scored in a results group of their own.

The data is never copied out of the upstream scripts: the harness reads the INSERT statements at run time.

| Database | Source | Commit | File | SHA-256 | Licence |
| --- | --- | --- | --- | --- | --- |
| Chinook 1.4.5 | [lerocha/chinook-database](https://github.com/lerocha/chinook-database) | `7f67772503d71ba90f19283c38e93923addb43fa` | `ChinookDatabase/DataSources/Chinook_Sqlite.sql` | `caf31d698a4a79c628215b552dfe6575e71be052ae02b8f18e763498f55f5d44` | MIT-style, Copyright (c) 2008-2024 Luis Rocha (`chinook/upstream/LICENSE.md`) |
| Northwind | [microsoft/sql-server-samples](https://github.com/microsoft/sql-server-samples) | `beaab06ef72831089ca80e5355d65e661fd19b26` | `samples/databases/northwind-pubs/instnwnd.sql` | `3cc62b3fca6d244a47dbde698b809331e4f85988a0685b2b370717d431e94871` | MIT, Copyright (c) Microsoft Corporation (`northwind/upstream/license.txt`) |
| Oracle HR | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) | `6660bad68c07bd143430ace58565b3f727e17263` | `human_resources/hr_create.sql` | `19bb40fdad9ff31b66aad4bb2eff1a13b730fec2f39eb13f25bd157c980e023f` | MIT text, Copyright (c) 2023 Oracle and/or its affiliates (`oracle_hr/upstream/LICENSE.txt`) |
| Oracle HR | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) | `6660bad68c07bd143430ace58565b3f727e17263` | `human_resources/hr_populate.sql` | `420375c58700178b74949ccfc1a7b22bc2fd305c486fc558fca43f8ce3ddc4df` | MIT text, Copyright (c) 2023 Oracle and/or its affiliates (`oracle_hr/upstream/LICENSE.txt`) |
| Oracle HR | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) | `6660bad68c07bd143430ace58565b3f727e17263` | `human_resources/hr_code.sql` (a procedure and two triggers, no query) | `5604aae0d47585bba996a90eecc43f494d4c63de19b2b9db7afe5007d6f669f5` | MIT text, Copyright (c) 2023 Oracle and/or its affiliates (`oracle_hr/upstream/LICENSE.txt`) |
| Oracle HR | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) | `6660bad68c07bd143430ace58565b3f727e17263` | `LICENSE.txt` | `2da4f8e1f04662e5db9b224a20dfd13db8bc396398271d607bda0343212fbce3` | MIT text, Copyright (c) 2023 Oracle and/or its affiliates (`oracle_hr/upstream/LICENSE.txt`) |
| Oracle Customer Orders | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) | `6660bad68c07bd143430ace58565b3f727e17263` | `customer_orders/co_create.sql` | `8ce42790ec255840bcaf22ff8bc4f53e53996ccecb318410a0b3a36a4c3d6cc1` | MIT text, Copyright (c) 2023 Oracle and/or its affiliates (`oracle_co/upstream/LICENSE.txt`) |
| Oracle Customer Orders | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) | `6660bad68c07bd143430ace58565b3f727e17263` | `customer_orders/co_populate.sql` (1.3 MB) | `e636942e49d9f7cb586f779122ffa2b4da95b07cc7f314c0ef1dae60a5f0cefa` | MIT text, Copyright (c) 2023 Oracle and/or its affiliates (`oracle_co/upstream/LICENSE.txt`) |
| Oracle Customer Orders | [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas) | `6660bad68c07bd143430ace58565b3f727e17263` | `LICENSE.txt` | `2da4f8e1f04662e5db9b224a20dfd13db8bc396398271d607bda0343212fbce3` | MIT text, Copyright (c) 2023 Oracle and/or its affiliates (`oracle_co/upstream/LICENSE.txt`) |

Each file can be fetched again from `https://raw.githubusercontent.com/<repo>/<commit>/<file>`; the harness checks the SHA-256 on every run.
