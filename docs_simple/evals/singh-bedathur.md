# Checking alternative LeetCode SQL solutions

[Simple eval index](README.md) · [Full reference](../../docs/evals/singh-bedathur.md)

The Singh and Bedathur corpus contains pairs of LeetCode SQL solutions. KumoSQL checks whether each pair agrees, then compares its answer with the published label.

## How it decides

The checker tries proof and safe canonical rewrites, or searches for a database where the results differ. It does not read the published label while deciding.

A proof establishes equivalence under the supported semantics and assumptions. A counterexample establishes a difference on one valid database. If neither is found, the answer stays unknown.

Labels and checker outcomes are compared afterward. This separates the deciding method from the answer key.

## Where the cases live

The source repository has no license file, so the query files are downloaded into a cache from a pinned version and checked by hashes. They are not copied into KumoSQL's repository.

The automated tests include a smaller fixed sample and a slow full-corpus run. Download failures can cause those tests to skip, so check the test output before treating a run as complete.

The full guide provides commands, corpus details, canonicalization limits, score tables, and label comparisons. The rules were developed with available cases in view; consult its caveats before treating the percentage as unseen-case performance.
