# Hunting for wrong proofs

[All simple guides](README.md) · [Full reference](../docs/proof-recheck.md)

When KumoSQL says two queries always return the same rows, that is a proof. The evals check each proof against a modest number of random databases. A wrong proof, one where some unusual database gives different rows, can slip through if that database is rare. The proof re-check is a developer tool that goes looking for those databases much harder.

## An example

Suppose KumoSQL has proved `SELECT name FROM dept WHERE deptno > 1` equal to `SELECT name FROM dept WHERE NOT deptno <= 1`. The tool builds thousands of small databases for the pair: empty tables, tables full of NULLs, duplicated rows, values next to the numbers and strings the queries mention, big numbers, strings that differ only in capitals or trailing spaces. Every database respects the keys and other rules the eval declares. It runs both queries on each one. If the results ever differ, the proof is a candidate false proof, and the tool keeps the smallest database that shows it.

Before it counts a difference, the tool rules out noise: rounding in decimal digits, rows returned in a different order, the database engine picking among tied rows, and a known DuckDB optimizer bug. Only a difference that survives all of that is reported.

## What it has found so far

On the first runs, every counted proof of QED (177), Singh and Bedathur (862, tested on DuckDB and on a real MySQL 8) and VeriEQL (7,062) survived. A candidate in VeriEQL was a rounding artifact of the checking engine, which now counts as noise. The Singh runs did turn up one rule that is wrong for MySQL: it treats a string and a number as never equal, which MySQL does not. No counted proof depended on it.

## Limits

- Surviving the search is evidence, not a proof. A database the search never builds can still hide a bug.
- A difference can also come from the checking engine rather than the proof, for example a different rounding, so each one is reviewed by hand before it is called a bug.
- Only some evals have an adapter so far. The full reference lists them, the recorded results and what is still open.
- It is a tool for developers. It changes no scores and no rules; the full reference covers the commands and the output.
