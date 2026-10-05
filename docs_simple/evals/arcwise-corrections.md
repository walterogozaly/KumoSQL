# Checking the corrected BIRD queries

[Simple eval index](README.md) · [Full reference](../../docs/evals/arcwise-corrections.md)

BIRD is a well-known text-to-SQL benchmark: a question in English and a "gold" SQL query that answers it. A research team found that many of the gold queries were wrong and published a corrected query next to each original. This suite takes those original and corrected queries as pairs. A person decided they do not mean the same thing, so KumoSQL must never say they are equivalent.

## What it does

For each pair KumoSQL either finds a database where the two queries return different rows, or leaves the pair unknown. A proof of equivalence would be suspicious: it is only accepted if someone checks by hand that the "repair" changed nothing (for instance, only `INNER JOIN` became `JOIN`).

For example, one repair changes `COUNT(city)` to `COUNT(DISTINCT city)`. A small generated database in which two zip codes share a city gives 2 for the first query and 1 for the second, so the pair is refuted.

## Limits

* BIRD's own databases cannot be downloaded here, so the databases are generated from BIRD's table layouts. Pairs that need realistic dates or text formats usually stay unknown; unknown does not mean the repair is harmless.
* The suite checks that two queries differ, not that the corrected query is the right answer to the question.
* The data is licensed CC BY-SA, so it is downloaded from a pinned version when you run the suite and is never stored in this repository. The test skips when the download is not possible.

The full guide records the pinned version, the counts, the scores and how a pair is decided.
