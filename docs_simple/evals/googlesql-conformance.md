# Does the Python BigQuery evaluator give Google's answers?

[Simple eval index](README.md) · [Full reference](../../docs/evals/googlesql-conformance.md)

KumoSQL has a small interpreter, written in Python, that runs BigQuery SQL on in-memory tables. This eval asks whether it returns what Google's own compliance tests say a query should return.

Google publishes those tests for GoogleSQL, the language of BigQuery. Each has a query, the tables it reads and the rows (or the error) a correct engine gives. The eval runs each query the interpreter claims to handle, and each case ends in one of three ways: exact, unsupported (the interpreter says it does not know), or mismatch (it answered, and the answer was wrong). A mismatch is a bug, so the goal is zero of them. Saying "unsupported" is always allowed.

For example, a test asks for `NaN BETWEEN 1 AND 2`. The right answer is false. The first version said true, because it computed "not less than 1" and a NaN is not less than anything. That was caught on the held-out files and fixed.

## What counts

About 4,700 of the 12,000 cases were claimed up front: queries using features BigQuery has, with no protocol buffers, JSON, ranges or maps. A quarter of the test files were kept aside before any code was written. The interpreter was built against the rest.

## Limits of the evidence

- Matching Google's reference answers is not the same as matching live BigQuery.
- On the files it was built against, the interpreter gets 85.7% exact and none wrong. On the held-out files, the first run got 61.9% exact with 30 wrong answers; those were fixed, so the held-out score now is "tuned on test". Use 62% as the honest estimate for new queries.
- Some of the exact answers are errors that the interpreter also raises.
- About 250 of the cases it declines read columns of integer and float sizes that BigQuery does not have.

The full reference has the pinned source version, the exact numbers and what was fixed. Run it with `python tools/googlesql_conformance.py`.
