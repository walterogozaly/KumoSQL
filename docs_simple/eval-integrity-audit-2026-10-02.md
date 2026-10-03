# An outside audit of the checks

[All simple guides](README.md) · [Full reference](../docs/eval-integrity-audit-2026-10-02.md)

An outside auditor read KumoSQL's own checks (the test that scores a set of sample queries, the pairs of queries used to test the provers, and the random testing of the solver) and asked a plain question: when a check passes, what has it actually shown?

For example, the random testing of the solver reported 133 proofs, but 106 of them were a query compared with an identical copy of itself. Only 27 involved changed text, so the headline number overstated how much was tested.

The audit did not find anything dishonest. It found places where a number looked bigger than the evidence behind it. What each finding meant on the current code, and what was done about it, is in [Eval integrity audit status](eval-integrity-status.md).

Limits: the audit describes an older version of the code, and no live BigQuery run was used to confirm its findings. The full report keeps the auditor's own words and measurements.
