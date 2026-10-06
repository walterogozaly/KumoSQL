# When two queries match only if some facts hold

[All simple guides](README.md) · [Full reference](../docs/conditional-equivalence.md)

Sometimes two queries return the same rows only because of something the queries never say: an `id` column has no duplicates, a column is never empty, every order has a customer. A prover that has to be right on every possible database answers "unknown" or "different" for these pairs. KumoSQL can instead answer **equivalent under conditions**: the pair matches on every database where a short list of facts is true.

Suppose you replace `SELECT DISTINCT id FROM users` with `SELECT id FROM users`. The two match only if `id` is unique and never NULL. KumoSQL lists exactly those two facts.

## What you get

- A short list of facts that makes the proof work, with none the prover could drop one by one. It is one such list, not every possible one.
- A SQL check for each fact. It counts the rows that break the fact, so zero means the fact holds in your data.
- A verdict that is never shown as "proven". It is a separate answer, and it stays off unless you ask for it (`--conditional` on the command line; the browser app and API show it).

## What the facts can be

The facts come from the queries themselves: a column is NOT NULL, a set of columns is unique, or one table's column always has a match in another table. Facts already declared in your catalog or Dataform assertions are assumed without being listed. A fact can also say that no row matches a test (for example, no row of `orders` has `status IS NULL`). The search above never proposes that kind; it is what a verified explanation of a difference between two queries is made of. Some simple outer joins can also yield a verified condition when their match cases cannot overlap. Those explanations are marked as covering the difference; more complex joins stay unknown.

## Limits

- The facts are yours to make true. BigQuery does not enforce keys, so run each check before relying on a result.
- Facts about value ranges, tables that are never empty, or keys that hold only for some rows are not tried. Pairs that need them stay unproven.
- "Short" means short for this prover. A stronger prover might need fewer facts, and a fact on the list is one the prover could not do without, not a fact shown to be necessary: a fact can be stronger than the pair needs.
- If the check that guards against proving a pair for the wrong reason breaks, the answer stays "unknown". If the test engine simply cannot run the queries, the answer is kept and says it was not checked.
- The proof shown next to the list names its own assumptions, including, when declared keys come from BigQuery, that those keys are not enforced.
- The answer says nothing about speed or cost, only about the rows returned.

The full guide has the API fields, the command-line exit codes and how the search avoids proving a pair for the wrong reason. The [eval guide](evals/conditional-equivalence.md) says how well it works on public benchmarks.
