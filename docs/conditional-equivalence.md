# Equivalent under conditions

Two queries are often equal only because of a fact the queries never state: `id` is a primary key, `customer_id` is never NULL, every order has a customer. A prover that has to be right on every database answers "unknown" or "different" for such a pair. In a team's own warehouse the fact may be cheap to assume, or already true, so the provers have a fourth verdict next to equivalent, different and unknown:

**`proven_conditionally`: equivalent on every database where the listed conditions hold.**

```text
SELECT x.id, x.name FROM users x JOIN users y ON x.id = y.id      vs      SELECT id, name FROM users
proven_conditionally: equivalent whenever users(id) is a primary key (unique, never NULL)
  condition: (id) is unique in users
  condition: users.id is NOT NULL
```

It is a proof, not a guess: the prover is run again with the conditions added as declared facts and proves the pair. The proof is exactly as strong as the unconditional one, with the conditions as extra assumptions. It never counts as `proven`: `result.proven` is `False`, `kumosql.prove_equivalent_*` return it only when asked, and every existing caller (rewrites, refactors, the evals) still sees an unproven pair.

## What it tries

When a pair is not proven, the provers look for conditions in this catalog, all read off the two queries themselves:

| Condition | Taken from |
| --- | --- |
| A column is NOT NULL | every column of a base table the queries read |
| A column set is unique | the columns of each join equality and `IN` subquery, `GROUP BY` and `ORDER BY` columns, the select list of a one-table select, the group keys together with the columns the aggregates read, the columns of `a.k <> b.k` |
| A foreign key | each pair of tables joined on a column equality, in both directions |

Facts that are already declared (the saved BigQuery catalog, Dataform assertions, a `constraints` argument) stay assumed and are never listed as conditions: only the extra facts are. A column the schema does not list is never a candidate. At most 40 candidates are tried.

The search assumes every candidate at once; if that does not prove the pair, the answer stays what it was. If it does, conditions are dropped in chunks while the proof survives, until every single condition has been tried against the set that is left (the last one too, so a set the prover can shrink to nothing is not reported as conditional). The reported set is **minimal for the prover**: it is a set the prover proves the pair under and cannot shrink by one condition. That is a statement about this prover, not about the data:

- A condition that cannot be dropped is not thereby *necessary*. An abstention ("not proven") after a deletion says the prover gave up, not that a database exists where the pair differs without the condition. A returned condition can be stronger than the pair needs (a UNIQUE on an inner table whose duplicates a `DISTINCT` collapses anyway).
- The search returns **one** sufficient set, not every incomparable alternative. Where several minimal sets exist, it leaves out unique-key conditions first and keeps NOT NULL conditions longest, so the result is the cheapest set to accept, not the only one.
- The proof reported with the verdict is the proof under exactly the returned conditions, with its own assumptions (`result.assumptions`), not the assumptions of the first proof under every candidate.

A search that runs out of its 30 seconds returns the set it has, marked in the reason as possibly not minimal.

Three guards keep the verdict honest:

- **Not vacuous.** A set that makes both queries return the same thing on every test database is passed over for another set. A unique key under `HAVING COUNT(*) > 1`, or a self join on a key that asks for two different rows, proves the pair for the wrong reason: both queries come back empty.
- **Fails closed.** The first guard samples databases; when that check itself fails, the conditions are not cleared and the verdict is withheld. When the engine rejects the queries outright (no sample database ran on both, for example a MySQL-only `GROUP BY`), the guard has nothing to run: the verdict stands and its reason ends "not checked for conditions that make both queries constant". Declared types the sample databases cannot build (`TIME`, `ENUM`) are left to inference instead of stopping the check.
- **Not refuted.** If the prover or the executed search has a counterexample for the pair, the counterexample must break one of the reported conditions. A database that meets every condition and still separates the queries would contradict the proof, so no conditional verdict is given.

Not in the catalog: that a table is non-empty (the provers have no way to assume it), filtered uniqueness, functional dependencies and CHECK constraints. Pairs that need them stay unproven.

## The check for each condition

Each condition carries `check_sql`, a query in the input dialect that counts the rows breaking it (zero means the condition holds). Run it against the warehouse before relying on the verdict:

```sql
-- users.id is NOT NULL
SELECT COUNT(*) AS violations FROM `users` WHERE `id` IS NULL
-- (id) is unique in users: duplicates among the non-NULL keys
SELECT COUNT(*) AS violations FROM (SELECT `id` FROM `users` WHERE `id` IS NOT NULL GROUP BY `id` HAVING COUNT(*) > 1) AS duplicated
-- orders(cid) references customers(id): every non-NULL value has a parent
SELECT COUNT(*) AS violations FROM `orders` AS c WHERE c.`cid` IS NOT NULL AND NOT EXISTS (SELECT 1 FROM `customers` AS p WHERE p.`id` = c.`cid`)
```

The check names the table as the query spells it. BigQuery does not enforce keys, so the proof relies on the data meeting them whether or not they are declared.

## Where it appears

- **Python.** `prove_equivalent_algebraic(left, right, conditional=True)` and `prove_equivalent_smt(left, right, conditional=True)` return `SmtStatus.PROVEN_CONDITIONALLY` with `result.conditions`, each a `kumosql.conditional_equivalence.Condition` (`kind`, `table`, `columns`, `text`, `check_sql`, and `parent` and `parent_columns` for a foreign key). A fourth kind, `no_rows` ("no row of t has P", with the SQL `predicate` and a check that counts the rows satisfying it), is not one of the facts this search proposes: `kumosql.difference_explanation` builds it from a verified predicate, `with_conditions` never passes it to a prover as a declared fact, and `broken_by` runs the predicate on the counterexample rows with DuckDB. It supports mutually exclusive outer-join branches for simple one-row SPJ queries; these explanations are marked as covering rather than exact. Nested queries and branches that can return together remain unknown. The unconditional counterexample, if there was one, stays in `result.counterexample`: it is a database that breaks a condition.
- **Command line.** `prove-sql-equivalent` and `prove-sql-smt` take `--conditional` (and `--schema FILE`, a JSON map of table to column list, for `prove-sql-equivalent`; `prove-sql-smt` already had it). They print the verdict, the conditions and their checks, and exit **3**; exit 0 still means proven outright, so a script that treats 0 as a proof is unchanged. `prove-sql-smt --conditional` prints `status`, `reason`, `assumptions` and a `conditions` list as JSON.
- **API.** `POST /api/prove-queries` returns `status: "proven_conditionally"` and `conditions: [{kind, table, columns, text, check_sql}]`; `POST /api/prove-tables` returns `status: "conditional"` with the same list. Both are on by default.
- **Settings → Solver.** **Compare queries** and **Compare tables** say "Equivalent under 2 conditions" and list the conditions; each opens to its check query. Both panels also list the assumptions of the proof, including, where declared keys come from BigQuery, the warning that its NOT ENFORCED keys are offered as premises, not validated facts.

## Measured

On Singh and Bedathur's 2,800 LeetCode pairs (no keys, no NOT NULL facts) 610 pairs that the prover cannot prove outright are proved under a minimal set of conditions, 0 wrong; on a 3,000-case sample of VeriEQL's LeetCode set, where keys are already declared, 297 more are proved under conditions beyond the declared ones, 0 wrong. Three hand-checked suites cover the shape of each verdict. See [the eval page](evals/conditional-equivalence.md). The verdict is off by default in the Python API, so no existing score moves.
