# Employees: attribution and licence

The Employees sample database ([datacharmer/test_db](https://github.com/datacharmer/test_db), commit
`e324b56193ca506ab7cc1ab143a9153d8c4535d7`) is licensed under the
[Creative Commons Attribution-Share Alike 3.0 Unported License](https://creativecommons.org/licenses/by-sa/3.0/)
(`CC BY-SA 3.0`; the header of its `employees.sql` says so). Copyright (C) 2007, 2008 MySQL AB. Original data created by
Fusheng Wang and Carlo Zaniolo, current schema by Giuseppe Maxia, data conversion from XML to relational by Patrick Crews.
To the best of the authors' knowledge the data is fabricated and does not correspond to real people.

**Nothing from upstream is stored in this repository unchanged.** The upstream scripts and the 3.9 million rows are
downloaded at run time from the pinned commit by `tools/sample_db_employees.py` (SHA-256 of every file checked) into a
cache folder outside the repository. Because the licence is share-alike, the files of this folder that adapt upstream
material are shared under the same licence, `CC BY-SA 3.0`, and are marked as adapted:

| File | What it adapts |
| --- | --- |
| `adapted/schema.sql` | the `CREATE TABLE` statements of `employees.sql`, rewritten as BigQuery DDL (its header lists every change) |
| `workload.json`, origins `upstream-view`, `upstream-procedure` and `upstream-test` | the views of `employees.sql` and `objects.sql`, the `SELECT`s of the stored functions and of the `show_departments` procedure, and the record-count comparison of `test_employees_md5.sql`, adapted from MySQL to BigQuery SQL (each entry's `adaptation` says how) |

The queries with origin `authored` in `workload.json` and every pair in `pairs.json` were written for this eval and are
not derived from upstream text; they name the upstream tables and columns, which are facts about the schema.

The Creative Commons legal code could not be fetched when this folder was written; the licence is cited by its public
address above, as the upstream header cites it.
