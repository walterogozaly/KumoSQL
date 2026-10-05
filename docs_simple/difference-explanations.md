# Why two queries differ: "equivalent except when"

[All simple guides](README.md) · [Full reference](../docs/difference-explanations.md)

When KumoSQL says two queries are different, it can show a small database where they disagree. That tells you they differ, but not why. A **difference explanation** says it in one line of SQL: the queries return the same rows, except for rows where a condition holds.

Suppose you change `WHERE status = 'a'` to `WHERE status = 'a' OR status IS NULL`. KumoSQL answers: "Equivalent except when `status IS NULL`." You can now decide whether rows with no status exist or matter, or add a rule that says the column is never empty, without studying a made-up database.

## What you get

- The condition, written in SQL from the pieces your own queries use.
- A note on whether the condition is the whole difference or only covers it.
- Nothing at all when KumoSQL cannot back a condition up. A missing explanation never means the queries are the same.

## Where to find it

- Settings, Solver, **Compare queries**: the line "Equivalent except when ..." appears above the example database.
- The command line, with `--explain-difference`, on `prove-sql-equivalent` and `prove-sql-smt`.
- Change reports, when you ask for differences to be explained (`--explain-differences`): the condition is listed next to each changed model it applies to.
- The API, by sending `"explain": true` with the two queries.

It is off unless you ask, so nothing you already use changes.

## Limits

- The condition describes where the two queries differ in KumoSQL's checks. It does not say how many rows in your warehouse meet it, so run a count with the same condition before you decide.
- Sometimes the condition is wider than the real difference. KumoSQL tells you when that is the case.
- It covers conditions on one table at a time. A difference that depends on two tables together gets no explanation.
- It can take extra time, and it gives up quietly when the time limit runs out.

The [full reference](../docs/difference-explanations.md) has the exact fields and options. How often it finds an explanation on public examples is on the scoreboard in the [README](../README.md).
