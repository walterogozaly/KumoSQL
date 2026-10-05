# Employees: a 3.9 million row sample database, downloaded when needed

[Simple eval index](README.md) · [Full reference](../../docs/evals/sample-databases-employees.md) · [Sample databases overview](sample-databases.md)

Employees is the MySQL sample database of a fictional company: 300,000 employees, their departments, managers, job titles and 2.8 million salary periods. This eval loads all of it, about 3.9 million rows in 6 tables, into DuckDB and asks the same two questions as the other sample databases. It has its own results files, so the other databases' numbers stay as they were.

Its licence (Creative Commons Attribution-Share Alike) does not allow this repository to hold the data, and 167 MB is a lot anyway. So the eval **downloads** the files from a pinned commit on GitHub the first time it needs them, checks every file against a recorded SHA-256, and keeps them in a cache folder outside the repository. When GitHub cannot be reached the tests that need the data skip instead of failing, and they sit in the slow tier, so the quick test run never touches the network. What is in the repository is only our own work: the BigQuery version of the table definitions, the workload and the pairs.

## What it checks

- **Rewrites on real data.** Employees' four views, the `SELECT`s inside its stored functions and procedure, its test script's record-count comparison and queries written for this eval go through KumoSQL's rewrite rules. A rewrite is wrong only if the rewritten query returns different rows on the real data than the original.
- **Query pairs.** Hand-written pairs go through the provers. Pairs that really are equal should be proved, and pairs that really differ should be refuted with a small database that DuckDB replays. Many pairs are siblings: the same rewrite with and without a key, foreign key or NOT NULL guarantee, and the sibling without the guarantee must not be proved.

## A concrete example

`salaries` is keyed by `(emp_no, from_date)`: an employee has one salary per start date. Joining `salaries` to itself on both columns returns every row once, so the join can go. Joining on `emp_no` alone pairs every salary period of an employee with every other one, so the same rewrite is wrong. The same goes for `titles`, keyed by `(emp_no, title, from_date)`: `SELECT DISTINCT emp_no, title, from_date` loses its `DISTINCT`, while `SELECT DISTINCT emp_no, from_date` does not, because an employee can start two titles on one day. In both tables the key contains a `DATE`, which is what this database adds to the others.

## What the eval found

- The load is checked three ways against upstream: a row count taken by a separate reader of the `INSERT` scripts, the counts upstream's test scripts publish, and the MD5 and SHA-256 checksums those scripts publish for every table, recomputed from the loaded rows the way MySQL computes them. All agree.
- `titles.to_date` is the only nullable column, which makes it the place for NULL traps (`NOT IN`, `COUNT`, `COALESCE`, `INTERSECT`).
- The data uses `9999-01-01` as "still open". Pairs about that sentinel show which date reasoning the provers handle and which they leave unknown.
- One pair label was wrong in the first run (it assumed a nullable column on both sides when only one side is nullable). The prover was right; the label is corrected and the run recorded in the full reference.

## Limits of the evidence

- The pairs and most of the workload are written for this eval, so a good score shows the rules behave on this one database, not on every database.
- Upstream queries are adapted from MySQL to BigQuery SQL, and DuckDB stands in for BigQuery as the oracle. The data is fabricated, so it has the shape of a small company's history, not of real data.
- Most pair queries are restricted to a range of employee numbers so that comparing their results on 3.9 million rows stays quick; the filter is on both sides of every pair.
- A fifth of the queries and pairs, chosen by hash of their id, are held out and reported apart.

The recorded scores, the pinned commit, the licence and every adaptation are in the [full reference](../../docs/evals/sample-databases-employees.md) and the two `sample-databases-employees-*` results files.

```sh
python tools/sample_db_bench.py --check --database employees      # downloads on first use (about two minutes), then checks
python tools/sample_db_bench.py --database employees --part pairs
```
