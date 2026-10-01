# SQLSolver backend

KumoSQL can use [SQLSolver](https://github.com/SJTU-IPADS/SQLSolver) as a second equivalence prover next to the Z3 prover in `smt_equivalence.py`. SQLSolver (SIGMOD 2024) proves bag-equivalence with linear integer arithmetic and handles shapes the Z3 prover rejects.

## Design rules

- **Additive.** SQLSolver can only turn `not_proven` into `proven_equivalent`. Missing runtime, untranslatable SQL, `NEQ`, `UNKNOWN`, `TIMEOUT` and any failure fall back to `prove_equivalent_smt`. SQLSolver's `NEQ` has no counterexample, so it is never reported as `not_equivalent`; the Z3 fallback supplies counterexamples.
- **No admin rights.** Nothing is installed system-wide and no PATH or registry edits are needed. The runtime lives in one user folder: `KUMOSQL_SQLSOLVER_HOME`, default `%LOCALAPPDATA%\kumosql\sqlsolver` on Windows and `~/.local/share/kumosql/sqlsolver` elsewhere.
- **License.** SQLSolver is Apache-2.0, which permits redistribution with its LICENSE and NOTICE. This repo does not vendor it yet; it is built or downloaded into the user folder.

## Runtime layout

```
<home>/sqlsolver.jar     built with `gradle fatjar` from SQLSolver (Java 17)
<home>/lib/              Z3 4.13 natives: libz3 + libz3java (.dll / .so / .dylib)
<home>/jre/bin/java      optional portable JRE 17+, used when no Java is on PATH
```

Java is taken from `KUMOSQL_JAVA`, `<home>/jre`, `JAVA_HOME`, then `PATH`. `prove-sql-sqlsolver --check` says what is missing. SQLSolver's repository ships only Linux Z3 libraries, so Windows needs the Windows build of Z3 4.13.0 (`libz3.dll`, `libz3java.dll`) from the Z3 release page, unzipped into `<home>/lib`.

## Translation

BigQuery SQL is parsed with sqlglot and re-emitted as one line of Calcite-compatible SQL. Table names such as `proj.ds.orders` become `proj__ds__orders`, identifiers are lowercased and unquoted, and the schema is written as `CREATE TABLE` statements (BigQuery types mapped to SQL types; untyped columns are `INT`). Queries are refused, and fall back to Z3, when they use `UNNEST`, `QUALIFY`, window functions, arrays or structs, `PIVOT`, `TABLESAMPLE`, `SELECT * EXCEPT/REPLACE`, nondeterministic functions, or a table missing from the schema.

SQLSolver answers `EQ` when both queries fail its semantic checks. To stop a mistyped schema from producing a false proof, each query is also compared with an always-empty wrapper of itself in the same JVM run; if either control says `EQ`, the proof is discarded.

## Rollout

1. **Adapter and fallback** (this change): translation, runtime discovery, `prove_equivalent`, `prove-sql-sqlsolver`, tests against a stub Java.
2. **Real-engine validation:** build the jar, run the corpus used by `test_smt_fuzz.py` and `test_safety_corpus.py` through it, and record where it disagrees with Z3. Do this on Linux and on a Windows laptop with no admin rights.
3. **Setup command:** `kumosql-sqlsolver-setup` downloads a portable JRE and the Z3 natives into the user folder and fetches or builds the jar, with checksums.
4. **Pipeline integration:** Dataform declarations and BigQuery table metadata supply schemas automatically; `prove_equivalent` is used by rewrite verification and the equivalent-work reports.
5. **UI:** a Settings toggle and a status line showing which backend produced each proof.
